"""先完整写入临时目录，再一次发布结果 JSON 与标注 PNG。"""

import os
from pathlib import Path
from tempfile import TemporaryDirectory


def write_detection_result(directory: Path, content: str, image: bytes) -> None:
    if directory.exists() or directory.is_symlink():
        raise FileExistsError(str(directory))
    directory.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=f".{directory.name}-", dir=directory.parent) as temporary:
        staged = Path(temporary)
        with (staged / "result.json").open("x", encoding="utf-8", newline="\n") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        with (staged / "annotated.png").open("xb") as output:
            output.write(image)
            output.flush()
            os.fsync(output.fileno())
        if directory.exists() or directory.is_symlink():
            raise FileExistsError(str(directory))
        # 本阶段目标 Windows 上 rename 不覆盖任何已存在的目标目录。
        staged.rename(directory)
