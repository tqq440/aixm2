"""离线入口的真实子进程验证：读取样例、解释选择，不访问运行环境。"""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from backend.modules.asr.device_policy_cli import main


ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "tests/fixtures/device-policy"


@pytest.mark.parametrize("name,action,kind", [
    ("gpu-first", "select", "gpu"), ("gpu-full-cpu", "select", "cpu"),
    ("gpu-unknown-wait", "wait", None), ("cpu-outside-measurement-wait", "wait", None),
])
def test_reviewed_isolated_examples(name, action, kind, capsys):
    assert main(["preview", "--input", str(EXAMPLES / (name + ".json"))]) == 0
    output = capsys.readouterr()
    data = json.loads(output.out)
    assert not output.err
    assert data["mode"] == "preview" and data["executionEnabled"] is False
    assert (data["action"], data["deviceKind"]) == (action, kind)
    assert data["evidenceKind"] in {"isolated", "none"}
    assert "未签发许可" in data["notice"]


def test_schema_command_exposes_contract_without_enable_operation(capsys):
    assert main(["schema"]) == 0
    schema = json.loads(capsys.readouterr().out)
    assert schema["additionalProperties"] is False
    assert "evaluatedAt" in schema["required"]
    with pytest.raises(SystemExit) as error:
        main(["apply"])
    assert error.value.code == 2


@pytest.mark.parametrize("content,error_code", [
    ('{"executionEnabled":false,"executionEnabled":true}', "OFFLINE_INPUT_INVALID"),
    ('{"secret":"do-not-echo-this","evaluatedAt":NaN}', "OFFLINE_INPUT_INVALID"),
    ('{"secret":"do-not-echo-this"}', "VALIDATION_ERROR"),
])
def test_bad_inputs_are_structured_and_do_not_echo_payload(tmp_path, capsys, content, error_code):
    path = tmp_path / "input.json"
    path.write_text(content, encoding="utf-8")
    assert main(["preview", "--input", str(path)]) == 2
    output = capsys.readouterr()
    assert output.out == ""
    error = json.loads(output.err)
    assert error["error"]["code"] == error_code
    assert len(error["requestId"]) == 32
    assert "do-not-echo-this" not in output.err


def test_actual_process_blocks_network_database_config_and_file_writes(tmp_path):
    # 实际独立Python解释器：审计钩子阻断越过离线范围的任何访问。
    # 子进程不继承pytest应用夹具，运行目录有故意无效的.env作为额外保护。
    env_path = tmp_path / ".env"
    env_path.write_text("INFERENCE_REMOTE_ASR_BASE_URL=not-a-valid-url\n", encoding="utf-8")
    original = env_path.read_bytes()
    program = r'''
import os, sys
sys.dont_write_bytecode = True
def audit(event, args):
    if event.startswith("socket.") or event in {"sqlite3.connect", "subprocess.Popen"}:
        raise RuntimeError("Offline boundary crossed: " + event)
    if event == "open":
        file, mode, flags = args
        name = str(file).replace("\\", "/").lower()
        if name.endswith((".env", ".sqlite", ".sqlite3", ".db")):
            raise RuntimeError("Offline environment access")
        if (isinstance(mode, str) and any(ch in mode for ch in "wax+")) or (
            isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)):
            raise RuntimeError("Offline file write")
sys.addaudithook(audit)
from backend.modules.asr.device_policy_cli import main
code = main(sys.argv[1:])
assert "backend.config.settings.app_settings" not in sys.modules
assert "backend.data.database.database" not in sys.modules
raise SystemExit(code)
'''
    env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE="1")
    process = subprocess.run([sys.executable, "-X", "utf8", "-c", program,
        "preview", "--input", str(EXAMPLES / "gpu-full-cpu.json")],
        cwd=tmp_path, env=env, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert process.returncode == 0, process.stderr
    assert process.stderr == ""
    data = json.loads(process.stdout)
    assert data["action"] == "select" and data["deviceKind"] == "cpu"
    assert data["executionEnabled"] is False and data["evidenceKind"] == "isolated"
    assert env_path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [env_path]
