"""只读主机采集与异常快照，所有 GPU 输出均为明确样例。"""

import json
import sys
from datetime import timedelta

import pytest

from backend.common.process.bounded_command import CommandFailure, run_bounded_command
from backend.modules.host_node.app import close_node_app, create_node_app
from backend.service.host_node.resource_service import NativeSampler
from backend.utils.metrics.native_metrics import cpu_percent, parse_gpu_csv, read_cpu_memory
from tests.integration.host_node_support import auth, data, node_case


def test_linux_host_observation_uses_available_memory_and_cpu_delta(tmp_path):
    (tmp_path / "stat").write_text("cpu 100 10 20 300 40 5 6 7 99 99\n", encoding="ascii")
    (tmp_path / "meminfo").write_text("MemTotal: 1000 kB\nMemAvailable: 250 kB\nMemFree: 20 kB\n", encoding="ascii")
    cpu, memory = read_cpu_memory(platform="linux", proc_root=tmp_path)
    assert cpu == (488, 340)
    assert memory["usedBytes"] == 750 * 1024 and memory["usedPercent"] == 75
    assert cpu_percent(None, cpu) is None
    assert cpu_percent((1000, 500), (1200, 550)) == 75
    assert cpu_percent((1000, 500), (900, 400)) is None


def test_gpu_csv_handles_unknown_fields_without_inventing_zero():
    gpu = parse_gpu_csv(b"0, GPU-example-1, Test GPU, 8192, [N/A], N/A\n")[0]
    assert gpu["memoryTotalBytes"] == 8192 * 1024 * 1024
    assert gpu["memoryUsedBytes"] is None and gpu["utilizationPercent"] is None


@pytest.mark.parametrize("content", [b"bad-columns", b"0,GPU-1,GPU,10,20,2", b"0,GPU-1,GPU,10,2,NaN",
    b"0,GPU-1,GPU,10,2,101", b"0,wrong,GPU,10,2,1", b"0,GPU-1,GPU,10,2,1\n1,GPU-1,GPU,10,2,1"])
def test_invalid_gpu_output_is_rejected(content):
    with pytest.raises(ValueError): parse_gpu_csv(content)


def test_native_sampler_first_cpu_is_unknown_then_calculated_and_gpu_command_is_fixed():
    samples = iter([((1000, 500), {"totalBytes": 100, "availableBytes": 50, "usedBytes": 50, "usedPercent": 50}),
                    ((1200, 550), {"totalBytes": 100, "availableBytes": 40, "usedBytes": 60, "usedPercent": 60})])
    calls = []
    def run(command, **options):
        calls.append((command, options))
        return b"0, GPU-sample, Test GPU, 8192, 1024, 10\n"
    sampler = NativeSampler(system_reader=lambda: next(samples), command_runner=run, gpu_executable="trusted-nvidia-smi")
    assert sampler.sample()["cpu"]["value"]["usedPercent"] is None
    assert sampler.sample()["cpu"]["value"]["usedPercent"] == 75
    assert calls[0][0] == ["trusted-nvidia-smi", "--query-gpu=index,uuid,name,memory.total,memory.used,utilization.gpu", "--format=csv,noheader,nounits"]
    assert calls[0][1] == {"timeout": 3, "max_bytes": 65536}


def test_failed_sampler_does_not_report_empty_idle_gpu():
    def broken(): raise OSError("not collected")
    def gpu(*_args, **_kwargs): raise CommandFailure("COMMAND_TIMEOUT")
    result = NativeSampler(system_reader=broken, command_runner=gpu, gpu_executable="fake").sample()
    assert result["cpu"]["status"] == "unavailable" and result["cpu"]["value"] is None
    assert result["gpu"]["errorCode"] == "GPU_COMMAND_TIMEOUT" and result["gpu"]["value"] is None


class SamplerFake:
    def __init__(self): self.calls = 0; self.fail = False
    def sample(self):
        self.calls += 1
        return {name: {"status": "unavailable" if self.fail else "ok", "value": None if self.fail else {"observed": 7},
                      "errorCode": "SAMPLE_FAILED" if self.fail else None} for name in ("cpu", "memory", "gpu")}


def test_resources_are_sampled_once_and_multiple_gets_only_read_snapshot(tmp_path):
    case = node_case(tmp_path)
    try:
        service = case.app.extensions["host_node_resources"]; sampler = SamplerFake(); service.sampler = sampler
        assert data(case.client.get("/node/v1/resources", headers=auth()))["sampledAt"] is None
        service.tick(); service.tick()
        for _ in range(6):
            snapshot = data(case.client.get("/node/v1/resources", headers=auth()))
            assert snapshot["components"]["gpu"]["fresh"]
        assert sampler.calls == 1
        case.clock.advance(5); sampler.fail = True; service.tick()
        failed = data(case.client.get("/node/v1/resources", headers=auth()))["components"]["gpu"]
        assert failed["value"] == {"observed": 7} and not failed["fresh"] and failed["errorCode"] == "SAMPLE_FAILED"
        assert failed["lastSuccessAt"] != failed["checkedAt"]
        sampler.fail = False; case.clock.advance(5); service.tick()
        case.clock.advance(16)
        assert not data(case.client.get("/node/v1/resources", headers=auth()))["components"]["cpu"]["fresh"]
    finally: close_node_app(case.app)


def test_resources_from_previous_node_epoch_are_stale_until_new_sample(tmp_path):
    case = node_case(tmp_path)
    service = case.app.extensions["host_node_resources"]; service.sampler = SamplerFake(); service.tick()
    close_node_app(case.app)
    case.app = create_node_app(case.settings, clock=case.clock)
    try:
        snapshot = case.app.extensions["host_node_resources"].snapshot()
        assert snapshot["components"]["cpu"]["value"] == {"observed": 7}
        assert not snapshot["components"]["cpu"]["fresh"]
    finally: close_node_app(case.app)


@pytest.mark.parametrize("script,options,expected", [
    ("print('x'*2048)", {"max_bytes": 1024}, "OUTPUT_TOO_LARGE"),
    ("import time;time.sleep(5)", {"timeout": 0.2}, "COMMAND_TIMEOUT"),
    ("raise SystemExit(1)", {}, "COMMAND_FAILED"),
])
def test_fixed_command_output_and_timeout_are_bounded(script, options, expected):
    with pytest.raises(CommandFailure) as caught:
        run_bounded_command([sys.executable, "-c", script], **options)
    assert caught.value.code == expected


def test_fixed_command_success_and_missing_tool_do_not_echo_command_arguments():
    expected = b"sample\r\n" if sys.platform == "win32" else b"sample\n"
    assert run_bounded_command([sys.executable, "-c", "print('sample')"]) == expected
    with pytest.raises(CommandFailure) as caught:
        run_bounded_command(["nonexistent-private-tool-DO_NOT_ECHO"])
    assert "DO_NOT_ECHO" not in str(caught.value)
