"""本地环境问题不能伪装为任务失败，也不把连接原响应写入日志。"""

import json
from pathlib import Path
import pytest
from pydantic import ValidationError
from backend.modules.deployment.validation_log import main
from backend.schema.deployment.validation_issue_schema import ValidationDeferral
from backend.service.deployment.validation_issue_service import ValidationIssueLog


def command(**changes):
    values=dict(stage="module6b",target="swufefic-v100",code="CAMPUS_NETWORK_UNAVAILABLE",source="operator")
    values.update(changes)
    return ValidationDeferral(**values)


def test_deferred_issue_and_existing_json_logger_are_persisted_without_raw_evidence(tmp_path):
    evidence=tmp_path/"evidence";evidence.mkdir()
    (evidence/"ssh.json").write_text('{"stderr":"PRIVATE_SENTINEL"}',encoding="utf-8")
    log=ValidationIssueLog(tmp_path/"logs",evidence_root=evidence)
    result=log.defer(command(evidence_ref="ssh.json"))
    assert result["snapshotSaved"] and result["runtimeLogWritten"] and result["taskStateChanged"] is False
    issue=log.latest("module6b","swufefic-v100")
    assert issue["state"]=="waiting_external_verification" and issue["observationCount"]==1
    assert issue["source"]=="operator" and len(issue["evidenceSha256"])==64
    records=[json.loads(line) for path in (tmp_path/"logs").glob("*.log") for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(records)==1 and records[0]["level"]=="WARNING"
    assert "VALIDATION_DEFERRED" in records[0]["message"] and "CAMPUS_NETWORK_UNAVAILABLE" in records[0]["message"]
    assert "PRIVATE_SENTINEL" not in json.dumps(result)+json.dumps(records)
    assert not list((tmp_path/"logs").glob("*.tmp"))


def test_show_is_readonly_and_missing_query_does_not_create_directory(tmp_path):
    root=tmp_path/"logs";log=ValidationIssueLog(root)
    assert log.latest("module6b","lab")==None and not root.exists()
    log.defer(command())
    before={p.name:p.read_bytes() for p in root.iterdir()}
    log.latest("module6b","swufefic-v100")
    assert before=={p.name:p.read_bytes() for p in root.iterdir()}


def test_repeated_observation_keeps_first_date_and_updates_same_snapshot(tmp_path):
    log=ValidationIssueLog(tmp_path/"logs")
    first=log.defer(command())["issue"]
    second=log.defer(command(code="REDIS_SERVER_UNVERIFIED"))["issue"]
    assert second["firstObservedAt"]==first["firstObservedAt"] and second["observationCount"]==2
    assert second["lastObservedAt"]>=first["lastObservedAt"]
    assert len(list((tmp_path/"logs").glob("issue-*.json")))==1


@pytest.mark.parametrize("changes",[{"code":"TASK_FAILED"},{"stage":"../../task"},{"target":"redis://user:PRIVATE@host"},
    {"evidence_ref":"../../private.json"},{"evidence_ref":"C:/private.json"},{"evidence_ref":"secret.env"},{"evidence_ref":".env"},
    {"source":"service"},{"raw_response":"PRIVATE"}])
def test_unsafe_or_task_status_inputs_are_rejected(changes):
    with pytest.raises((ValidationError,ValueError)):command(**changes)


def test_corrupt_snapshot_is_not_overwritten(tmp_path):
    log=ValidationIssueLog(tmp_path/"logs");result=log.defer(command())
    path=Path(result["snapshotPath"]);path.write_text('{"unrelated":"keep"}',encoding="utf-8")
    before=path.read_bytes()
    with pytest.raises(ValueError):log.defer(command())
    assert path.read_bytes()==before


def test_log_open_failure_does_not_undo_saved_issue(tmp_path,monkeypatch):
    log=ValidationIssueLog(tmp_path/"logs")
    def fail(*_args,**_kwargs):raise OSError("PRIVATE_ERROR")
    monkeypatch.setattr("backend.service.deployment.validation_issue_service.configure_logging",fail)
    result=log.defer(command())
    assert result["snapshotSaved"] and result["runtimeLogWritten"] is False
    assert log.latest("module6b","swufefic-v100")["state"]=="waiting_external_verification"
    assert "PRIVATE_ERROR" not in json.dumps(result)


def test_handler_write_failure_is_reported_without_undoing_issue(tmp_path,monkeypatch):
    monkeypatch.setattr("backend.common.logging.app_logging.RotatingFileHandler.emit",lambda handler,record:handler.handleError(record))
    result=ValidationIssueLog(tmp_path/"logs").defer(command())
    assert result["snapshotSaved"] and result["runtimeLogWritten"] is False


def test_cli_ignores_invalid_daily_environment_and_safe_error_does_not_echo_input(tmp_path,monkeypatch,capsys):
    log=ValidationIssueLog(tmp_path/"logs")
    monkeypatch.setattr("backend.modules.deployment.validation_log.ValidationIssueLog",lambda:log)
    monkeypatch.setenv("INFERENCE_PORT","INVALID_DAILY_CONFIG")
    assert main(["defer","--stage","module6b","--target","lab","--code","CAMPUS_NETWORK_UNAVAILABLE"])==0
    assert main(["show","--stage","module6b","--target","lab"])==0
    assert main(["show","--stage","module6b","--target","redis://user:PRIVATE@host"])==2
    output=capsys.readouterr()
    assert "PRIVATE" not in output.out+output.err
