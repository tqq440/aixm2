"""保存本地验证结果，不创建任务档案；既有文件绝不覆盖。"""

from pathlib import Path


def write_result(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # x 模式原子地拒绝已存在的路径，包括并发产生的同名文件。
    stream = path.open("x", encoding="utf-8", newline="\n")
    try:
        with stream:
            stream.write(content)
            stream.flush()
    except BaseException:
        # 包含退出 with 时的 close/flush 失败；只清理本次 x 模式创建的文件。
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass  # 保留原始写入异常，不能将保存失败误报为成功。
        raise
