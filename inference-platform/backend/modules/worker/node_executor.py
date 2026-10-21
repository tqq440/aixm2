"""显式启动中心 Node 执行器；不迁移数据库、不启动业务容器。"""

import argparse
import signal
from pathlib import Path
from threading import Event

from filelock import FileLock,Timeout

from backend.config.settings.app_settings import load_settings
from backend.common.logging.app_logging import configure_logging,close_logging
from backend.data.database.database import Database
from backend.service.queue.queue_service import QueueService
from backend.service.host_node.input_service import CentralMaterialSource
from backend.service.invocations.node_bridge import NodeInvocationBridge
from backend.service.invocations.node_runner import NodeExecutorRunner


def main(argv=None):
    parser=argparse.ArgumentParser(description="中心 Node 执行器，仅显式开启公共执行后运行。")
    parser.add_argument("--env-file",type=Path,required=True)
    parser.add_argument("--workers",type=int,choices=range(1,9),default=4)
    parser.add_argument("--once",action="store_true")
    args=parser.parse_args(argv);stop=Event();previous={}
    logger=database=queue=runner=None;lock=None
    try:
        if not args.env_file.is_file():raise ValueError()
        settings=load_settings(args.env_file,include_service_selection=False)
        if not settings.common_execution_enabled or not settings.database_path.is_file():raise ValueError()
        lock=FileLock(str(settings.database_path)+".node-executor.lock",thread_local=False,timeout=0);lock.acquire()
        logger=configure_logging(settings.log_dir,settings.log_level,"node-executor.log",process_scoped=True)
        database=Database(settings);queue=QueueService(database,settings,logger=logger)
        from sqlalchemy import text
        with database.engine.connect() as connection:
            if connection.scalar(text("SELECT version_num FROM alembic_version")) not in {"20261021_0019","20261021_0020"}:raise ValueError()
        bridge=NodeInvocationBridge(queue.core,material_source=CentralMaterialSource(queue.core))
        runner=NodeExecutorRunner(queue,bridge,workers=args.workers)
        for name in ("SIGINT","SIGTERM","SIGBREAK"):
            number=getattr(signal,name,None)
            if number is not None:previous[number]=signal.signal(number,lambda *_:stop.set())
        while not stop.is_set():
            try:runner.tick()
            except Exception:
                logger.error("NODE_EXECUTOR_TICK_UNAVAILABLE")
                if args.once:return 2
                stop.wait(5)
            if args.once:break
            stop.wait(0.25)
        return 0
    except Exception:
        print("Node 执行器未能启动，请检查显式配置、迁移、独立运行锁和日志。")
        return 2
    finally:
        if runner:runner.close()
        if queue:queue.close()
        if database:database.dispose()
        if logger:close_logging(logger)
        if lock:lock.release()
        for number,handler in previous.items():signal.signal(number,handler)


if __name__=="__main__":raise SystemExit(main())
