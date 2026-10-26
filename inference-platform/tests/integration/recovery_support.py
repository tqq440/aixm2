"""含成功、未知、排队、许可和Key的隔离成套资料，不执行真实模型。"""

from io import BytesIO
from types import SimpleNamespace
from uuid import uuid4
from alembic.config import Config
from backend.config.settings.app_settings import PROJECT_ROOT
from backend.config.deployment.linux_settings import deployment_settings
from backend.data.files.upload_store import LocalUploadStore
from backend.data.tasks.task_models import TaskRecord
from backend.data.host_node.execution_models import HostNodeExecution
from backend.modules.api.app import create_app, close_app
from backend.modules.host_node.app import create_node_app, close_node_app
from backend.schema.host_node.execution_schema import ExecutionAvailability, ExecutionReceipt
from backend.schema.monitoring.budget_schema import MonitorPermitCommand
from backend.schema.gateway.gateway_schema import KeyCommand
from backend.service.invocations.node_bridge import NodeInvocationBridge
from backend.service.monitoring.budget_service import MonitorBudgetService
from tests.integration.host_node_support import data,config,report_body
from tests.integration.test_node_execution_bridge import Adapter
from tests.integration.test_common_execution import Case


def populate_pair(root):
    center_settings,_=deployment_settings("center",root/"config/center.env")
    node_settings,_=deployment_settings("node",root/"config/node.env")
    center=create_app(center_settings);node=None
    try:
        migration=Config(str(PROJECT_ROOT/"alembic.ini"));migration.attributes["settings"]=center_settings
        case=Case(center,center_settings,migration)
        adapter=Adapter();node=create_node_app(node_settings,execution_adapter=adapter)
        control=SimpleNamespace(app=node,client=node.test_client(),service=node.extensions["host_node_service"])
        definition=config();definition["modelId"]="asr-fixture"
        definition["reportTokenEnv"]="NODE_REPORT_TOKEN_ASR"
        header={"Authorization":"Bearer "+node_settings.admin_token.get_secret_value()}
        entry=data(control.client.put("/node/v1/admin/services/asr",headers=header,json={"config":definition,"expectedVersion":None}))
        entry=data(control.client.post("/node/v1/admin/services/asr/instances",headers=header,json={
            "instanceId":str(uuid4()),"configRevision":entry["configRevision"],"expectedGeneration":entry["generation"]}),201)
        report_token=node_settings.report_tokens["NODE_REPORT_TOKEN_ASR"].get_secret_value()
        data(control.client.post("/node/internal/v1/services/asr/reports",headers={"Authorization":"Bearer "+report_token},json=report_body(entry)))
        target={"targetId":"primary","name":"isolated","baseUrl":"http://127.0.0.1:18003","invokePath":"/asr",
            "healthPath":"/health","concurrencyLimit":2,"capacityEvidence":"isolated test fixture only","resourceGroupIds":["shared"],"node":{
                "nodeId":node_settings.node_id,"serviceId":"asr","configRevision":entry["configRevision"],
                "baseUrl":"http://127.0.0.1:18020","executorTokenEnv":"DEPLOY_NODE_EXECUTOR_TOKEN"}}
        case.group(limit=2);case.service(targets=[target]);case.budget(limit=2)
        case.budget(app="backup-client",limit=1)
        key=center.extensions["gateway_keys"].issue("backup-client",KeyCommand(label="isolated-backup"))
        executions=node.extensions["host_node_executions"]
        class Client:
            def __init__(self,binding):pass
            def availability(self):return ExecutionAvailability.model_validate(executions.availability("asr"))
            def execute(self,command):return ExecutionReceipt.model_validate(executions.execute(command))
            def result(self,identifier):return ExecutionReceipt.model_validate(executions.get(identifier,include_result=True))
        bridge=NodeInvocationBridge(case.core,client_factory=Client)
        identifiers=[];unknown=None
        for index in range(3):
            owner,invocation=case.task()
            stored=LocalUploadStore(center_settings.upload_dir).save(BytesIO(b"SYNTHETIC_AUDIO_"+str(index).encode()),max_bytes=1024)
            with case.db.transaction(immediate=True) as session:
                row=session.get(TaskRecord,owner);row.input_ref=stored.input_ref;row.input_sha256=stored.sha256;row.input_size_bytes=stored.size_bytes
            identifiers.append(owner)
            if index==2:continue
            lease=case.core.claim_round("snapshot-fixture")[0]
            if index==1:
                def fail(command):raise RuntimeError("SYNTHETIC_UNKNOWN_NO_RETRY")
                adapter.action=fail
            bridge.dispatch(lease)
            if index==1:
                case.core.fail(lease)
                unknown=lease.invocation_id
                command=bridge.prepare(lease)
                poll=MonitorPermitCommand(poll_id=uuid4().hex,command=command)
                assert MonitorBudgetService(case.db).change(poll).state=="held"
                with control.service.database.transaction(immediate=True) as session:
                    row=session.get(HostNodeExecution,unknown)
                    row.monitoring_json={**(row.monitoring_json or {}),"budgetPollId":poll.poll_id,"budgetPhase":"uncertain",
                        "queryBudgetState":"uncertain","queryBudgetReason":"MONITOR_QUERY_UNCONFIRMED"}
        return SimpleNamespace(tasks=identifiers,unknown=unknown,poll_id=poll.poll_id,key_id=key["keyId"],calls=len(adapter.calls))
    finally:
        if node:close_node_app(node)
        close_app(center)
