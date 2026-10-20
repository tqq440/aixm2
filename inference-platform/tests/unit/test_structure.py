import ast
import os
import subprocess
import sys

from backend.config.settings.app_settings import PROJECT_ROOT


def test_backend_uses_three_level_modules_and_absolute_imports():
    backend = PROJECT_ROOT / "backend"
    source_files = list(backend.rglob("*.py"))
    assert source_files
    for path in source_files:
        if path.name != "__init__.py":
            assert len(path.relative_to(backend).parts) == 3, str(path)
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.level == 0, f"Relative import in {path}:{node.lineno}"
                imported = node.module or ""
                layer = path.relative_to(backend).parts[0]
                if layer == "data":
                    assert not imported.startswith("backend.service"), str(path)
                if layer == "utils" and imported.startswith("backend."):
                    assert imported.startswith("backend.utils."), str(path)


def test_importing_all_backend_modules_does_not_read_invalid_environment_or_create_files(tmp_path):
    modules = [
        ".".join(path.relative_to(PROJECT_ROOT).with_suffix("").parts)
        for path in (PROJECT_ROOT / "backend").rglob("*.py")
        if path.name != "__init__.py"
    ]
    environment = {key: value for key, value in os.environ.items() if not key.startswith("INFERENCE_")}
    environment.update({"PYTHONPATH": str(PROJECT_ROOT), "INFERENCE_PORT": "invalid-before-startup", "PYTHONDONTWRITEBYTECODE": "1"})
    result = subprocess.run(
        [sys.executable, "-c", f"import importlib; [importlib.import_module(name) for name in {modules!r}]"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert list(tmp_path.iterdir()) == []
