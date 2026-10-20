"""ASR Worker 兼容入口；每数据库ASR队列保留一个执行槽位。"""

from backend.modules.worker.run_worker import run_worker


def main() -> int:
    return run_worker("asr")


if __name__ == "__main__":
    raise SystemExit(main())
