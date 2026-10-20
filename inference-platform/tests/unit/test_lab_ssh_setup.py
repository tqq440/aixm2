"""在Windows PowerShell 5.1中隔离验证脚本；外部SSH、服务和密钥生成全部替身。"""

import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "setup-lab-ssh.ps1"
POWERSHELL = Path(os.environ.get("WINDIR", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
pytestmark = pytest.mark.skipif(not POWERSHELL.is_file(), reason="Windows PowerShell 5.1 required")


def ssh_string(value):
    return struct.pack(">I", len(value)) + value


BLOB = ssh_string(b"ssh-ed25519") + ssh_string(bytes(range(32)))
PUBLIC_B64 = base64.b64encode(BLOB).decode("ascii")
PUBLIC = "ssh-ed25519 " + PUBLIC_B64 + " fixture-only-not-a-real-private-key"
FINGERPRINT = "SHA256:" + base64.b64encode(hashlib.sha256(BLOB).digest()).decode("ascii").rstrip("=")
HOST_LINE = "[10.8.21.33]:52222 ssh-ed25519 " + PUBLIC_B64 + "\n"


def quoted(value):
    return "'" + str(value).replace("'", "''") + "'"


def envelope(*, encrypted=True, blob=BLOB, malformed=False):
    # Synthetic container only. No key generation, real private key or usable encrypted payload.
    kdf = ssh_string(b"fixture-salt-012345") + struct.pack(">I", 64)
    data = b"openssh-key-v1\0" + ssh_string(b"aes256-ctr" if encrypted else b"none")
    data += ssh_string(b"bcrypt" if encrypted else b"none") + ssh_string(kdf if encrypted else b"")
    data += struct.pack(">I", 1) + ssh_string(blob) + ssh_string(b"synthetic-not-a-real-key-payload")
    if malformed:
        data = data[:-3]
    return "-----BEGIN OPENSSH PRIVATE KEY-----\n" + base64.b64encode(data).decode("ascii") + "\n-----END OPENSSH PRIVATE KEY-----\n"


@pytest.fixture
def profile(tmp_path):
    folder = tmp_path / "isolated-user"
    folder.mkdir()
    return folder


def files(profile, *, private=None, public=PUBLIC, host=True):
    directory = profile / ".ssh"
    directory.mkdir(exist_ok=True)
    if private is not None:
        (directory / "id_ed25519_aixm_lab").write_text(private, encoding="ascii")
    if public is not None:
        (directory / "id_ed25519_aixm_lab.pub").write_text(public + "\n", encoding="ascii")
    if host:
        (directory / "known_hosts_aixm_lab").write_text(HOST_LINE, encoding="ascii")
    return directory


def run_ps(profile, body, *, mock_native=True):
    guard = """
function Set-Service { throw 'Unexpected real service configuration' }
function Start-Service { throw 'Unexpected real service start' }
function Get-Service { throw 'Unexpected real service read' }
function Get-LabExecutable([string]$Name) { return $Name }
"""
    if mock_native:
        guard += "function Invoke-LabNative { throw 'Unexpected native SSH/key-generation call' }\n"
    code = """
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
""" + ". " + quoted(SCRIPT) + "\n" + guard + "\n" + body
    environment = dict(os.environ, USERPROFILE=str(profile))
    completed = subprocess.run(
        [str(POWERSHELL), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand",
         base64.b64encode(code.encode("utf-16le")).decode("ascii")],
        env=environment, cwd=profile, capture_output=True, text=True, encoding="utf-8", timeout=20,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    markers = [line[len("LABJSON:"):] for line in completed.stdout.splitlines() if line.startswith("LABJSON:")]
    assert len(markers) == 1, completed.stdout + completed.stderr
    return json.loads(markers[0])


def emit(expression):
    return "Write-Output ('LABJSON:' + (" + expression + " | ConvertTo-Json -Compress -Depth 8))"


def captured_error(command):
    return "$message = $null; try { " + command + " } catch { $message = $_.Exception.Message }; " + emit("@{ error=$message }")


def test_script_parses_under_windows_powershell_51_without_action(profile):
    value = run_ps(profile, "$e=$null; $t=$null; [Management.Automation.Language.Parser]::ParseFile(" + quoted(SCRIPT) +
                   ", [ref]$t, [ref]$e) | Out-Null; " + emit("@{ errors=@($e).Count; major=$PSVersionTable.PSVersion.Major; minor=$PSVersionTable.PSVersion.Minor }"))
    assert value == {"errors": 0, "major": 5, "minor": 1}
    assert not (profile / ".ssh").exists()


def test_public_key_structure_and_fingerprint_are_independently_verified(profile):
    value = run_ps(profile, emit("ConvertTo-LabPublicKey " + quoted(PUBLIC)))
    assert value["Fingerprint"] == FINGERPRINT
    assert value["Blob"] == PUBLIC_B64
    assert value["Line"] == "ssh-ed25519 " + PUBLIC_B64 + " aixm-lab"


@pytest.mark.parametrize("text", [
    "ssh-rsa " + PUBLIC_B64, "command=evil " + PUBLIC, PUBLIC + "\n" + PUBLIC,
    "ssh-ed25519 !!!!", "ssh-ed25519 " + base64.b64encode(BLOB + b"extra").decode(),
    "ssh-ed25519 " + base64.b64encode(ssh_string(b"ssh-ed25519") + ssh_string(b"short")).decode(),
])
def test_public_key_rejects_options_malformed_algorithms_and_multiple_lines(profile, text):
    assert run_ps(profile, captured_error("ConvertTo-LabPublicKey " + quoted(text)))["error"]


@pytest.mark.parametrize("contents,accepted", [(envelope(), True), (envelope(encrypted=False), False),
                                               (envelope(malformed=True), False),
                                               (envelope(blob=BLOB[:-1] + b"Z"), False)])
def test_encryption_metadata_check_is_local_and_never_decrypts(profile, contents, accepted):
    files(profile, private=contents)
    value = run_ps(profile, captured_error("Assert-LabEncryptedKey (Get-LabPaths)"))
    assert (value["error"] is None) is accepted


def candidate(profile, content=HOST_LINE):
    path = profile / "AppData/Local/Temp/codex-swufefic-v100-known_hosts"
    path.parent.mkdir(parents=True)
    path.write_text(content, encoding="ascii")
    return path


def test_trust_requires_matching_user_verified_fingerprint_and_is_idempotent(profile):
    candidate(profile)
    body = "Set-LabHostTrust (Get-LabPaths) " + quoted(FINGERPRINT) + "; "
    body += "Set-LabHostTrust (Get-LabPaths) " + quoted(FINGERPRINT) + "; "
    body += emit("@{ present=(Test-Path -LiteralPath (Get-LabPaths).KnownHosts) }")
    assert run_ps(profile, body)["present"]
    assert (profile / ".ssh/known_hosts_aixm_lab").read_bytes() == HOST_LINE.encode("ascii")


@pytest.mark.parametrize("fingerprint", ["", "SHA256:" + "x" * 43, "SHA256:bad;run-command"])
def test_bad_or_missing_host_fingerprint_never_writes_trust(profile, fingerprint):
    candidate(profile)
    assert run_ps(profile, captured_error("Set-LabHostTrust (Get-LabPaths) " + quoted(fingerprint)))["error"]
    assert not (profile / ".ssh/known_hosts_aixm_lab").exists()


def test_trust_does_not_use_another_host_or_overwrite_existing_different_file(profile):
    path = candidate(profile, HOST_LINE.replace("52222", "22"))
    assert run_ps(profile, captured_error("Set-LabHostTrust (Get-LabPaths) " + quoted(FINGERPRINT)))["error"]
    path.write_text(HOST_LINE, encoding="ascii")
    directory = files(profile)
    original = HOST_LINE + "# retained local content\n"
    (directory / "known_hosts_aixm_lab").write_text(original, encoding="ascii")
    assert run_ps(profile, captured_error("Set-LabHostTrust (Get-LabPaths) " + quoted(FINGERPRINT)))["error"]
    assert (directory / "known_hosts_aixm_lab").read_text() == original


@pytest.mark.parametrize("action", ["Setup", "CreateKey", "Unlock", "RegisterKey"])
def test_secret_prompt_actions_reject_redirected_automation_before_side_effects(profile, action):
    value = run_ps(profile, captured_error("Invoke-LabSshSetup " + quoted(action) + " ''"))
    assert "interactive" in value["error"]
    assert not (profile / ".ssh").exists()


def test_existing_key_cannot_be_overwritten(profile):
    directory = files(profile, private="unchanged", host=False)
    before = {p.name: p.read_bytes() for p in directory.iterdir()}
    value = run_ps(profile, "function Assert-LabInteractive {}\n" + captured_error("Invoke-LabSshSetup 'CreateKey' ''"))
    assert "already exists" in value["error"]
    assert before == {p.name: p.read_bytes() for p in directory.iterdir()}


def test_create_key_never_passes_empty_passphrase_and_rejects_unencrypted_output(profile):
    body = """
function Assert-LabInteractive {}
$script:seen = @()
function Invoke-LabNative([string]$Executable, [string[]]$Arguments, [switch]$Capture) {
    $script:seen = @($Arguments)
""" + "[IO.File]::WriteAllText((Get-LabPaths).PrivateKey, " + quoted(envelope(encrypted=False)) + ");\n"
    body += "[IO.File]::WriteAllText((Get-LabPaths).PublicKey, " + quoted(PUBLIC) + "); }\n"
    body += "$message=$null; try { Invoke-LabSshSetup 'CreateKey' '' } catch { $message=$_.Exception.Message }; "
    body += emit("@{ error=$message; arguments=$script:seen }")
    value = run_ps(profile, body)
    assert "not passphrase-encrypted" in value["error"]
    assert "-N" not in value["arguments"] and "" not in value["arguments"]
    assert value["arguments"][:4] == ["-t", "ed25519", "-a", "64"]


def test_unlock_uses_one_hour_agent_lifetime_and_verifies_loaded_public_identity(profile):
    files(profile, private=envelope())
    body = """
function Assert-LabInteractive {}
function Assert-LabAgentRunning {}
$script:seen=@()
function Invoke-LabNative([string]$Executable, [string[]]$Arguments, [switch]$Capture) {
    $script:seen += ,@($Arguments)
    if ($Capture) {
""" + "return " + quoted(PUBLIC) + " } }\nInvoke-LabSshSetup 'Unlock' ''; "
    value = run_ps(profile, body + emit("@{ calls=$script:seen }"))
    assert value["calls"] == [["-t", "3600", str(profile / ".ssh/id_ed25519_aixm_lab")], ["-L"]]


def register_spies(public_in_agent=True):
    return """
function Assert-LabInteractive {}
function Assert-LabAgentRunning {}
function Assert-LabEncryptedKey { throw 'RegisterKey must not read the private key' }
$script:seen=@()
function Invoke-LabNative([string]$Executable, [string[]]$Arguments, [switch]$Capture) {
    $script:seen += @{ executable=$Executable; arguments=@($Arguments) }
    if ($Capture) {
""" + ("return " + quoted(PUBLIC) if public_in_agent else "return 'no matching identity'") + " } }\n"


def test_register_transfers_only_public_key_with_pinned_host_and_normal_interactive_auth(profile):
    files(profile, private=None)
    value = run_ps(profile, register_spies() + "Invoke-LabSshSetup 'RegisterKey' ''; " + emit("@{ calls=$script:seen }"))
    assert len(value["calls"]) == 2
    args = value["calls"][1]["arguments"]
    assert value["calls"][1]["executable"] == "ssh"
    assert args[:4] == ["-F", "none", "-p", "52222"]
    assert "StrictHostKeyChecking=yes" in args and "BatchMode=no" in args
    assert "GlobalKnownHostsFile=NUL" in args and "HostKeyAlgorithms=ssh-ed25519" in args
    assert args[args.index("-i") + 1].endswith(".pub")
    assert args[-2] == "swufe@10.8.21.33"
    assert args[-1].startswith("printf %s ") and args[-1].endswith(" | base64 -d | sh")
    remote = base64.b64decode(args[-1].split()[2]).decode("ascii")
    assert "PRIVATE KEY" not in remote and "fixture-only-not-a-real-private-key" not in remote
    assert " >> " in remote and "ssh-keygen -l -E sha256" in remote
    assert "mkdir \"$lock\"" in remote
    assert "\r" not in args[-1] and "\n" not in args[-1] and '"' not in args[-1]
    assert not (profile / ".ssh/id_ed25519_aixm_lab").exists()


def test_register_does_not_connect_if_dedicated_key_is_not_in_agent(profile):
    files(profile)
    value = run_ps(profile, register_spies(False) + "$message=$null; try { Invoke-LabSshSetup 'RegisterKey' '' } catch { $message=$_.Exception.Message }; " +
                   emit("@{ error=$message; calls=$script:seen }"))
    assert "not unlocked" in value["error"]
    assert [call["executable"] for call in value["calls"]] == ["ssh-add"]


def test_native_failure_cannot_be_reported_as_success(profile):
    value = run_ps(profile, captured_error("Invoke-LabNative " + quoted(sys.executable) + " @('-c', 'raise SystemExit(7)')"), mock_native=False)
    assert "exit 7" in value["error"]


def test_native_terminal_only_prompt_setting_is_restored_even_after_failure(profile):
    body = "$env:SSH_ASKPASS_REQUIRE='force'; $message=$null; try { Invoke-LabNative " + quoted(sys.executable)
    body += " @('-c', " + quoted("import os; raise SystemExit(7 if os.getenv('SSH_ASKPASS_REQUIRE') == 'never' else 9)") + ") } catch { $message=$_.Exception.Message }; "
    body += emit("@{ error=$message; restored=$env:SSH_ASKPASS_REQUIRE }")
    value = run_ps(profile, body, mock_native=False)
    assert value == {"error": "OpenSSH action failed (exit 7).", "restored": "force"}


def test_configure_agent_requires_admin_and_uses_manual_start_only(profile):
    body = """
$script:seen=@()
function Assert-LabAdministrator { $script:seen += 'admin-checked' }
function Set-Service([string]$Name, [string]$StartupType) { $script:seen += ($Name + ':' + $StartupType) }
function Start-Service([string]$Name) { $script:seen += ('start:' + $Name) }
function Get-Service {
    $agent=[pscustomobject]@{}
    $agent | Add-Member -MemberType ScriptMethod -Name WaitForStatus -Value { param($state,$timeout) $script:seen += ('wait:' + $state + ':' + $timeout.TotalSeconds) }
    return $agent
}
Invoke-LabSshSetup 'ConfigureAgent' ''
"""
    assert run_ps(profile, body + emit("@{ calls=$script:seen }"))["calls"] == ["admin-checked", "ssh-agent:Manual", "start:ssh-agent", "wait:Running:10"]
    value = run_ps(profile, "function Assert-LabAdministrator { throw 'admin required' }\n" + captured_error("Invoke-LabSshSetup 'ConfigureAgent' ''"))
    assert value["error"] == "admin required"


def test_status_does_not_read_private_key_or_mutate_files(profile):
    directory = files(profile, private="not a readable key format")
    before = {p.name: p.read_bytes() for p in directory.iterdir()}
    body = "function Get-Service { [pscustomobject]@{ Status='Stopped'; StartType='Manual' } }; "
    body += "$status = Invoke-LabSshSetup 'Status' '' | ConvertFrom-Json; " + emit("$status")
    value = run_ps(profile, body)
    assert set(value) == {"PrivateKeyExists", "PublicKeyExists", "KnownHostsExists", "AgentStatus", "AgentStartType", "PublicKeyFingerprint"}
    assert value["PublicKeyFingerprint"] == FINGERPRINT and value["AgentStatus"] == "Stopped"
    assert before == {p.name: p.read_bytes() for p in directory.iterdir()}


def test_guided_setup_reuses_encrypted_key_trust_and_ready_agent_without_overwrite(profile):
    directory = files(profile, private=envelope())
    before = {p.name: p.read_bytes() for p in directory.iterdir()}
    body = """
function Assert-LabInteractive {}
function Get-Service { [pscustomobject]@{ Status='Running'; StartType='Manual' } }
$script:steps=@()
function Invoke-LabSshSetup([string]$SelectedAction, [string]$Fingerprint) { $script:steps += $SelectedAction }
""" + "Invoke-LabGuidedSetup " + quoted(FINGERPRINT) + "; " + emit("@{ steps=$script:steps }")
    assert run_ps(profile, body)["steps"] == ["Unlock", "RegisterKey", "Status"]
    assert before == {p.name: p.read_bytes() for p in directory.iterdir()}


def test_guided_setup_initializes_in_order_and_reads_only_public_fingerprint(profile):
    body = """
function Assert-LabInteractive {}
function Get-Service { [pscustomobject]@{ Status='Stopped'; StartType='Disabled' } }
$script:steps=@()
function Read-Host([string]$Prompt) {
    if ($Prompt -notmatch 'PUBLIC SHA256' -or $Prompt -notmatch 'not a password') { throw 'Unsafe prompt' }
""" + "return " + quoted(FINGERPRINT) + " }\n"
    body += "function Invoke-LabSshSetup([string]$SelectedAction, [string]$Fingerprint) { $script:steps += $SelectedAction }; "
    body += "Invoke-LabGuidedSetup ''; " + emit("@{ steps=$script:steps }")
    assert run_ps(profile, body)["steps"] == ["ConfigureAgent", "CreateKey", "TrustHost", "Unlock", "RegisterKey", "Status"]


def test_guided_setup_wrong_existing_host_stops_before_unlock_or_connection(profile):
    files(profile, private=envelope())
    body = """
function Assert-LabInteractive {}
function Get-Service { [pscustomobject]@{ Status='Running'; StartType='Manual' } }
function Invoke-LabSshSetup { throw 'Must not reach actions after mismatched host trust' }
"""
    value = run_ps(profile, body + captured_error("Invoke-LabGuidedSetup 'SHA256:" + "x" * 43 + "'"))
    assert "fingerprint differs" in value["error"]


def test_remote_registration_append_is_idempotent_and_preserves_existing_keys(profile):
    git = shutil.which("git")
    bash = Path(git).resolve().parents[1] / "bin/bash.exe" if git else Path("missing-bash.exe")
    if not bash.is_file():
        pytest.skip("Git Bash unavailable for isolated POSIX registration test")
    remote = run_ps(profile, emit("@{ command=(Get-LabRegistrationCommand (ConvertTo-LabPublicKey " + quoted(PUBLIC) + ")) }"))["command"]
    target = profile / "remote-home"
    directory = target / ".ssh"
    directory.mkdir(parents=True)
    authorized = directory / "authorized_keys"
    original = b'# existing public keys stay byte-for-byte\nssh-rsa ORIGINAL fixture\n'
    authorized.write_bytes(original)
    # This shell function replaces the remote public-key inspection tool. No ssh-keygen runs.
    fake = "ssh-keygen() { if grep -Fq '" + PUBLIC_B64 + "' \"${@: -1}\"; then printf '%s\\n' '256 " + FINGERPRINT + " fixture (ED25519)'; else return 1; fi; };\n"
    environment = dict(os.environ, HOME=target.as_posix(), MSYS_NO_PATHCONV="1")
    for _ in range(2):
        completed = subprocess.run([str(bash), "--noprofile", "--norc", "-c", fake + remote],
                                   env=environment, cwd=profile, capture_output=True, text=True, timeout=20)
        assert completed.returncode == 0, completed.stderr
    contents = authorized.read_bytes()
    assert contents.startswith(original)
    assert contents.count(PUBLIC_B64.encode()) == 1
    assert not (directory / ".aixm-authorized-keys.lock").exists()
