"""本机只读展示独立验收问题；不迁移旧日常库、不探测外部。"""

import json
from pathlib import Path
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import event
from backend.config.settings.app_settings import AppSettings,PROJECT_ROOT
from backend.modules.api.app import create_app,close_app
from backend.schema.deployment.validation_issue_schema import ValidationDeferral,ValidationIssueQuery
from backend.service.deployment.validation_issue_service import ValidationIssueLog
from backend.service.deployment.validation_openapi_service import validation_openapi


@pytest.fixture
def validation_case(tmp_path,request):
    public=getattr(request,"param",None)=="head"
    settings=AppSettings(database_path=tmp_path/"platform.sqlite3",log_dir=tmp_path/"logs",upload_dir=tmp_path/"uploads",
        asr_model_dir=tmp_path/"no-asr",vision_model_dir=tmp_path/"no-yolo",common_execution_enabled=public,
        public_api_enabled=public,ingress_token="TEST_INGRESS_"+"x"*40 if public else None)
    config=Config(str(PROJECT_ROOT/"alembic.ini"));config.attributes["settings"]=settings
    command.upgrade(config,"head" if public else "20261020_0014")
    app=create_app(settings)
    log=ValidationIssueLog(settings.log_dir/"validation",evidence_root=tmp_path/"evidence")
    try:yield app,app.test_client(),log,settings
    finally:close_app(app)


def seed(log,target="lab",stage="module6b",**values):
    return log.defer(ValidationDeferral(stage=stage,target=target,code="CAMPUS_NETWORK_UNAVAILABLE",source="operator",**values))


def test_get_works_on_0014_disabled_platform_without_sql_network_or_issue_writes(validation_case,monkeypatch):
    app,client,log,settings=validation_case;seed(log)
    before={p.name:p.read_bytes() for p in log.root.iterdir()}
    database_before=settings.database_path.read_bytes()
    def forbidden(*args,**kwargs):raise AssertionError("GET must not query DB, network or record a new issue")
    event.listen(app.extensions["database"].engine,"before_cursor_execute",forbidden)
    monkeypatch.setattr("socket.create_connection",forbidden)
    monkeypatch.setattr(ValidationIssueLog,"defer",forbidden)
    try:
        for _ in range(3):
            response=client.get("/api/admin/validation/issues")
            assert response.status_code==200,response.get_json()
            assert response.headers["Cache-Control"]=="no-store"
            assert response.get_json()["data"]["items"][0]["state"]=="waiting_external_verification"
    finally:event.remove(app.extensions["database"].engine,"before_cursor_execute",forbidden)
    assert before=={p.name:p.read_bytes() for p in log.root.iterdir()}
    assert settings.database_path.read_bytes()==database_before


def test_api_hides_evidence_paths_raw_response_and_absolute_paths(validation_case):
    _,client,log,_=validation_case
    log.evidence_root.mkdir();(log.evidence_root/"PRIVATE-ssh.json").write_text('{"stderr":"RAW_SECRET_RESPONSE"}',encoding="utf-8")
    seed(log,evidence_ref="PRIVATE-ssh.json")
    response=client.get("/api/admin/validation/issues");raw=response.get_data(as_text=True)
    assert response.status_code==200
    assert all(value not in raw for value in ("PRIVATE","RAW_SECRET_RESPONSE","evidenceRef","snapshotPath",str(log.root)))
    assert len(response.get_json()["data"]["items"][0]["evidenceSha256"])==64


def test_empty_query_does_not_create_issue_directory_or_claim_passed(validation_case):
    _,client,log,_=validation_case
    page=client.get("/api/admin/validation/issues").get_json()["data"]
    assert page["items"]==[] and page["total"]==0 and not log.root.exists()
    assert "不代表" in page["notice"] and "passed" not in page


def test_stage_filter_offset_and_order(validation_case):
    _,client,log,_=validation_case
    seed(log,"old-node",stage="module6a");seed(log,"redis-one");seed(log,"redis-two")
    page=client.get("/api/admin/validation/issues?stage=module6b&limit=1&offset=1").get_json()["data"]
    assert page["total"]==2 and page["limit"]==1 and page["offset"]==1
    assert page["items"][0]["target"]=="redis-one"
    assert client.get("/api/admin/validation/issues?stage=module6a").get_json()["data"]["total"]==1


