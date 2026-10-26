"""实际独立Python进程的备份/校验/恢复；没有HTTP、Redis或模型。"""

import json
import os
import subprocess
import sys
from backend.config.settings.app_settings import PROJECT_ROOT
from backend.schema.deployment.linux_schema import ValidationWorkspace
from backend.service.deployment.validation_service import create_validation_workspace
from tests.integration.recovery_support import populate_pair


def test_cli_round_trip_and_fresh_process_refuses_restored_databases(tmp_path):
    source=tmp_path/"source";snapshot=tmp_path/"snapshot";restored=tmp_path/"restored"
    create_validation_workspace(ValidationWorkspace(root=source))
    populate_pair(source)
    environment=dict(os.environ)
    environment.update(PYTHONPATH=str(PROJECT_ROOT),PYTHONDONTWRITEBYTECODE="1",INFERENCE_PORT="invalid-daily-config")
    prefix=[sys.executable,"-X","utf8","-m","backend.modules.deployment.recovery_cli"]
    outputs=[]
    for arguments in (("backup","--root",str(source),"--output",str(snapshot),"--controllers-stopped-confirmed"),
                      ("verify","--snapshot",str(snapshot)),("restore","--snapshot",str(snapshot),"--output",str(restored))):
        result=subprocess.run(prefix+list(arguments),cwd=tmp_path,env=environment,capture_output=True,text=True,encoding="utf-8",timeout=45)
        assert result.returncode==0,result.stderr
        payload=json.loads(result.stdout);assert payload["quarantined"]
        outputs.append(payload)
        assert "NEVER_LOG_THIS_RESULT" not in result.stdout and "NODE_ADMIN_TOKEN" not in result.stdout
    assert outputs[0]["snapshotId"]==outputs[1]["snapshotId"]==outputs[2]["snapshotId"]
    assert outputs[1]["databases"]["center"]["queryHeld"]==1 and not outputs[2]["queueReplayed"]
    script='''
from pathlib import Path
import sys
from backend.config.deployment.linux_settings import deployment_settings
from backend.common.exceptions.app_error import StartupError
from backend.modules.api.app import create_app
from backend.modules.host_node.app import create_node_app
source,target=map(Path,sys.argv[1:])
for kind,factory in (("center",create_app),("node",create_node_app)):
    settings,_=deployment_settings(kind,source/f"config/{kind}.env")
    settings=settings.model_copy(update={"database_path":target/f"db/{kind}.sqlite3","log_dir":target/"must-not-start"})
    try:factory(settings)
    except StartupError as error:
        assert "RESTORE_QUARANTINED" in str(error)
    else:raise AssertionError("restored database started")
assert not (target/"must-not-start").exists()
print("RESTORE_BLOCKED")
'''
    result=subprocess.run([sys.executable,"-X","utf8","-c",script,str(source),str(restored)],cwd=tmp_path,
        env=environment,capture_output=True,text=True,encoding="utf-8",timeout=30)
    assert result.returncode==0,result.stderr
    assert result.stdout.strip()=="RESTORE_BLOCKED"


def test_cli_error_does_not_echo_untrusted_manifest_or_create_restore(tmp_path):
    package=tmp_path/"broken";package.mkdir()
    (package/"manifest.json").write_text("SYNTHETIC_PRIVATE_MUST_NOT_PRINT",encoding="utf-8")
    environment={**os.environ,"PYTHONPATH":str(PROJECT_ROOT),"PYTHONDONTWRITEBYTECODE":"1"}
    result=subprocess.run([sys.executable,"-X","utf8","-m","backend.modules.deployment.recovery_cli","restore",
        "--snapshot",str(package),"--output",str(tmp_path/"restore")],env=environment,cwd=tmp_path,
        capture_output=True,text=True,encoding="utf-8",timeout=30)
    assert result.returncode==2 and "RECOVERY_CHECK_FAILED" in result.stderr
    assert "SYNTHETIC_PRIVATE_MUST_NOT_PRINT" not in result.stdout+result.stderr
    assert not (tmp_path/"restore").exists()
