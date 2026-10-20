"""模型完整性与下载失败检查；微型测试文件不能作为真实模型验收。"""

import json
from pathlib import Path

import pytest

from backend.common.exceptions.app_error import AppError
from backend.config.settings.app_settings import AppSettings
from backend.data.asr.model_store import ModelStore
from backend.service.asr import model_preparation_service as preparation
from backend.utils.file.file_utils import sha256_file


@pytest.fixture
def model_setup(tmp_path, monkeypatch):
    settings = AppSettings(asr_model_dir=tmp_path / "models" / "whisper-small")
    snapshot = tmp_path / "downloaded-snapshot"
    snapshot.mkdir()
    for filename in preparation.REQUIRED_MODEL_FILES:
        (snapshot / filename).write_bytes(f"test-only {filename}".encode())
    monkeypatch.setattr(preparation, "EXPECTED_MODEL_BYTES", (snapshot / "model.bin").stat().st_size)
    monkeypatch.setattr(preparation, "EXPECTED_MODEL_SHA256", sha256_file(snapshot / "model.bin"))
    downloads = []

    def download(_store, repo, revision, filenames):
        downloads.append((repo, revision, tuple(filenames)))
        return snapshot

    monkeypatch.setattr(ModelStore, "download_snapshot", download)
    return settings, snapshot, downloads


def test_prepare_writes_verified_manifest_and_reuses_local_files(model_setup):
    settings, _snapshot, downloads = model_setup
    manifest = preparation.prepare_model(settings)
    assert downloads == [(settings.asr_model_repo, settings.asr_model_revision, preparation.REQUIRED_MODEL_FILES)]
    saved = json.loads((settings.asr_model_dir / "manifest.json").read_text(encoding="utf-8"))
    assert saved["modelRevision"] == settings.asr_model_revision
    assert saved["files"]["model.bin"]["sizeBytes"] == preparation.EXPECTED_MODEL_BYTES
    assert preparation.inspect_model(settings) == manifest
    assert preparation.prepare_model(settings) == manifest
    assert len(downloads) == 1


def test_inspection_without_manifest_never_downloads(model_setup):
    settings, _snapshot, downloads = model_setup
    with pytest.raises(AppError) as caught:
        preparation.inspect_model(settings)
    assert caught.value.code == "MODEL_NOT_READY"
    assert not downloads
    assert not settings.asr_model_dir.exists()


@pytest.mark.parametrize("filename", preparation.REQUIRED_MODEL_FILES)
def test_inspection_rejects_missing_required_file(model_setup, filename):
    settings, _snapshot, downloads = model_setup
    preparation.prepare_model(settings)
    (settings.asr_model_dir / filename).unlink()
    with pytest.raises(AppError) as caught:
        preparation.inspect_model(settings)
    assert caught.value.code == "MODEL_INVALID"
    assert filename in caught.value.message
    assert len(downloads) == 1


def test_inspection_rejects_same_size_corruption(model_setup):
    settings, _snapshot, _downloads = model_setup
    preparation.prepare_model(settings)
    path = settings.asr_model_dir / "tokenizer.json"
    path.write_bytes(b"x" * path.stat().st_size)
    with pytest.raises(AppError, match="tokenizer.json") as caught:
        preparation.inspect_model(settings)
    assert caught.value.code == "MODEL_INVALID"


def test_inspection_rejects_forged_weight_manifest(model_setup):
    settings, _snapshot, _downloads = model_setup
    preparation.prepare_model(settings)
    weights = settings.asr_model_dir / "model.bin"
    weights.write_bytes(b"invalid weights")
    store = ModelStore(settings.asr_model_dir)
    manifest = json.loads(store.read_manifest())
    manifest["files"]["model.bin"] = {"sizeBytes": weights.stat().st_size, "sha256": sha256_file(weights)}
    store.write_manifest(json.dumps(manifest))
    with pytest.raises(AppError, match="可信大小或 SHA256"):
        preparation.inspect_model(settings)


@pytest.mark.parametrize("change", ["revision", "repo", "files", "malformed"])
def test_inspection_rejects_invalid_manifest(model_setup, change):
    settings, _snapshot, _downloads = model_setup
    preparation.prepare_model(settings)
    store = ModelStore(settings.asr_model_dir)
    manifest = json.loads(store.read_manifest())
    if change == "revision":
        manifest["modelRevision"] = "a" * 40
    elif change == "repo":
        manifest["modelRepo"] = "some-other/model"
    elif change == "files":
        manifest["files"]["../outside"] = manifest["files"].pop("config.json")
    store.write_manifest("{invalid json" if change == "malformed" else json.dumps(manifest))
    with pytest.raises(AppError) as caught:
        preparation.inspect_model(settings)
    assert caught.value.code == "MODEL_INVALID"