@pytest.mark.parametrize("query",["stage=bad","offset=-1","offset=1001","limit=0","limit=201","stage=module6a&stage=module6b","rawResponse=PRIVATE"])
def test_invalid_or_duplicate_query_is_rejected(validation_case,query):
    _,client,_,_=validation_case
    assert client.get("/api/admin/validation/issues?"+query).status_code==422


@pytest.mark.parametrize("options",[
    {"environ_overrides":{"REMOTE_ADDR":"10.0.0.3"},"headers":{"Authorization":"Bearer TEST_APP_KEY"}},
    {"headers":{"Host":"attacker.invalid"}},
    {"headers":{"Origin":"https://attacker.invalid"}},
    {"headers":{"Sec-Fetch-Site":"cross-site"}},
])
def test_remote_or_cross_site_cannot_read_issue_details(validation_case,options):
    _,client,log,_=validation_case;seed(log)
    for path in ("issues","openapi.json"):
        response=client.get("/api/admin/validation/"+path,**options)
        assert response.status_code==403 and "waiting_external_verification" not in response.get_data(as_text=True)


def test_corrupt_oversize_and_identity_mismatched_files_are_counted_not_leaked(validation_case):
    _,client,log,_=validation_case;seed(log)
    (log.root/("issue-"+"a"*32+".json")).write_text("RAW_PRIVATE_CONTENT",encoding="utf-8")
    (log.root/("issue-"+"b"*32+".json")).write_bytes(b"x"*8193)
    valid=log.latest("module6b","lab")
    # 有效结构但不属于该文件的身份也不得冒充新问题。
    (log.root/("issue-"+"c"*32+".json")).write_text(json.dumps(valid),encoding="utf-8")
    response=client.get("/api/admin/validation/issues")
    assert response.status_code==200
    assert response.get_json()["data"]["unreadableCount"]==3
    assert response.get_json()["data"]["total"]==1 and "RAW_PRIVATE_CONTENT" not in response.get_data(as_text=True)


def test_scan_is_bounded_and_marks_partial_list(validation_case):
    app,client,log,_=validation_case;seed(log)
    log.max_entries=1;app.extensions["validation_issue_log_factory"]=lambda:log
    assert client.get("/api/admin/validation/issues").get_json()["data"]["scanTruncated"] is True


def test_broken_log_directory_does_not_block_platform_startup_or_leak_errors(validation_case):
    app,client,log,settings=validation_case
    log.root.write_text("keep unrelated file",encoding="utf-8")
    restarted=create_app(settings)
    close_app(restarted)
    response=client.get("/api/admin/validation/issues")
    assert response.status_code==503 and response.get_json()["error"]["code"]=="VALIDATION_LOG_UNAVAILABLE"
    assert str(log.root) not in response.get_data(as_text=True) and log.root.read_text()=="keep unrelated file"


def test_endpoint_is_get_only_and_openapi_matches_saved_contract(validation_case):
    _,client,_,_=validation_case
    assert client.post("/api/admin/validation/issues",json={}).status_code==405
    assert client.get("/api/admin/validation/openapi.json").get_json()==validation_openapi()
    assert json.loads((PROJECT_ROOT/"docs/validation-issues.openapi.json").read_text(encoding="utf-8"))==validation_openapi()
    assert client.get("/api/admin/validation/openapi.json?query=x").status_code==422


def test_cli_explicit_directory_matches_configured_api_log_root(validation_case,capsys):
    from backend.modules.deployment.validation_log import main
    _,client,log,_=validation_case
    assert main(["--log-dir",str(log.root),"defer","--stage","module6b","--target","configured-local",
        "--code","REDIS_SERVER_UNVERIFIED"])==0
    assert client.get("/api/admin/validation/issues").get_json()["data"]["items"][0]["target"]=="configured-local"


@pytest.mark.parametrize("validation_case",["head"],indirect=True)
def test_public_mode_still_requires_local_admin_and_reads_without_sql(validation_case):
    app,client,log,_=validation_case;seed(log)
    def forbidden(*_args,**_kwargs):raise AssertionError("Listing must not query application/task DB")
    event.listen(app.extensions["database"].engine,"before_cursor_execute",forbidden)
    try:
        assert client.get("/api/admin/validation/issues").status_code==200
        assert client.get("/api/admin/validation/issues",environ_overrides={"REMOTE_ADDR":"10.0.0.3"}).status_code==403
    finally:event.remove(app.extensions["database"].engine,"before_cursor_execute",forbidden)
