"""独立进程演练CLI与故障隐私；无网络、无真实模型或日常资料。"""

import json
import os
import subprocess
import sys
from backend.config.settings.app_settings import PROJECT_ROOT


def test_cli_run_verify_ignores_ambient_settings_and_keeps_runtime_quarantined(tmp_path):
    output = tmp_path / "rehearsal"
    environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT), "PYTHONDONTWRITEBYTECODE": "1",
                   "INFERENCE_DATABASE_PATH": str(tmp_path / "DO_NOT_CREATE.sqlite3"), "INFERENCE_PORT": "invalid-ambient-value"}
    prefix = [sys.executable, "-X", "utf8", "-m", "backend.modules.deployment.migration_cli"]
    results = []
    for arguments in (("run", "--output", str(output)), ("verify", "--root", str(output))):
        result = subprocess.run(prefix + list(arguments), cwd=tmp_path, env=environment,
            capture_output=True, text=True, encoding="utf-8", timeout=60)
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["syntheticOnly"] and not payload["newExecutionEnabled"]
        assert payload["state"] == "quarantined"
        assert "迁移保留测试结果" not in result.stdout and "configFingerprint" not in result.stdout
        results.append(payload)
    assert results[0] == results[1]
    assert not (tmp_path / "DO_NOT_CREATE.sqlite3").exists()
    script = '''
from pathlib import Path
import sys
from backend.config.settings.app_settings import AppSettings
from backend.common.exceptions.app_error import StartupError
from backend.modules.api.app import create_app
root=Path(sys.argv[1])
for kind in ('legacy','upgraded'):
    try:create_app(AppSettings(database_path=root/kind/'db/center.sqlite3',log_dir=root/'DO_NOT_START'))
    except StartupError as error:assert 'RESTORE_QUARANTINED' in str(error)
    else:raise AssertionError('migration fixture started')
assert not (root/'DO_NOT_START').exists()
print('MIGRATION_START_BLOCKED')
'''
    result = subprocess.run([sys.executable, "-X", "utf8", "-c", script, str(output)], cwd=tmp_path,
        env=environment, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "MIGRATION_START_BLOCKED"
    (output / "migration-evidence.json").write_text("SYNTHETIC_PRIVATE_ERROR", encoding="ascii")
    result = subprocess.run(prefix + ["verify", "--root", str(output)], cwd=tmp_path,
        env=environment, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 2 and "MIGRATION_REHEARSAL_FAILED" in result.stderr
    assert "SYNTHETIC_PRIVATE_ERROR" not in result.stderr
