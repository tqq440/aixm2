"""正式目标 CLI 的隔离进程验证；复制源码但不复制日常 .env，不允许网络或模型进程。"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
from uuid import uuid4

from alembic import command
from alembic.config import Config
import pytest

from backend.config.settings.app_settings import AppSettings, PROJECT_ROOT
from backend.data.database.database import Database
from backend.service.asr.target_service import TargetAdmissionService


@pytest.fixture
def targets_cli_case(tmp_path):
    isolated_project = tmp_path / "project"
    shutil.copytree(PROJECT_ROOT / "backend", isolated_project / "backend",
                    ignore=shutil.ignore_patterns("__pycache__"))
    assert not (isolated_project / ".env").exists()
    forbidden_attempt = tmp_path / "forbidden-operation.txt"
    (isolated_project / "sitecustomize.py").write_text(
        "import os\nimport sys\nimport traceback\nfrom pathlib import Path\n"
        f"marker = Path({str(forbidden_attempt)!r})\n"
        # SQLAlchemy 导入 platform.machine() 时会执行 Windows 内置 ver。
        # 只放行该只读系统查询，模型及其他子进程仍由审计钩子阻止。
        "comspec = os.environ.get('COMSPEC', '').casefold()\n"
        "def guard(event, args):\n"
        "    if event == 'subprocess.Popen' and str(args[0]).casefold() == comspec:\n"
        "        if str(args[1]).casefold() in {comspec + ' /c \\\"ver\\\"', comspec + ' /c ver'}:\n"
        "            return\n"
        "    if event in {'socket.connect', 'socket.getaddrinfo', 'subprocess.Popen'}:\n"
        "        detail = repr(args[1]) if event == 'subprocess.Popen' else event\n"
        "        marker.write_text(event + '\\n' + detail + '\\n' + ''.join(traceback.format_stack()), encoding='utf-8')\n"
        "        raise RuntimeError('isolated CLI must not use network or subprocesses')\n"
        "sys.addaudithook(guard)\n", encoding="utf-8",
    )
    settings = AppSettings(database_path=tmp_path / "cli.sqlite3", log_dir=tmp_path / "logs",
                           upload_dir=tmp_path / "uploads", asr_model_dir=tmp_path / "no-model")
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.attributes["settings"] = settings
    command.upgrade(config, "head")

    def run(name, *, configured=True, version=None):
        environment = {key: value for key, value in os.environ.items()
                       if not key.upper().startswith("INFERENCE_")}
        environment.update(
            PYTHONPATH=str(isolated_project), PYTHONUTF8="1", PYTHONUNBUFFERED="1",
            INFERENCE_DATABASE_PATH=str(settings.database_path), INFERENCE_LOG_DIR=str(settings.log_dir),
            INFERENCE_UPLOAD_DIR=str(settings.upload_dir), INFERENCE_ASR_MODEL_DIR=str(settings.asr_model_dir),
        )
        if configured:
            environment.update(INFERENCE_REMOTE_ASR_BASE_URL="https://cli-fixture.invalid:8003",
                               INFERENCE_REMOTE_ASR_MODEL_ID="cli-fixture-asr")
            if version is not None:
                environment["INFERENCE_REMOTE_ASR_MODEL_VERSION"] = version
        result = subprocess.run(
            [sys.executable, "-m", "backend.modules.asr.targets_cli", name], cwd=isolated_project,
            env=environment, capture_output=True, text=True, encoding="utf-8", timeout=30, check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        assert not forbidden_attempt.exists(), "CLI attempted network access or child-process creation"
        assert not settings.asr_model_dir.exists(), "Registration must not load/download a model"
        return result

    return SimpleNamespace(run=run, settings=settings, root=isolated_project)


def success(result):
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_real_cli_first_repeat_registration_and_list_do_not_claim_readiness(targets_cli_case):
    case = targets_cli_case
    empty = success(case.run("list"))
    assert all(empty[name] == [] for name in ("targets", "groups", "revisions", "workers", "permits"))
    first = success(case.run("register-current"))
    repeated = success(case.run("register-current"))
    assert repeated == first
    assert first["device_kind"] == "unknown" and first["device_evidence"] is None
    assert first["config_snapshot"]["modelVersion"].startswith("integration-")
    assert first["config_snapshot"]["versionInfo"]["upstream_version"] is None
    inventory = success(case.run("list"))
    assert inventory["revisions"] == [first]
    assert len(inventory["targets"]) == len(inventory["groups"]) == 1
    assert inventory["targets"][0]["target_limit"] == inventory["groups"][0]["capacity_limit"] == 1
    assert inventory["workers"] == inventory["permits"] == []
    assert not any(key in first for key in ("ready", "model_ready", "gpu", "gpu_utilization"))


def test_real_cli_configuration_change_preserves_old_revision_and_shared_capacity(targets_cli_case):
    case = targets_cli_case
    unknown = success(case.run("register-current"))
    provided = success(case.run("register-current", version="fixture-deployment-2"))
    assert provided["revision_id"] != unknown["revision_id"]
    assert provided["endpoint_identity"] == unknown["endpoint_identity"]
    assert provided["config_snapshot"]["modelVersion"] == "fixture-deployment-2"
    inventory = success(case.run("list", version="fixture-deployment-2"))
    by_id = {item["revision_id"]: item for item in inventory["revisions"]}
    assert by_id == {unknown["revision_id"]: unknown, provided["revision_id"]: provided}
    assert len(inventory["targets"]) == len(inventory["groups"]) == 1
    assert inventory["workers"] == inventory["permits"] == []
    assert success(case.run("register-current")) == unknown


def test_real_cli_unconfigured_error_is_structured_and_does_not_inherit_host_config(targets_cli_case, monkeypatch):
    case = targets_cli_case
    monkeypatch.setenv("INFERENCE_REMOTE_ASR_BASE_URL", "https://must-not-inherit.invalid:8003")
    monkeypatch.setenv("INFERENCE_REMOTE_ASR_MODEL_ID", "must-not-inherit")
    monkeypatch.setenv("INFERENCE_UNKNOWN_FROM_HOST", "must-not-inherit")
    result = case.run("register-current", configured=False)
    assert result.returncode == 2 and result.stdout == ""
    start = result.stderr.index('{\n  "error"')
    error = json.loads(result.stderr[start:])
    assert error["error"]["code"] == "REMOTE_ASR_NOT_CONFIGURED"
    assert len(error["requestId"]) == 32
    assert "must-not-inherit" not in str(error)
    logs = (case.settings.log_dir / "asr-targets-maintenance.log").read_text(encoding="utf-8")
    assert error["requestId"] in logs and error["error"]["code"] in logs
    inventory = success(case.run("list", configured=False))
    assert all(not value for value in inventory.values())


def test_independent_target_new_binding_preserves_group_and_previous_revision(targets_cli_case):
    """独立组修订回归使用基础 Service，不声称已运行多目标 Worker。"""
    case = targets_cli_case
    settings = case.settings.model_copy(update={
        "remote_asr_base_url": "https://independent-fixture.invalid:8003",
        "remote_asr_model_id": "independent-fixture-asr", "remote_asr_model_version": "fixture-release-1",
    })
    database = Database(settings)
    try:
        old_service = TargetAdmissionService(database, settings, target_id="fixture-independent")
        original = old_service.register_current(group_id="fixture-independent-group",
            independence_evidence="fixture-only: isolated capacity, no real GPU observation")
        old_session = uuid4().hex
        old_service.bind_worker(old_session, pid=123)
        old_service.end_worker(old_session)
        changed = settings.model_copy(update={"remote_asr_model_version": "fixture-release-2"})
        new_service = TargetAdmissionService(database, changed, target_id="fixture-independent")
        # 运行入口可在绑定时登记新配置；此处没有再次显式传容量组。
        bound = new_service.bind_worker(uuid4().hex, pid=456)
        inventory = new_service.inventory()
        assert len(inventory["groups"]) == len(inventory["targets"]) == 1
        assert inventory["targets"][0]["capacity_group_id"] == "fixture-independent-group"
        revisions = {row["revision_id"]: row for row in inventory["revisions"]}
        assert len(revisions) == 2 and revisions[original["revision_id"]] == original
        assert revisions[bound["revision_id"]]["config_snapshot"]["modelVersion"] == "fixture-release-2"
        assert inventory["permits"] == []
        assert bound["state"] == "starting" and not bound["model_ready"]
    finally:
        database.dispose()
