"""读取已安装依赖、项目清单和现有数据库，绝不创建数据库。"""

import json
import sqlite3
from importlib import metadata
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from packaging.requirements import Requirement


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def dependency_mismatches(requirements_file: Path) -> list[str]:
    mismatches = []
    for line in requirements_file.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-r "):
            mismatches.extend(dependency_mismatches(requirements_file.parent / line[3:].strip()))
            continue
        requirement = Requirement(line)
        if requirement.marker is not None and not requirement.marker.evaluate():
            continue
        try:
            actual = metadata.version(requirement.name)
        except metadata.PackageNotFoundError:
            mismatches.append(f"{requirement.name}: 未安装")
        else:
            if actual not in requirement.specifier:
                mismatches.append(f"{requirement.name}: {actual}，要求 {requirement.specifier}")
    return mismatches


def database_revisions(path: Path, project_root: Path) -> tuple[set[str], set[str]]:
    # mode=ro 在路径缺失时失败，避免检查操作悄悄生成空数据库。
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5) as connection:
        connection.execute("SELECT 1").fetchone()
        current = {row[0] for row in connection.execute("SELECT version_num FROM alembic_version")}
    config = Config()
    config.set_main_option("script_location", str(project_root / "migrations").replace("%", "%%"))
    return current, set(ScriptDirectory.from_config(config).get_heads())


def frontend_mismatches(frontend: Path) -> list[str]:
    package = read_json(frontend / "package.json")
    expected = {**package.get("dependencies", {}), **package.get("devDependencies", {})}
    mismatches = []
    for name, version in expected.items():
        try:
            actual = read_json(frontend / "node_modules" / name / "package.json")["version"]
        except (OSError, ValueError, KeyError):
            mismatches.append(f"{name}: 未安装或清单不可读")
            continue
        if actual != version:
            mismatches.append(f"{name}: {actual}，要求 {version}")
    return mismatches
