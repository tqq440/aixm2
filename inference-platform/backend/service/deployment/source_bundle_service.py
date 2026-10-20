"""实验室首次源码交付白名单；不读取配置、运行数据或凭据。"""

from pathlib import Path
from uuid import uuid4

from backend.utils.files.source_archive import (
    MAX_ARCHIVE_BYTES, MAX_FILE_BYTES, archive_path, canonical_json, create_archive, plain_path,
    read_bytes, regular_files, rename_directory_no_replace, sha256, write_exclusive,
)


SOURCE_TREES = ("backend", "tests", "migrations", "requirements", "scripts", "docs", "frontend/src", "frontend/e2e", "frontend/public")
ROOT_FILES = frozenset({"AGENTS.md", "README.md", "alembic.ini", "pytest.ini", "env.example", ".gitignore"})
FRONTEND_FILES = frozenset({"package.json", "package-lock.json", "tsconfig.json", "vite.config.ts", "playwright.config.ts", "index.html", "env.example"})
REFERENCE_FILES = frozenset({"AGENTS.md", "后端开发规范.md", "后端开发规范-项目补充约定.md"})
EXCLUDED_NAMES = frozenset({"runtime", "node_modules", "dist", "build", ".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".cache", ".ssh", ".aixm-incoming"})
SOURCE_SUFFIXES = frozenset({".py", ".pyi", ".md", ".json", ".toml", ".ini", ".txt", ".yaml", ".yml", ".ts", ".tsx", ".js", ".mjs", ".cjs", ".css", ".html", ".svg", ".png", ".jpg", ".jpeg", ".webp", ".ico", ".ps1", ".sh", ".mako"})


def source_path_allowed(value: str) -> bool:
    """收发两端执行同一白名单，任意新目录不会自动进入交付。"""
    archive_path(value)
    if not value.startswith("inference-platform/"):
        return False
    relative = value[len("inference-platform/"):]
    parts = relative.split("/")
    lowered = [part.casefold() for part in parts]
    if any(part in EXCLUDED_NAMES or part.startswith(".env") or part in {"id_rsa", "id_ed25519", "id_ecdsa", "credentials.json", "secrets.json", "private_key", "private-key"} for part in lowered):
        return False
    if relative in ROOT_FILES:
        return True
    if len(parts) == 2 and parts[0] == "frontend" and parts[1] in FRONTEND_FILES:
        return True
    if len(parts) == 2 and parts[0] == "reference-docs" and parts[1] in REFERENCE_FILES:
        return True
    if not any(relative.startswith(tree + "/") for tree in SOURCE_TREES):
        return False
    return Path(parts[-1]).suffix.casefold() in SOURCE_SUFFIXES and not any(part.startswith(".") for part in parts)


def build_bundle(project_root: Path, output_root: Path) -> dict:
    root = plain_path(project_root)
    if not root.is_dir() or not (root / "backend").is_dir() or not (root / "frontend").is_dir():
        raise ValueError("Expected the inference-platform source root")
    output = plain_path(output_root, allow_missing=True)
    if output == root or (output.is_relative_to(root) and output.relative_to(root).parts[0] != "runtime"):
        raise ValueError("Bundle output must not overlap included source paths")
    files = {}
    for tree in SOURCE_TREES:
        directory = root / tree
        if directory.exists() or directory.is_symlink():
            for path in regular_files(directory, excluded_names=EXCLUDED_NAMES):
                name = "inference-platform/" + path.relative_to(root).as_posix()
                if source_path_allowed(name):
                    files[name] = path
    for relative in (*sorted(ROOT_FILES), *("frontend/" + name for name in sorted(FRONTEND_FILES))):
        path = root / relative
        if path.exists() or path.is_symlink():
            files["inference-platform/" + relative] = plain_path(path)
    references = {"AGENTS.md": root.parent / "AGENTS.md", "后端开发规范.md": root.parent / "后端开发规范.md", "后端开发规范-项目补充约定.md": root.parent / "docs/后端开发规范-项目补充约定.md"}
    output.mkdir(parents=True, exist_ok=True)
    plain_path(output)
    staging = output / (".building-" + uuid4().hex)
    staging.mkdir(mode=0o700)
    snapshots = staging / "reference-snapshots"
    snapshots.mkdir(mode=0o700)
    for name, path in references.items():
        if path.exists() or path.is_symlink():
            # 根规范可能与历史快照硬链接；只读稳定内容再写独立普通文件。
            # 例外仅限上述三个固定参考路径，业务源码和接收端不采用它。
            content = read_bytes(path, MAX_FILE_BYTES, allow_hardlinks=True)
            snapshot = snapshots / name
            write_exclusive(snapshot, content)
            files["inference-platform/reference-docs/" + name] = snapshot
    archive = staging / "source.zip"
    manifest = create_archive(files, archive)
    for snapshot in snapshots.iterdir():
        plain_path(snapshot).unlink()
    snapshots.rmdir()
    manifest_bytes = canonical_json(manifest)
    write_exclusive(staging / "manifest.json", manifest_bytes)
    bundle_id = sha256(read_bytes(archive, MAX_ARCHIVE_BYTES))
    destination = output / bundle_id
    if destination.exists() or destination.is_symlink():
        plain_path(destination)
        if set(path.name for path in destination.iterdir()) != {"source.zip", "manifest.json"} or read_bytes(destination / "source.zip", MAX_ARCHIVE_BYTES) != read_bytes(archive, MAX_ARCHIVE_BYTES) or read_bytes(destination / "manifest.json", len(manifest_bytes)) != manifest_bytes:
            raise ValueError("Existing bundle differs; refusing overwrite")
        # 仅清理本次刚创建的两个文件和空目录，不递归处理用户路径。
        archive.unlink()
        (staging / "manifest.json").unlink()
        staging.rmdir()
    else:
        rename_directory_no_replace(staging, destination)
    return {"bundleId": bundle_id, "archivePath": str(destination / "source.zip"), "manifestPath": str(destination / "manifest.json"), "manifestSha256": sha256(manifest_bytes), "fileCount": len(manifest["files"]), "totalBytes": sum(entry["size"] for entry in manifest["files"])}