def test_download_failure_does_not_publish_manifest_or_damage_previous_files(model_setup, monkeypatch):
    settings, _snapshot, _downloads = model_setup
    preparation.prepare_model(settings)
    store = ModelStore(settings.asr_model_dir)
    old_manifest = json.loads(store.read_manifest())
    old_manifest["modelRevision"] = "a" * 40
    store.write_manifest(json.dumps(old_manifest))
    before = {entry.name: entry.read_bytes() for entry in settings.asr_model_dir.iterdir()}

    def fail_download(*_args):
        raise ConnectionError("test-only network outage")

    monkeypatch.setattr(ModelStore, "download_snapshot", fail_download)
    with pytest.raises(AppError) as caught:
        preparation.prepare_model(settings)
    assert caught.value.code == "MODEL_DOWNLOAD_FAILED"
    assert before == {entry.name: entry.read_bytes() for entry in settings.asr_model_dir.iterdir()}


def test_failed_first_download_does_not_mark_model_ready(model_setup, monkeypatch):
    settings, _snapshot, _downloads = model_setup

    def fail_download(*_args):
        raise ConnectionError("test-only network outage")

    monkeypatch.setattr(ModelStore, "download_snapshot", fail_download)
    with pytest.raises(AppError) as caught:
        preparation.prepare_model(settings)
    assert caught.value.code == "MODEL_DOWNLOAD_FAILED"
    assert not (settings.asr_model_dir / "manifest.json").exists()


@pytest.mark.parametrize("damage", ["wrong_weights", "missing_file", "empty_file"])
def test_invalid_download_does_not_publish_ready_manifest(model_setup, damage):
    settings, snapshot, _downloads = model_setup
    if damage == "wrong_weights":
        (snapshot / "model.bin").write_bytes(b"partial download")
    elif damage == "missing_file":
        (snapshot / "tokenizer.json").unlink()
    else:
        (snapshot / "config.json").write_bytes(b"")
    with pytest.raises(AppError) as caught:
        preparation.prepare_model(settings)
    assert caught.value.code == "MODEL_INVALID"
    assert not (settings.asr_model_dir / "manifest.json").exists()
    assert not list(settings.asr_model_dir.parent.glob(".*-stage-*"))


def test_unknown_directory_contents_are_preserved_without_downloading(model_setup):
    settings, _snapshot, downloads = model_setup
    settings.asr_model_dir.mkdir(parents=True)
    unrelated = settings.asr_model_dir / "my-notes.txt"
    unrelated.write_text("user content", encoding="utf-8")
    with pytest.raises(AppError, match="独立的空目录"):
        preparation.prepare_model(settings)
    assert unrelated.read_text(encoding="utf-8") == "user content"
    assert not downloads


def test_repair_keeps_previous_incomplete_model_as_backup(model_setup):
    settings, _snapshot, _downloads = model_setup
    settings.asr_model_dir.mkdir(parents=True)
    (settings.asr_model_dir / "model.bin").write_bytes(b"old partial download")
    manifest = preparation.prepare_model(settings)
    backups = list(settings.asr_model_dir.parent.glob("whisper-small.backup-*"))
    assert len(backups) == 1
    assert (backups[0] / "model.bin").read_bytes() == b"old partial download"
    assert preparation.inspect_model(settings) == manifest


def test_failed_publish_restores_previous_directory(model_setup, monkeypatch):
    settings, _snapshot, _downloads = model_setup
    settings.asr_model_dir.mkdir(parents=True)
    (settings.asr_model_dir / "model.bin").write_bytes(b"old partial download")
    original_replace = Path.replace

    def fail_stage_replace(path, target):
        if path.name == "model" and Path(target) == settings.asr_model_dir:
            raise OSError("test-only publish failure")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_stage_replace)
    with pytest.raises(AppError) as caught:
        preparation.prepare_model(settings)
    assert caught.value.code == "MODEL_INVALID"
    assert (settings.asr_model_dir / "model.bin").read_bytes() == b"old partial download"
    assert not (settings.asr_model_dir / "manifest.json").exists()


def test_hf_download_uses_explicit_revision_project_cache_and_no_token(tmp_path, monkeypatch):
    import huggingface_hub

    captured = {}

    def fake_snapshot_download(**kwargs):
        captured.update(kwargs)
        return str(tmp_path / "fake-snapshot")

    monkeypatch.setattr(huggingface_hub, "snapshot_download", fake_snapshot_download)
    store = ModelStore(tmp_path / "models" / "whisper-small")
    store.download_snapshot(preparation.MODEL_REPO, preparation.MODEL_REVISION, preparation.REQUIRED_MODEL_FILES)
    assert captured == {
        "repo_id": preparation.MODEL_REPO,
        "revision": preparation.MODEL_REVISION,
        "allow_patterns": list(preparation.REQUIRED_MODEL_FILES),
        "cache_dir": str(tmp_path / "models" / ".huggingface-cache"),
        "token": False,
    }
