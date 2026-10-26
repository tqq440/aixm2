"""新目录专属写入，不覆盖、跟随链接或递归清理。"""

import os
from pathlib import Path
from backend.utils.files.source_archive import plain_path


def new_directory(path):
    path=plain_path(Path(path),allow_missing=True)
    if not path.parent.is_dir():raise ValueError("Parent directory must exist")
    path.mkdir(mode=0o700)
    return plain_path(path)


def private_text(path,text):
    path=plain_path(Path(path),allow_missing=True)
    descriptor=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,"O_NOFOLLOW",0),0o600)
    with os.fdopen(descriptor,"w",encoding="utf-8",newline="\n") as stream:
        stream.write(text);stream.flush();os.fsync(stream.fileno())
