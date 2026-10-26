"""真实 SQLite 中心账本的隔离调用资料；无业务 HTTP 或推理。"""

from uuid import uuid4
from backend.data.database.database import database_now
from backend.data.invocations.node_dispatch_models import InferenceNodeDispatch
from backend.schema.host_node.execution_schema import ExecutionCommand
from backend.schema.monitoring.budget_schema import MonitorPermitCommand
from backend.utils.json.json_utils import canonical_json_sha256
from backend.utils.time.time_utils import as_utc


def build_calls(case, *, count=8, group=None, origin=None):
    case.group(limit=32)
    for index in range(count):
        name=f"query-{index}"
        monitoring={"protocol":"job-json-v1","statusPath":"/status/{requestId}"}
        if group:monitoring["budgetGroup"]=group(index) if callable(group) else group
        target={"targetId":"primary","name":"fixture","baseUrl":origin or f"http://service-{index}.invalid:8003",
            "invokePath":"/infer","healthPath":"/health","upstreamModel":"fixture",
            "concurrencyLimit":32,"capacityEvidence":"isolated","resourceGroupIds":["shared"],"monitoring":monitoring,
            "node":{"nodeId":f"node-{index%2}","serviceId":name,"configRevision":"b"*64,
                    "baseUrl":f"http://127.0.0.1:{18020+index%2}","executorTokenEnv":"TEST_EXECUTOR"}}
        case.service(name,targets=[target])
    case.budget(limit=32)
    requests=[]
    for index in range(count):
        owner,identifier=case.task(f"query-{index}")
        lease=case.core.claim_round("monitor-budget-fixture")[0]
        assert case.core.mark_sent(lease)
        command=ExecutionCommand(node_id=f"node-{index%2}",service_id=f"query-{index}",config_revision="b"*64,
            instance_id=uuid4(),invocation_id=identifier,attempt=lease.attempt,service_revision=lease.service_revision,
            target_id=lease.target_id,kind="asr",model_id="asr-fixture",protocol="remote-v2",input_ref=owner,
            input_sha256="a"*64,deadline_at=as_utc(lease.deadline_at))
        payload=command.model_dump(mode="json",by_alias=True)
        with case.db.transaction(immediate=True) as session:
            now=database_now(session)
            session.add(InferenceNodeDispatch(invocation_id=identifier,command_json=payload,
                request_hash=canonical_json_sha256(payload),created_at=now,updated_at=now))
        requests.append(MonitorPermitCommand(poll_id=uuid4().hex,command=command))
    return requests
