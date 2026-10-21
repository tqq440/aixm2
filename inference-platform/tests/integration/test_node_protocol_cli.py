"""执行器显式配置与单进程锁，仅隔离资料。"""

import os

from filelock import FileLock

from backend.modules.worker.node_executor import main
from tests.integration.test_common_execution import case


def configuration(case,tmp_path,monkeypatch,*,enabled=True,path=None):
    for key in os.environ:
        if key.startswith("INFERENCE_"):monkeypatch.delenv(key)
    env=tmp_path/"executor.env"
    env.write_text("\n".join((
        "INFERENCE_COMMON_EXECUTION_ENABLED="+str(enabled).lower(),
        "INFERENCE_DATABASE_PATH="+str(path or case.settings.database_path),
        "INFERENCE_LOG_DIR="+str(case.settings.log_dir),
        "INFERENCE_UPLOAD_DIR="+str(case.settings.upload_dir),
    )),encoding="utf-8")
    return ["--env-file",str(env),"--once"]


def test_explicit_executor_once_keeps_existing_tasks(case,tmp_path,monkeypatch):
    assert main(configuration(case,tmp_path,monkeypatch))==0


def test_executor_disabled_or_missing_database_cannot_bootstrap_daily_data(case,tmp_path,monkeypatch):
    assert main(configuration(case,tmp_path,monkeypatch,enabled=False))==2
    missing=tmp_path/"missing.sqlite3"
    assert main(configuration(case,tmp_path,monkeypatch,path=missing))==2
    assert not missing.exists()


def test_second_executor_is_blocked_by_process_lock(case,tmp_path,monkeypatch):
    args=configuration(case,tmp_path,monkeypatch)
    with FileLock(str(case.settings.database_path)+".node-executor.lock"):
        assert main(args)==2
