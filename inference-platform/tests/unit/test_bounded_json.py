"""离线策略文件读取不能借重复键/非有限数绕过输入校验。"""

from pathlib import Path

import pytest

from backend.utils.json.bounded_json import read_bounded_json


@pytest.mark.parametrize("payload", ['{"value":1,"value":2}', '{"nested":{"x":1,"x":2}}',
    '{"value":NaN}', '{"value":Infinity}', '{"value":-Infinity}', '{"unfinished":'])
def test_ambiguous_or_non_json_is_rejected(tmp_path, payload):
    path = tmp_path / "snapshot.json"
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(ValueError):
        read_bounded_json(path, max_bytes=512)


def test_utf8_bom_and_exact_size_are_supported_without_type_coercion(tmp_path):
    path = tmp_path / "snapshot.json"
    path.write_text('{"说明":"隔离","count":1,"enabled":false}', encoding="utf-8-sig")
    data = read_bounded_json(path, max_bytes=path.stat().st_size)
    assert data == {"说明": "隔离", "count": 1, "enabled": False}
    assert type(data["count"]) is int
    assert type(data["enabled"]) is bool
    with pytest.raises(ValueError):
        read_bounded_json(path, max_bytes=path.stat().st_size - 1)


@pytest.mark.parametrize("path", [Path("//server/share/input.json"), Path("https://example.invalid/input.json")])
def test_network_file_locations_are_rejected(path):
    with pytest.raises(ValueError):
        read_bounded_json(path, max_bytes=512)


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_invalid_limits_rejected_before_io(tmp_path, limit):
    with pytest.raises(ValueError):
        read_bounded_json(tmp_path / "missing.json", max_bytes=limit)
