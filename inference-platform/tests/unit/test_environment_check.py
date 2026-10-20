import json
import sqlite3
import subprocess
from importlib import metadata

from alembic.config import Config
from alembic.script import ScriptDirectory
import pytest

from backend.common.exceptions.app_error import AppError
from backend.config.settings.app_settings import AppSettings, PROJECT_ROOT
from backend.data.maintenance import environment_probe
from backend.service.maintenance import environment_service


def test_readonly_database_probe_does_not_create_missing_file(tmp_path):
    target = tmp_path / "missing.sqlite3"
    with pytest.raises(sqlite3.OperationalError):
        environment_probe.database_revisions(target, PROJECT_ROOT)
    assert not target.exists()


def test_database_probe_detects_old_revision_without_mutation(tmp_path):
    target = tmp_path / "old.sqlite3"
    with sqlite3.connect(target) as connection:
        connection.execute("CREATE TABLE alembic_version (version_num TEXT)")
        connection.execute("INSERT INTO alembic_version VALUES ('20261012_0001')")
    before = target.read_bytes()
    actual, expected = environment_probe.database_revisions(target, PROJECT_ROOT)
    assert actual == {"20261012_0001"}
    assert expected == set(ScriptDirectory.from_config(Config(str(PROJECT_ROOT / "alembic.ini"))).get_heads())
    assert target.read_bytes() == before


def test_dependency_probe_recurses_and_reports_missing_and_mismatch(tmp_path, monkeypatch):
    (tmp_path / "base.txt").write_text("example==1.0\n", encoding="utf-8")
    requirement_file = tmp_path / "runtime.txt"
    requirement_file.write_text("# comment\n-r base.txt\nmissing==2.0\n", encoding="utf-8")

    def version(name):
        if name == "missing":
            raise metadata.PackageNotFoundError(name)
        return "0.9"

    monkeypatch.setattr(environment_probe.metadata, "version", version)
    result = environment_probe.dependency_mismatches(requirement_file)
    assert len(result) == 2
    assert "0.9" in result[0] and "未安装" in result[1]


def test_frontend_probe_checks_scoped_packages_and_absent_dependencies(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"dependencies": {"@test/one": "1.0", "missing": "2.0"}}))
    installed = tmp_path / "node_modules/@test/one"
    installed.mkdir(parents=True)
    (installed / "package.json").write_text('{"version": "1.0"}')
    assert environment_probe.frontend_mismatches(tmp_path) == ["missing: 未安装或清单不可读"]


def test_service_reports_failures_and_continues_remaining_checks(tmp_path, monkeypatch):
    monkeypatch.setattr(environment_service, "dependency_mismatches", lambda _: [])
    monkeypatch.setattr(environment_service, "database_revisions", lambda *_: ({"old"}, {"new"}))
    monkeypatch.setattr(environment_service, "frontend_mismatches", lambda _: [])

    def unavailable(_):
        raise AppError("MODEL_NOT_READY", "请准备模型")

    monkeypatch.setattr(environment_service, "inspect_model", unavailable)
    monkeypatch.setattr(environment_service, "inspect_vision_model", unavailable)
    monkeypatch.setattr(environment_service.subprocess, "run", lambda *_, **__: subprocess.CompletedProcess([], 0, "v25.0.0\n", ""))
    result = environment_service.check_environment(AppSettings(database_path=tmp_path / "absent.sqlite3"))
    by_name = {item.name: item for item in result.checks}
    assert not result.passed
    assert not by_name["database"].passed
    assert not by_name["modelFiles"].passed
    assert not by_name["visionModelFiles"].passed
    assert not by_name["node"].passed
    assert by_name["frontendDependencies"].passed
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("version, compatible", [("v24.14.0", False), ("v24.15.0", True), ("v26.0.0", True), ("invalid", False)])
def test_node_version_matches_supported_frontend_range(tmp_path, monkeypatch, version, compatible):
    monkeypatch.setattr(environment_service, "dependency_mismatches", lambda _: [])
    monkeypatch.setattr(environment_service, "database_revisions", lambda *_: ({"head"}, {"head"}))
    monkeypatch.setattr(environment_service, "frontend_mismatches", lambda _: [])
    monkeypatch.setattr(environment_service, "inspect_model", lambda _: None)
    monkeypatch.setattr(environment_service, "inspect_vision_model", lambda _: None)
    monkeypatch.setattr(environment_service.subprocess, "run", lambda *_, **__: subprocess.CompletedProcess([], 0, version, ""))
    result = environment_service.check_environment(AppSettings(database_path=tmp_path / "absent.sqlite3"), node_path=tmp_path / "node.exe")
    assert next(item for item in result.checks if item.name == "node").passed == compatible


def test_platform_environment_checks_vision_dependency_closure_and_both_models(tmp_path, monkeypatch):
    observed = []
    monkeypatch.setattr(environment_service, "dependency_mismatches", lambda path: observed.append(path) or [])
    monkeypatch.setattr(environment_service, "database_revisions", lambda *_: ({"head"}, {"head"}))
    monkeypatch.setattr(environment_service, "frontend_mismatches", lambda _: [])
    monkeypatch.setattr(environment_service, "inspect_model", lambda _: observed.append("asr"))
    monkeypatch.setattr(environment_service, "inspect_vision_model", lambda _: observed.append("vision"))
    monkeypatch.setattr(environment_service.subprocess, "run", lambda *_, **__: subprocess.CompletedProcess([], 0, "v24.15.0", ""))
    report = environment_service.check_environment(AppSettings(database_path=tmp_path / "absent.sqlite3"))
    assert observed == [PROJECT_ROOT / "requirements" / "vision.txt", "asr", "vision"]
    assert report.passed
    assert list(tmp_path.iterdir()) == []
