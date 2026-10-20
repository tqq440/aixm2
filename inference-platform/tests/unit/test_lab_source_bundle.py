"""源码交付仅操作临时目录，不读取日常配置、数据库或网络。"""

import json
from pathlib import Path
import zipfile

import pytest

from backend.service.deployment.source_bundle_service import build_bundle
from backend.utils.files import source_archive


@pytest.fixture
def source_project(tmp_path):
    root = tmp_path / "source" / "inference-platform"
    for name, content in {"backend/__init__.py": "", "backend/service/demo/main.py": "print('demo')\n", "frontend/package.json": "{}", "frontend/src/main.tsx": "export {};\n", "frontend/public/favicon.svg": "<svg/>", "migrations/env.py": "# migration", "requirements/base.txt": "Flask==3.1.2\n", "README.md": "source", "env.example": "INFERENCE_PORT=5000\n", ".gitignore": "runtime/\n", "docs/开发计划.md": "计划"}.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


def test_bundle_is_deterministic_and_contains_explicit_reference_documents(source_project, tmp_path):
    (source_project.parent / "后端开发规范.md").write_text("规范", encoding="utf-8")
    first = build_bundle(source_project, tmp_path / "packages")
    second = build_bundle(source_project, tmp_path / "packages")
    assert first == second
    manifest = json.loads(Path(first["manifestPath"]).read_text(encoding="utf-8"))
    assert first["fileCount"] == len(manifest["files"])
    assert first["bundleId"] == source_archive.sha256(Path(first["archivePath"]).read_bytes())
    assert first["manifestSha256"] == source_archive.sha256(Path(first["manifestPath"]).read_bytes())
    with zipfile.ZipFile(first["archivePath"]) as archive:
        assert set(archive.namelist()) == {entry["path"] for entry in manifest["files"]}
        assert archive.read("inference-platform/reference-docs/后端开发规范.md").decode() == "规范"
    assert list((tmp_path / "packages").iterdir()) == [Path(first["archivePath"]).parent]


def test_bundle_excludes_credentials_runtime_models_and_dependencies_without_reading_them(source_project, tmp_path, monkeypatch):
    excluded = [".env", "frontend/.env.local", "backend/service/demo/secret.pem", "docs/credentials.json", "docs/.ssh/id_ed25519", "runtime/db/platform.sqlite3", "runtime/uploads/objects/x.audio", "runtime/models/weights.bin", "frontend/node_modules/a/index.js", "backend/__pycache__/x.pyc", "frontend/dist/index.html"]
    for relative in excluded:
        path = source_project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"private-sentinel")
    original_read = source_archive.read_bytes

    def guarded_read(path, limit):
        assert path not in [source_project / relative for relative in excluded]
        return original_read(path, limit)

    monkeypatch.setattr(source_archive, "read_bytes", guarded_read)
    result = build_bundle(source_project, source_project / "runtime/verification/source-transfer")
    with zipfile.ZipFile(result["archivePath"]) as archive:
        assert all(b"private-sentinel" not in archive.read(name) for name in archive.namelist())


@pytest.mark.parametrize("subpath", ["", "docs/packages", "backend/packages"])
def test_output_cannot_overlap_source(source_project, subpath):
    with pytest.raises(ValueError, match="overlap"):
        build_bundle(source_project, source_project / subpath)


def test_changed_source_makes_new_immutable_bundle(source_project, tmp_path):
    before = build_bundle(source_project, tmp_path / "packages")
    original = Path(before["archivePath"]).read_bytes()
    (source_project / "README.md").write_text("changed", encoding="utf-8")
    after = build_bundle(source_project, tmp_path / "packages")
    assert before["bundleId"] != after["bundleId"]
    assert Path(before["archivePath"]).read_bytes() == original


def test_existing_bundle_is_never_overwritten(source_project, tmp_path):
    result = build_bundle(source_project, tmp_path / "packages")
    path = Path(result["manifestPath"])
    path.write_text("user content", encoding="utf-8")
    with pytest.raises(ValueError, match="refusing overwrite"):
        build_bundle(source_project, tmp_path / "packages")
    assert path.read_text() == "user content"


def test_source_hardlink_is_rejected(source_project, tmp_path):
    target = source_project / "backend/copied.py"
    try:
        target.hardlink_to(source_project / "backend/__init__.py")
    except OSError:
        pytest.skip("Temporary filesystem does not support hard links")
    with pytest.raises(ValueError, match="Hard-linked"):
        build_bundle(source_project, tmp_path / "packages")


def test_source_file_size_is_bounded(source_project, tmp_path, monkeypatch):
    monkeypatch.setattr(source_archive, "MAX_FILE_BYTES", 4)
    with pytest.raises(ValueError, match="size limit"):
        build_bundle(source_project, tmp_path / "packages")


def test_invalid_source_root_does_not_create_output(tmp_path):
    root = tmp_path / "empty"
    root.mkdir()
    with pytest.raises(ValueError, match="source root"):
        build_bundle(root, tmp_path / "packages")
    assert not (tmp_path / "packages").exists()


def test_regular_file_scan_rejects_simulated_windows_reparse_point(source_project, monkeypatch):
    original = Path.lstat
    target = source_project / "README.md"

    def lstat(path, *args, **kwargs):
        actual = original(path, *args, **kwargs)
        if path != target:
            return actual
        from types import SimpleNamespace
        return SimpleNamespace(st_mode=actual.st_mode, st_nlink=actual.st_nlink, st_file_attributes=0x400)

    monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(ValueError, match="Linked paths"):
        source_archive.plain_path(target)


def test_atomic_commit_never_replaces_existing_empty_directory(tmp_path):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    before = destination.stat().st_ino
    with pytest.raises(FileExistsError):
        source_archive.rename_directory_no_replace(source, destination)
    assert source.is_dir() and destination.stat().st_ino == before


def test_explicit_reference_hardlink_is_snapshotted_without_changing_original(source_project, tmp_path):
    original = tmp_path / "historic-standard.md"
    original.write_text("保留的规范", encoding="utf-8")
    reference = source_project.parent / "后端开发规范.md"
    try:
        reference.hardlink_to(original)
    except OSError:
        pytest.skip("Temporary filesystem does not support hard links")
    before = reference.stat()
    result = build_bundle(source_project, tmp_path / "packages")
    with zipfile.ZipFile(result["archivePath"]) as archive:
        assert archive.read("inference-platform/reference-docs/后端开发规范.md").decode() == "保留的规范"
    assert reference.stat().st_ino == before.st_ino
    assert reference.stat().st_nlink == before.st_nlink
    assert original.read_text(encoding="utf-8") == "保留的规范"
    assert {path.name for path in Path(result["archivePath"]).parent.iterdir()} == {"source.zip", "manifest.json"}
