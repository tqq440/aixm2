"""一次同步的本地文件转写；不启动 API、任务队列或 Worker。"""

import argparse
from uuid import uuid4

from backend.common.cli.execution import execute
from backend.config.settings.app_settings import PROJECT_ROOT
from backend.schema.asr.asr_schema import TranscriptionOptions, TranscriptionRequest
from backend.service.asr.local_transcription_service import transcribe_to_file


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="使用本地 Whisper small 在 CPU 上转写音频；不联网下载模型。")
    parser.add_argument("audio_path", help="本地音频文件路径；相对路径以当前工作目录为准")
    parser.add_argument("--language", default="zh", help="语言码，如 zh/en；auto 表示自动检测")
    parser.add_argument("--output", dest="output_path", help="新 JSON 文件路径；默认存入 runtime/results，不覆盖已有文件")
    args = parser.parse_args(argv)

    def operation(settings, logger):
        values = vars(args).copy()
        if values["language"] == "auto":
            values["language"] = None
        options = TranscriptionOptions.model_validate(values)
        output = options.output_path or PROJECT_ROOT / "runtime/results" / f"asr-{uuid4().hex}.json"
        request = TranscriptionRequest(audio_path=options.audio_path, language=options.language)
        logger.info("开始本地转写：%s", options.audio_path.name)
        result = transcribe_to_file(settings, request, output)
        logger.info("结果已保存：%s；模型加载 %.3fs；转写 %.3fs；总计 %.3fs", output, result.model_load_seconds, result.transcription_seconds, result.total_seconds)
        return result

    return execute(operation)


if __name__ == "__main__":
    raise SystemExit(main())
