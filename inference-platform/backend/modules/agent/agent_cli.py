"""本机维护身份下的固定 ASR 启停命令；不接受任意 PID 或命令行。"""

import argparse

from backend.common.cli.execution import execute
from backend.data.database.database import Database
from backend.service.agent.agent_command_service import AgentCommandService


def main(argv=None):
    parser = argparse.ArgumentParser(description='管理本机固定 ASR 实例；停止时等待当前音频收尾。')
    parser.add_argument('command', choices=('status', 'start', 'stop'))
    parser.add_argument('--wait-seconds', type=int, default=0, choices=range(0, 601), metavar='0..600',
                        help='等待目标状态的最长秒数，0表示只提交意图；超时不强制终止')
    args = parser.parse_args(argv)

    def operation(settings, logger):
        database = Database(settings)
        try:
            return AgentCommandService(database, settings).local_command(args.command, wait_seconds=args.wait_seconds)
        finally:
            database.dispose()

    return execute(operation, log_filename='agent-cli.log', command_name='本机 ASR 管理')


if __name__ == '__main__':
    raise SystemExit(main())
