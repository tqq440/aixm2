"""真实系统采集入口的隔离测试；不修改注册表或使用真实机器标识作样例。"""

from contextlib import contextmanager
from hashlib import sha256
import sys
from types import SimpleNamespace

import pytest

from backend.data.nodes import local_probe
from backend.utils.identity.identity_utils import hash_namespaced_uuid


SAMPLE_GUID = "12345678-90ab-4cde-8f01-234567890abc"


@pytest.fixture
def windows_probe(monkeypatch):
    calls = []

    @contextmanager
    def open_key(root, path, reserved, access):
        calls.append((root, path, reserved, access))
        yield "test-registry-handle"
        calls.append("closed")

    def query_value(key, name):
        calls.append((key, name))
        return SAMPLE_GUID, 1

    registry = SimpleNamespace(
        HKEY_LOCAL_MACHINE="test-HKLM", KEY_READ=0x20019, KEY_WOW64_64KEY=0x100,
        REG_SZ=1, OpenKey=open_key, QueryValueEx=query_value,
    )
    monkeypatch.setitem(sys.modules, "winreg", registry)
    monkeypatch.setattr(local_probe.platform, "system", lambda: "Windows")
    monkeypatch.setattr(local_probe.platform, "node", lambda: "test-laptop")
    monkeypatch.setattr(local_probe.platform, "version", lambda: "10.0.12345")
    monkeypatch.setattr(local_probe.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(local_probe.os, "cpu_count", lambda: 8)
    return registry, calls


def test_windows_probe_reads_fixed_key_with_readonly_64bit_view_and_hides_raw_guid(windows_probe):
    registry, calls = windows_probe
    snapshot = local_probe.probe_local_node()
    assert snapshot == {
        "local_identity": sha256(f"windows:{SAMPLE_GUID}".encode()).hexdigest(),
        "hostname": "test-laptop", "os_name": "Windows", "os_version": "10.0.12345",
        "architecture": "AMD64", "cpu_count": 8,
    }
    assert calls == [
        (registry.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", 0,
         registry.KEY_READ | registry.KEY_WOW64_64KEY),
        ("test-registry-handle", "MachineGuid"), "closed",
    ]
    assert SAMPLE_GUID not in str(snapshot)
    assert not any("gpu" in key.lower() for key in snapshot)


def test_same_uuid_spelling_produces_same_stable_identity():
    assert hash_namespaced_uuid("{" + SAMPLE_GUID.upper() + "}", namespace="windows") == (
        hash_namespaced_uuid(SAMPLE_GUID, namespace="windows"))
    assert hash_namespaced_uuid(SAMPLE_GUID, namespace="linux") != (
        hash_namespaced_uuid(SAMPLE_GUID, namespace="windows"))


def test_non_windows_fails_without_registry_access(windows_probe, monkeypatch):
    _, calls = windows_probe
    monkeypatch.setattr(local_probe.platform, "system", lambda: "Linux")
    with pytest.raises(OSError, match="Windows"):
        local_probe.probe_local_node()
    assert calls == []


@pytest.mark.parametrize("raw", ["", "   ", "malformed-secret-guid", None, 123,
                                  "00000000-0000-0000-0000-000000000000"])
def test_missing_or_invalid_registry_uuid_fails_without_fallback(windows_probe, raw):
    registry, _ = windows_probe
    registry.QueryValueEx = lambda *_: (raw, registry.REG_SZ)
    with pytest.raises(OSError) as caught:
        local_probe.probe_local_node()
    assert caught.value.__suppress_context__
    assert "malformed-secret-guid" not in str(caught.value)


def test_wrong_registry_type_fails(windows_probe):
    registry, _ = windows_probe
    registry.QueryValueEx = lambda *_: (SAMPLE_GUID, 7)
    with pytest.raises(OSError):
        local_probe.probe_local_node()


@pytest.mark.parametrize("operation", ["OpenKey", "QueryValueEx"])
def test_registry_read_failure_is_sanitized(windows_probe, operation):
    registry, _ = windows_probe

    def denied(*_):
        raise PermissionError("sensitive raw registry value: " + SAMPLE_GUID)

    setattr(registry, operation, denied)
    with pytest.raises(OSError) as caught:
        local_probe.probe_local_node()
    assert SAMPLE_GUID not in str(caught.value)
    assert caught.value.__suppress_context__


def test_unknown_cpu_count_is_preserved_as_unknown(windows_probe, monkeypatch):
    monkeypatch.setattr(local_probe.os, "cpu_count", lambda: None)
    assert local_probe.probe_local_node()["cpu_count"] is None


@pytest.mark.parametrize("count", [0, -1, True, 1.5])
def test_invalid_cpu_count_fails(windows_probe, monkeypatch, count):
    monkeypatch.setattr(local_probe.os, "cpu_count", lambda: count)
    with pytest.raises(OSError):
        local_probe.probe_local_node()


@pytest.mark.parametrize("field,value", [("node", ""), ("node", "x" * 256),
                                        ("version", ""), ("machine", "x" * 65)])
def test_missing_or_oversized_platform_observation_fails(windows_probe, monkeypatch, field, value):
    monkeypatch.setattr(local_probe.platform, field, lambda: value)
    with pytest.raises(OSError):
        local_probe.probe_local_node()
