"""视觉模型与结果存储边界；下载响应使用内存替身，不连接外网。"""

from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
from unittest.mock import Mock

import numpy as np
from PIL import Image
import pytest

from backend.common.exceptions.app_error import AppError
from backend.config.settings.app_settings import AppSettings
from backend.data.vision import model_store, result_store
from backend.service.vision import model_preparation_service as preparation
from backend.utils.image.annotation_utils import annotated_png


@pytest.fixture
def model_environment(tmp_path, monkeypatch):
    weights = b"test-only-weights-not-an-executable-model"
    settings = AppSettings(vision_model_dir=tmp_path / "model")
    monkeypatch.setattr(preparation, "MODEL_SIZE_BYTES", len(weights))
    monkeypatch.setattr(preparation, "MODEL_SHA256", sha256(weights).hexdigest())
    network = Mock(return_value=BytesIO(weights))
    monkeypatch.setattr(model_store, "urlopen", network)
    return settings, weights, network


def test_explicit_model_prepare_publishes_verified_files_and_cleans_stage(model_environment):
    settings, weights, network = model_environment
    manifest = preparation.prepare_model(settings)
    network.assert_called_once_with(preparation.MODEL_URL, timeout=60)
    assert (settings.vision_model_dir / preparation.MODEL_FILENAME).read_bytes() == weights
    assert json.loads((settings.vision_model_dir / "manifest.json").read_text(encoding="utf-8")) == manifest.model_dump(mode="json", by_alias=True)
    assert list(settings.vision_model_dir.parent.iterdir()) == [settings.vision_model_dir]
    assert preparation.inspect_model(settings) == manifest


def test_existing_empty_directory_can_be_prepared(model_environment):
    settings, _, _ = model_environment
    settings.vision_model_dir.mkdir()
    assert preparation.prepare_model(settings).model_id == preparation.MODEL_ID


def test_check_and_ready_prepare_never_connect_network(model_environment):
    settings, _, network = model_environment
    expected = preparation.prepare_model(settings)
    network.reset_mock()
    network.side_effect = AssertionError("不允许隐式联网")
    assert preparation.inspect_model(settings) == expected
    assert preparation.prepare_model(settings) == expected
    network.assert_not_called()


def test_missing_model_check_does_not_create_directory_or_connect(model_environment):
    settings, _, network = model_environment
    with pytest.raises(AppError) as caught:
        preparation.inspect_model(settings)
    assert caught.value.code == "VISION_MODEL_NOT_READY"
    network.assert_not_called()
    assert not settings.vision_model_dir.exists()


@pytest.mark.parametrize("failure", ["short", "same_size_bad_hash", "oversize", "network"])
def test_download_failure_never_publishes_and_removes_stage(model_environment, failure):
    settings, weights, network = model_environment
    if failure == "network":
        network.side_effect = OSError("test-only disconnected")
    else:
        payload = {"short": weights[:-1], "same_size_bad_hash": b"x" * len(weights), "oversize": weights + b"x"}[failure]
        network.return_value = BytesIO(payload)
    with pytest.raises(AppError) as caught:
        preparation.prepare_model(settings)
    expected = "VISION_MODEL_DOWNLOAD_FAILED" if failure in {"oversize", "network"} else "VISION_MODEL_INVALID"
    assert caught.value.code == expected
    assert not settings.vision_model_dir.exists()
    assert list(settings.vision_model_dir.parent.iterdir()) == []


def test_manifest_write_failure_does_not_publish_partial_model(model_environment, monkeypatch):
    settings, _, _ = model_environment
    monkeypatch.setattr(model_store.VisionModelStore, "write_manifest", Mock(side_effect=OSError("test-only disk full")))
    with pytest.raises(AppError) as caught:
        preparation.prepare_model(settings)
    assert caught.value.code == "VISION_MODEL_WRITE_FAILED"
    assert list(settings.vision_model_dir.parent.iterdir()) == []


@pytest.mark.parametrize("corruption", ["checksum", "missing_weights", "bad_json", "wrong_manifest", "unknown_file"])
def test_invalid_nonempty_model_directory_is_never_overwritten(model_environment, corruption):
    settings, weights, network = model_environment
    preparation.prepare_model(settings)
    manifest_path = settings.vision_model_dir / "manifest.json"
    weights_path = settings.vision_model_dir / preparation.MODEL_FILENAME
    if corruption == "checksum":
        weights_path.write_bytes(b"x" * len(weights))
    elif corruption == "missing_weights":
        weights_path.unlink()
    elif corruption == "bad_json":
        manifest_path.write_text("{broken", encoding="utf-8")
    elif corruption == "wrong_manifest":
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["sourceRevision"] = "0" * 40
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    else:
        weights_path.unlink()
        manifest_path.unlink()
        (settings.vision_model_dir / "keep.txt").write_text("preserve me", encoding="utf-8")
    before = {p.name: p.read_bytes() for p in settings.vision_model_dir.iterdir()}
    network.reset_mock()
    network.side_effect = AssertionError("不能用下载覆盖现有内容")
    with pytest.raises(AppError) as caught:
        preparation.prepare_model(settings)
    assert caught.value.code == "VISION_MODEL_INVALID"
    assert {p.name: p.read_bytes() for p in settings.vision_model_dir.iterdir()} == before
    network.assert_not_called()


