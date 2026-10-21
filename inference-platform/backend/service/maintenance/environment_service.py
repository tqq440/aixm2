"""本机版本的只读运行条件检查；不安装、迁移或下载任何内容。"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

from backend.common.exceptions.app_error import AppError
from backend.config.settings.app_settings import AppSettings, PROJECT_ROOT
from backend.data.maintenance.environment_probe import database_revisions, dependency_mismatches, frontend_mismatches
from backend.schema.maintenance.environment_schema import EnvironmentCheck, EnvironmentReport
from backend.service.asr.model_preparation_service import inspect_model
from backend.service.vision.model_preparation_service import inspect_model as inspect_vision_model


def check_environment(settings: AppSettings, *, node_path: Path | None = None) -> EnvironmentReport:
    checks: list[EnvironmentCheck] = []

    def add(name: str, passed: bool, message: str) -> None:
        checks.append(EnvironmentCheck(name=name, passed=passed, message=message))

    add("windows", os.name == "nt", "Worker 进程管理要求 Windows。")
    add("python", (3, 10) <= sys.version_info[:2] < (3, 12), f"当前 Python {sys.version.split()[0]}；要求 3.10 或 3.11。")
    try:
        mismatches = dependency_mismatches(PROJECT_ROOT / "requirements/vision.txt")
        add("pythonDependencies", not mismatches, "; ".join(mismatches) or "运行时依赖与 requirements/vision.txt 及其引用清单一致。")
    except (OSError, ValueError):
        add("pythonDependencies", False, "无法读取或解析运行时依赖清单。")
    try:
        current, expected = database_revisions(settings.database_path, PROJECT_ROOT)
        add("database", current == expected, f"当前迁移 {', '.join(sorted(current)) or '空'}；要求 {', '.join(sorted(expected))}。")
    except Exception:
        add("database", False, "现有 SQLite 数据库或迁移记录不可读；请先检查路径并执行 Alembic upgrade head。")
    if settings.asr_service_id:
        add("modelFiles", True, "ASR 使用手工配置的接口；平台启动不要求本机 Whisper 权重。")
    else:
        try:
            inspect_model(settings)
            add("modelFiles", True, "固定版本 Whisper small 的本地清单、文件大小与 SHA-256 校验通过；未加载推理模型。")
        except AppError as exc:
            add("modelFiles", False, f"{exc.code}: {exc.message}")
        except (OSError, ValueError):
            add("modelFiles", False, "本地模型文件不可读或清单损坏。")
    try:
        inspect_vision_model(settings)
        add("visionModelFiles", True, "固定版本 YOLOv5n 的本地清单、大小与 SHA-256 校验通过；未加载推理模型。")
    except AppError as exc:
        add("visionModelFiles", False, f"{exc.code}: {exc.message}")
    except (OSError, ValueError):
        add("visionModelFiles", False, "本地视觉模型文件不可读或清单损坏。")
    node = str(node_path) if node_path is not None else shutil.which("node")
    try:
        if not node:
            raise FileNotFoundError
        completed = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=10,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                                   check=True)
        version_text = completed.stdout.strip()
        version = tuple(int(item) for item in version_text.removeprefix("v").split("."))
        compatible = len(version) == 3 and ((24, 15, 0) <= version < (25, 0, 0) or version >= (26, 0, 0))
        add("node", compatible, f"当前 Node {version_text}；要求 ^24.15.0 或 >=26.0.0（与前端 engines 一致）。")
    except (OSError, ValueError, subprocess.SubprocessError):
        add("node", False, "Node 无法执行或版本输出无效；请指定现有 node.exe 的完整路径。")
    try:
        mismatches = frontend_mismatches(PROJECT_ROOT / "frontend")
        add("frontendDependencies", not mismatches, "; ".join(mismatches) or "前端直接依赖版本与 package.json 一致。")
    except (OSError, ValueError, KeyError):
        add("frontendDependencies", False, "前端依赖清单不可读。")
    return EnvironmentReport(passed=all(item.passed for item in checks), checks=checks)
