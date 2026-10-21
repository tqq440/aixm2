"""显式隔离上报器：模拟 ASR 生命周期，不加载模型、排队或转写。"""

import argparse
import json
import os
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node-url", required=True)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--sequence-start", type=int, default=1)
    parser.add_argument("--states", nargs="+", default=["starting", "warming", "ready"],
                        choices=["starting", "warming", "ready", "busy", "draining", "stopped", "failed"])
    args = parser.parse_args()
    parsed = urlsplit(args.node_url)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.path not in {"", "/"}:
        parser.error("模拟器只允许回环 Node")
    token = os.environ["NODE_REPORT_TOKEN_ASR"]
    opener = build_opener(ProxyHandler({}), NoRedirect())
    outputs = []
    for sequence, state in enumerate(args.states, args.sequence_start):
        body = {"instanceId": args.instance_id, "configRevision": args.revision, "sequence": sequence, "state": state,
            "health": "healthy" if state in {"ready", "busy"} else "unknown",
            "modelReady": state in {"ready", "busy", "draining"}, "acceptingRequests": state in {"ready", "busy"},
            "queuedTasks": 0, "activeTasks": 1 if state == "busy" else 0}
        request = Request(args.node_url + "/node/internal/v1/services/asr/reports", data=json.dumps(body).encode(),
                          headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        with opener.open(request, timeout=3) as response:
            payload = json.load(response)["data"]
        outputs.append({"sequence": sequence, "state": payload["service"]["state"], "duplicate": payload["duplicate"]})
    print(json.dumps({"simulation": True, "inferencePerformed": False, "reports": outputs}))


if __name__ == "__main__":
    main()