def test_concurrent_model_publish_preserves_winner_and_cleans_own_stage(model_environment, monkeypatch):
    settings, _, _ = model_environment
    original_rename = Path.rename

    def competing_publish(path, target):
        target = Path(target)
        if target == settings.vision_model_dir:
            target.mkdir()
            (target / "winner.txt").write_text("other operation", encoding="utf-8")
        return original_rename(path, target)

    monkeypatch.setattr(Path, "rename", competing_publish)
    with pytest.raises(AppError) as caught:
        preparation.prepare_model(settings)
    assert caught.value.code == "VISION_MODEL_WRITE_FAILED"
    assert (settings.vision_model_dir / "winner.txt").read_text(encoding="utf-8") == "other operation"
    assert list(settings.vision_model_dir.parent.iterdir()) == [settings.vision_model_dir]


def test_result_store_publishes_exact_json_and_image_together(tmp_path):
    output = tmp_path / "result"
    png = annotated_png(np.zeros((6, 8, 3), dtype=np.uint8), [])
    result_store.write_detection_result(output, '{"detections":[]}\n', png)
    assert sorted(p.name for p in output.iterdir()) == ["annotated.png", "result.json"]
    assert (output / "result.json").read_text(encoding="utf-8") == '{"detections":[]}\n'
    assert (output / "annotated.png").read_bytes() == png
    assert list(tmp_path.iterdir()) == [output]


@pytest.mark.parametrize("existing_kind", ["file", "empty_directory", "directory_with_files"])
def test_result_store_never_overwrites_existing_target(tmp_path, existing_kind):
    output = tmp_path / "result"
    if existing_kind == "file":
        output.write_text("previous", encoding="utf-8")
    else:
        output.mkdir()
        if existing_kind == "directory_with_files":
            (output / "keep.txt").write_text("previous", encoding="utf-8")
    with pytest.raises(FileExistsError):
        result_store.write_detection_result(output, "new json", b"new png")
    if existing_kind == "file":
        assert output.read_text(encoding="utf-8") == "previous"
    elif existing_kind == "empty_directory":
        assert list(output.iterdir()) == []
    else:
        assert (output / "keep.txt").read_text(encoding="utf-8") == "previous"


@pytest.mark.parametrize("failed_flush", [1, 2])
def test_result_fsync_failure_leaves_no_half_published_directory(tmp_path, monkeypatch, failed_flush):
    output = tmp_path / "result"
    actual_fsync = result_store.os.fsync
    calls = 0

    def failing_fsync(descriptor):
        nonlocal calls
        calls += 1
        if calls == failed_flush:
            raise OSError("test-only disk full")
        actual_fsync(descriptor)

    monkeypatch.setattr(result_store.os, "fsync", failing_fsync)
    with pytest.raises(OSError, match="disk full"):
        result_store.write_detection_result(output, "json", b"png")
    assert not output.exists()
    assert list(tmp_path.iterdir()) == []


def test_result_store_preserves_concurrent_winner(tmp_path, monkeypatch):
    output = tmp_path / "result"
    actual_rename = Path.rename

    def racing_rename(path, target):
        output.mkdir()
        (output / "winner.txt").write_text("previous", encoding="utf-8")
        return actual_rename(path, target)

    monkeypatch.setattr(Path, "rename", racing_rename)
    with pytest.raises(OSError):
        result_store.write_detection_result(output, "json", b"png")
    assert (output / "winner.txt").read_text(encoding="utf-8") == "previous"
    assert list(tmp_path.iterdir()) == [output]


def test_empty_annotation_preserves_dimensions_colors_and_original_array():
    original = np.arange(6 * 8 * 3, dtype=np.uint8).reshape((6, 8, 3))
    before = original.copy()
    image = annotated_png(original, [])
    with Image.open(BytesIO(image)) as decoded:
        assert decoded.size == (8, 6)
        assert decoded.mode == "RGB"
        np.testing.assert_array_equal(np.array(decoded), original)
    np.testing.assert_array_equal(original, before)


@pytest.mark.parametrize("box", [(7.5, 0, 8, 1), (0, 5.5, 1, 6), (7.5, 5.5, 8, 6)])
def test_subpixel_box_at_image_edge_can_be_drawn(box):
    image = annotated_png(np.zeros((6, 8, 3), dtype=np.uint8), [("person", 0.9, box)])
    with Image.open(BytesIO(image)) as decoded:
        assert decoded.size == (8, 6)
