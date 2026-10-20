"""Vision Worker 入口；只运行视觉队列的独立单槽生命周期。"""

from backend.modules.worker.run_worker import run_worker


def main() -> int:
    return run_worker("vision")


if __name__ == "__main__":
    raise SystemExit(main())
