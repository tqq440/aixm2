"""固定外部 ASR 调用池入口；每槽独立HTTP子进程，统一排空本机调用。"""

from backend.modules.worker.run_worker import run_worker


def main() -> int:
    return run_worker("asr", asr_profile="remote")


if __name__ == "__main__":
    raise SystemExit(main())
