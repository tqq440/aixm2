"""隔离验证命令编排与SSH保护；不访问实验室服务器。"""

import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from backend.common.transfer.openssh_transport import OpenSSHTransport, TransferFailure
from backend.config.deployment.lab_settings import LabSettings, PROJECT_ROOT, REMOTE_ROOT
from backend.schema.deployment.transfer_schema import TransferRequest, validate_receiver_result
from backend.service.deployment.transfer_service import TransferService
from backend.utils.files.source_archive import canonical_json, create_archive, sha256


@pytest.fixture
def settings(tmp_path):
    return LabSettings(tmp_path / "project", tmp_path / "ssh", tmp_path / "openssh")


@pytest.mark.parametrize("data", [
    {"action": "run"}, {"action": "verify"}, {"action": "upload", "bundle_id": "../escape"},
    {"action": "check", "bundle_id": "a" * 64}, {"action": "package", "host": "evil"},
])
def test_request_rejects_arbitrary_actions_hosts_and_paths(data):
    with pytest.raises(ValidationError):
        TransferRequest.model_validate(data)


def test_missing_setup_has_no_network_or_file_writes(settings, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *args, **kw: pytest.fail("unexpected process"))
    with pytest.raises(TransferFailure, match="setup-lab") as error:
        TransferService(settings).execute(TransferRequest(action="upload"))
    assert error.value.code == "SSH_SETUP_REQUIRED"
    assert not settings.project_root.exists()


def test_ssh_options_pin_destination_and_disable_other_auth(settings):
    options = OpenSSHTransport(settings)._options()
    for value in ["BatchMode=yes", "StrictHostKeyChecking=yes", "PasswordAuthentication=no",
                  "IdentitiesOnly=yes", "ForwardAgent=no", "UpdateHostKeys=no", "ClearAllForwardings=yes"]:
        assert value in options
    assert "-F" in options
    assert f"UserKnownHostsFile={settings.known_hosts}" in options


@pytest.mark.parametrize("stderr,expected", [
    (b"Host key verification failed", "HOST_KEY_REJECTED"),
    (b"REMOTE HOST IDENTIFICATION HAS CHANGED", "HOST_KEY_REJECTED"),
    (b"Permission denied (publickey)", "SSH_AUTH_OR_PERMISSION_DENIED"),
    (b"Connection timed out", "SSH_UNREACHABLE"),
])
def test_transport_errors_are_sanitized(settings, monkeypatch, stderr, expected):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=255, stdout=b"", stderr=stderr + b" secret-test"))
    with pytest.raises(TransferFailure) as error:
        OpenSSHTransport(settings)._run(["ssh"], data=b"")
    assert error.value.code == expected
    assert "secret-test" not in str(error.value)


def test_timeout_does_not_claim_remote_failure_or_replay(settings, monkeypatch):
    calls = []
    def fail(*args, **kwargs):
        calls.append(args)
        raise subprocess.TimeoutExpired("ssh", 1)
    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(TransferFailure) as error:
        OpenSSHTransport(settings)._run(["ssh"], data=b"")
    assert error.value.code == "TRANSFER_TIMEOUT"
    assert len(calls) == 1


def test_receiver_bootstrap_works_without_project_dependencies(tmp_path):
    settings = LabSettings(PROJECT_ROOT, tmp_path, tmp_path)
    script = OpenSSHTransport(settings)._receiver_program("check", None)
    remote = tmp_path / "isolated-server"
    script = script.replace(repr(REMOTE_ROOT).encode(), repr(str(remote)).encode())
    result = subprocess.run([sys.executable, "-I", "-S", "-"], input=script, capture_output=True,
                            cwd=tmp_path, timeout=15)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["result"]["targetState"] == "absent"
    assert not remote.exists(), "check must remain read-only"


def test_upload_uses_fixed_staging_and_no_shell_local_path(settings, tmp_path, monkeypatch):
    calls = []
    def record(command, **kwargs):
        calls.append((command, kwargs))
        return b""
    transport = OpenSSHTransport(settings)
    monkeypatch.setattr(transport, "_run", record)
    folder = tmp_path / "Chinese paths $() with spaces"
    transport.upload({"bundleId": "a" * 64, "archivePath": str(folder / "source.zip"),
                      "manifestPath": str(folder / "manifest.json")})
    command, kwargs = calls[0]
    assert command[-1] == "swufe@10.8.21.33"
    assert command[command.index("-P") + 1] == "52222"
    assert kwargs["cwd"] == folder
    assert b"$()" not in kwargs["data"]
    assert f"{REMOTE_ROOT}/.aixm-incoming/".encode() in kwargs["data"]
    assert b"inference-platform/" not in kwargs["data"]


class FakeTransport:
    def __init__(self, failure=None):
        self.events = []
        self.failure = failure

    def prerequisites(self):
        return {"ready": True}

    def receiver(self, action, bundle_id=None):
        self.events.append(action)
        if self.failure == action:
            raise TransferFailure("ISOLATED_FAILURE", "isolated failure")
        return {"status": action, "bundleId": bundle_id, "fileCount": 1, "totalBytes": 5}

    def upload(self, bundle):
        self.events.append("upload")
        if self.failure == "upload":
            raise TransferFailure("ISOLATED_FAILURE", "isolated failure")


def stub_bundle(monkeypatch):
    import backend.service.deployment.transfer_service as module
    monkeypatch.setattr(module, "build_bundle", lambda *a: {"bundleId": "b" * 64, "fileCount": 1, "totalBytes": 5})


