"""真正回环 HTTP：中心→Node→模拟业务；不加载任何模型。"""

from contextlib import contextmanager
from copy import deepcopy
from io import BytesIO
import json
from threading import Thread
import wave

import pytest
from flask import Flask,Response,jsonify,request
from werkzeug.serving import make_server

from backend.modules.host_node.app import close_node_app
from backend.service.host_node.execution_client import NodeExecutionClient
from backend.service.invocations.node_bridge import NodeInvocationBridge
from backend.service.host_node.input_service import CentralMaterialSource
from backend.schema.catalog.service_schema import SaveService,CheckTarget,ActivateService
from tests.integration.node_protocol_support import protocol_case
from tests.integration.test_common_execution import case
from tests.integration.test_node_execution_bridge import EXECUTOR


@contextmanager
def serving(app):
    server=make_server("127.0.0.1",0,app,threaded=True);thread=Thread(target=server.serve_forever);thread.start()
    try:yield f"http://127.0.0.1:{server.server_port}"
    finally:server.shutdown();server.server_close();thread.join(timeout=5)


@pytest.mark.parametrize("kind,protocol,mode",[("asr","remote-v2","sync"),("asr","openai-audio","sync"),
    ("vision","vision-json-v1","sync"),("llm","chat-completions-sse","stream"),("asr","remote-v2","async")])
def test_real_http_media_stream_and_background_result(case,tmp_path,kind,protocol,mode):
    upstream=Flask("isolated-business");state={"value":None,"contexts":{},"calls":[],"phase":"running"}
    @upstream.post("/infer")
    def infer():
        state["calls"].append(("POST","infer"))
        if kind=="llm":
            body=request.get_json();assert body["model"]=="fixture-upstream" and body["stream"] is True
            assert body["messages"][0]["content"]=="NEVER_LOG_THIS_BODY"
            def chunks():
                for text,reason in (("synthetic response",None),("","stop")):
                    value={"id":"real-stream","object":"chat.completion.chunk","model":"fixture-upstream",
                        "choices":[{"index":0,"delta":{"content":text},"finish_reason":reason}]}
                    yield "data: "+json.dumps(value)+"\n\n"
                yield "data: [DONE]\n\n"
            return Response(chunks(),mimetype="text/event-stream")
        identifier=request.form["request_id"]
        field="audio" if protocol=="remote-v2" else "file" if kind=="asr" else "image"
        content=request.files[field].read()
        if kind=="asr":
            with wave.open(BytesIO(content),"rb") as audio:
                assert audio.getnchannels()==1 and audio.getframerate()==16000 and audio.getnframes()==1600
        else:
            from PIL import Image
            with Image.open(BytesIO(content)) as image:assert image.size==(64,64)
        if mode=="async":
            context=json.loads(request.form["context"])
            assert context["invocationId"]==identifier
            identity={k:context[k] for k in ("invocationId","attempt","serviceRevision","configRevision")}|{"jobId":"reference-job"}
            state["contexts"][identifier]=identity
            return jsonify(identity),202
        if kind=="vision":return jsonify(state["value"].result)
        if protocol=="openai-audio":return jsonify({"text":"reference response","segments":[]})
        return jsonify({"success":True,"engine":"sensevoice","details":{"request_id":identifier,
            "segments":[{"start":0,"end":.1,"text":"reference response"}]}})
    @upstream.get("/status/<identifier>")
    def status(identifier):
        state["calls"].append(("GET","status"))
        return jsonify(state["contexts"][identifier]|{"state":state["phase"],"progress":45.0,"stage":"running",
            "actualDevice":{"kind":"cpu","deviceId":"simulated-service"}})
    @upstream.get("/result/<identifier>")
    def result(identifier):
        state["calls"].append(("GET","result"))
        return jsonify(state["contexts"][identifier]|{"state":"succeeded","result":state["value"].result})
    with serving(upstream) as base_url:
        value=protocol_case(case,tmp_path/"node",kind=kind,protocol=protocol,mode=mode,query=mode=="async",base_url=base_url)
        state["value"]=value;value.executor.adapter.transport=None
        try:
            with serving(value.node.app) as node_url:
                cfg=deepcopy(case.definitions[value.service]);cfg["targets"][0]["node"]["baseUrl"]=node_url
                current=case.catalog.save(value.service,SaveService.model_validate({"config":cfg,"expectedVersion":case.entries[value.service]["version"]}))
                case.catalog.check(value.service,"primary",CheckTarget(expected_version=current["version"],revision=current["draftRevision"]))
                case.entries[value.service]=case.catalog.activate(value.service,ActivateService(expected_version=current["version"],revision=current["draftRevision"],enabled=True))
                case.definitions[value.service]=cfg
                bridge=NodeInvocationBridge(case.core,material_source=CentralMaterialSource(case.core),client_factory=lambda binding:NodeExecutionClient(binding,environ={"TEST_NODE_EXECUTOR":EXECUTOR}))
                if kind=="llm":
                    accepted,online=case.online(value.service);lease=case.core.claim_online(accepted["invocationId"],accepted["workerId"])
                    frames=list(bridge.stream_online(lease,online));assert frames[0]["event"]=="delta" and frames[-1]["event"]=="done"
                else:
                    _,lease=value.task();settled=bridge.dispatch(lease)
                    if mode=="async":
                        assert not settled
                        value.monitor.reconcile(lease.invocation_id);bridge.fetch_result(lease)
                        assert bridge.snapshot(lease)["monitoring"]["progress"]==45
                        state["phase"]="succeeded"
                        assert bridge.reconcile(lease.invocation_id)["resultSaved"]
                    else:assert settled
                    assert not bridge.dispatch(lease)
                assert bridge.snapshot(lease)["resultSaved"]
                assert state["calls"].count(("POST","infer"))==1
        finally:close_node_app(value.node.app)
