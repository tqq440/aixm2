"""后台单采样源，GET 读取缓存；失败保留上次可信值并显示失效原因。"""

from copy import deepcopy
from datetime import datetime
import os
import shutil
from threading import Lock

from backend.common.process.bounded_command import CommandFailure, run_bounded_command
from backend.data.host_node.node_models import HostNodeResourceSnapshot
from backend.utils.metrics.native_metrics import cpu_percent, parse_gpu_csv, read_cpu_memory
from backend.utils.time.iso_time import iso_utc
from backend.utils.time.time_utils import as_utc


class NativeSampler:
    def __init__(self, *, system_reader=None, command_runner=None, gpu_executable=None):
        self.system_reader = system_reader or read_cpu_memory
        self.command_runner = command_runner or run_bounded_command
        self.gpu_executable = gpu_executable
        self.previous_cpu = None

    def sample(self):
        output = {}
        try:
            sample, memory = self.system_reader()
            percent = cpu_percent(self.previous_cpu, sample)
            self.previous_cpu = sample
            output["cpu"] = {"status": "ok" if percent is not None else "warming", "value": {
                "logicalCount": os.cpu_count(), "usedPercent": percent}, "errorCode": None}
            output["memory"] = {"status": "ok", "value": memory, "errorCode": None}
        except (OSError, ValueError, KeyError, AttributeError):
            output.update({name: {"status": "unavailable", "value": None, "errorCode": "HOST_METRICS_UNAVAILABLE"}
                           for name in ("cpu", "memory")})
        try:
            executable = self.gpu_executable or shutil.which("nvidia-smi")
            if not executable:
                raise CommandFailure("COMMAND_UNAVAILABLE")
            content = self.command_runner([executable,
                "--query-gpu=index,uuid,name,memory.total,memory.used,utilization.gpu", "--format=csv,noheader,nounits"],
                timeout=3, max_bytes=65536)
            devices = parse_gpu_csv(content)
            if not devices:
                raise CommandFailure("EMPTY_RESPONSE")
            output["gpu"] = {"status": "ok", "value": {"devices": devices}, "errorCode": None}
        except CommandFailure as exc:
            output["gpu"] = {"status": "unavailable", "value": None, "errorCode": "GPU_" + exc.code}
        except (UnicodeError, ValueError):
            output["gpu"] = {"status": "unavailable", "value": None, "errorCode": "GPU_RESPONSE_INVALID"}
        return output


class ResourceService:
    def __init__(self, node, *, sampler=None):
        self.node = node
        self.sampler = sampler or NativeSampler()
        self.next_sample = None
        self.lock = Lock()

    def tick(self):
        if not self.node.settings.metrics_enabled or not self.lock.acquire(blocking=False):
            return
        try:
            with self.node.database.transaction() as session:
                now = self.node._now(session)
            if self.next_sample is not None and 0 <= (now - self.next_sample).total_seconds() < self.node.settings.metrics_seconds:
                return
            self.next_sample = now
            values = self.sampler.sample()  # 不持数据库写锁。
            with self.node.database.transaction(immediate=True) as session:
                self.node._require_owner(session)
                row = session.get(HostNodeResourceSnapshot, 1)
                previous = deepcopy(row.payload) if row else {}
                for name, component in values.items():
                    old = previous.get(name, {})
                    if component["status"] in {"ok", "warming"}:
                        component["lastSuccessAt"] = iso_utc(now)
                    else:
                        component["value"] = old.get("value")
                        component["lastSuccessAt"] = old.get("lastSuccessAt")
                    component["checkedAt"] = iso_utc(now)
                if row is None:
                    row = HostNodeResourceSnapshot(snapshot_id=1, node_epoch_id=self.node.epoch_id, payload=values, sampled_at=now)
                    session.add(row)
                else:
                    row.node_epoch_id, row.payload, row.sampled_at = self.node.epoch_id, values, now
        finally:
            self.lock.release()

    def snapshot(self):
        with self.node.database.transaction() as session:
            row = session.get(HostNodeResourceSnapshot, 1)
            now = self.node._now(session)
            components = deepcopy(row.payload) if row else {}
            for name in ("cpu", "memory", "gpu"):
                component = components.setdefault(name, {"status": "unknown", "value": None,
                    "errorCode": "NOT_SAMPLED", "lastSuccessAt": None, "checkedAt": None})
                stamp = component.get("lastSuccessAt")
                age = (now - as_utc(datetime.fromisoformat(stamp)).replace(tzinfo=None)).total_seconds() if stamp else None
                component["fresh"] = bool(self.node.settings.metrics_enabled and row and row.node_epoch_id == self.node.epoch_id
                    and component["status"] == "ok" and age is not None and 0 <= age < self.node.settings.stale_seconds)
            return {"nodeId": self.node.settings.node_id, "enabled": self.node.settings.metrics_enabled,
                "sampledAt": iso_utc(row.sampled_at) if row else None, "evaluatedAt": iso_utc(now), "components": components}