def test_upload_requires_readback_before_success(settings, monkeypatch):
    stub_bundle(monkeypatch)
    transport = FakeTransport()
    result = TransferService(settings, transport).execute(TransferRequest(action="upload"))
    assert transport.events == ["check", "prepare", "upload", "install", "verify"]
    assert result["servicesStarted"] is False


@pytest.mark.parametrize("failure,events", [
    ("check", ["check"]), ("upload", ["check", "prepare", "upload"]),
    ("install", ["check", "prepare", "upload", "install"]),
    ("verify", ["check", "prepare", "upload", "install", "verify"]),
])
def test_failure_stops_pipeline_without_retry_or_restart(settings, monkeypatch, failure, events):
    stub_bundle(monkeypatch)
    transport = FakeTransport(failure)
    with pytest.raises(TransferFailure):
        TransferService(settings, transport).execute(TransferRequest(action="upload"))
    assert transport.events == events


def test_verify_never_prepares_or_uploads(settings):
    transport = FakeTransport()
    TransferService(settings, transport).execute(TransferRequest(action="verify", bundle_id="a" * 64))
    assert transport.events == ["verify"]


def test_package_is_offline(settings, monkeypatch):
    stub_bundle(monkeypatch)
    class NoTransport:
        def __getattr__(self, name):
            pytest.fail("package used transport")
    assert TransferService(settings, NoTransport()).execute(TransferRequest(action="package"))["bundleId"] == "b" * 64


def test_disk_shortage_is_reported_before_remote_staging(settings, monkeypatch, tmp_path):
    import backend.service.deployment.transfer_service as module
    archive = tmp_path / "source.zip"
    manifest = tmp_path / "manifest.json"
    archive.write_bytes(b"source")
    manifest.write_bytes(b"{}")
    monkeypatch.setattr(module, "build_bundle", lambda *a: {
        "bundleId": "a" * 64, "archivePath": str(archive), "manifestPath": str(manifest), "totalBytes": 500})
    class FullDisk(FakeTransport):
        def receiver(self, action, bundle_id=None):
            self.events.append(action)
            return {"freeBytes": 1}
    transport = FullDisk()
    with pytest.raises(TransferFailure) as error:
        TransferService(settings, transport).execute(TransferRequest(action="upload"))
    assert error.value.code == "REMOTE_DISK_SPACE_LOW"
    assert transport.events == ["check"]


def test_receiver_rejects_broken_or_non_json_response(settings, monkeypatch):
    transport = OpenSSHTransport(LabSettings(PROJECT_ROOT, settings.ssh_dir, settings.openssh_dir))
    monkeypatch.setattr(transport, "_run", lambda *a, **k: b'{"ok":true}')
    with pytest.raises(TransferFailure) as error:
        transport.receiver("check")
    assert error.value.code == "INVALID_RECEIVER_RESPONSE"


@pytest.mark.parametrize("payload", [
    {}, {"status": "verified"},
    {"bundleId": "c" * 64, "status": "verified", "fileCount": 1, "totalBytes": 1,
     "servicesStarted": False, "dependenciesInstalled": False},
    {"bundleId": "a" * 64, "status": "installed", "fileCount": 1, "totalBytes": 1,
     "servicesStarted": False, "dependenciesInstalled": False},
])
def test_remote_verified_result_must_match_action_and_batch(payload):
    with pytest.raises(ValueError):
        validate_receiver_result("verify", "a" * 64, payload)


def test_remote_check_requires_complete_identity_and_space():
    with pytest.raises(ValidationError):
        validate_receiver_result("check", None, {"freeBytes": 1})


def test_remote_prepare_cannot_redirect_staging():
    with pytest.raises(ValidationError):
        validate_receiver_result("prepare", "a" * 64, {
            "bundleId": "a" * 64, "status": "prepared", "incomingPath": "/tmp/other",
            "archivePath": "/tmp/other/source.zip", "manifestPath": "/tmp/other/manifest.json"})


def test_local_secret_batch_is_rejected_before_sftp(settings, tmp_path):
    secret = tmp_path / "secret-fixture"
    secret.write_text("isolated-not-a-real-secret", encoding="utf-8")
    archive = tmp_path / "bad.zip"
    manifest = create_archive({"inference-platform/.env": secret}, archive)
    bundle_id = sha256(archive.read_bytes())
    folder = settings.bundle_root / bundle_id
    folder.mkdir(parents=True)
    (folder / "source.zip").write_bytes(archive.read_bytes())
    (folder / "manifest.json").write_bytes(canonical_json(manifest))
    transport = FakeTransport()
    with pytest.raises(ValueError, match="secret path"):
        TransferService(settings, transport).execute(TransferRequest(action="upload", bundle_id=bundle_id))
    assert transport.events == ["check"]


def test_content_count_mismatch_cannot_report_delivery(settings, monkeypatch):
    stub_bundle(monkeypatch)
    class Mismatched(FakeTransport):
        def receiver(self, action, bundle_id=None):
            report = super().receiver(action, bundle_id)
            if action == "verify":
                report["fileCount"] = 2
            return report
    with pytest.raises(TransferFailure) as error:
        TransferService(settings, Mismatched()).execute(TransferRequest(action="upload"))
    assert error.value.code == "RECEIVER_CONTENT_MISMATCH"
