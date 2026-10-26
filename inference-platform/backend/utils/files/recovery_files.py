"""离线快照的有界文件/SQLite工具；不推断任务终态或释放容量。"""

from contextlib import closing, contextmanager
import os
from pathlib import Path
import sqlite3
from time import monotonic
from backend.utils.files.source_archive import plain_path, read_bytes, sha256, write_exclusive, MAX_FILE_BYTES


def private_parents(path, root):
    for parent in reversed(path.parents):
        if parent==root or not parent.is_relative_to(root):continue
        if not parent.exists():
            parent.mkdir(mode=0o700)
        plain_path(parent)


def file_fact(path, name):
    content=read_bytes(path,MAX_FILE_BYTES)
    return {"path":name,"size":len(content),"sha256":sha256(content)}


def copy_verified(source, target, expected=None):
    content=read_bytes(source,MAX_FILE_BYTES)
    if expected and (len(content)!=expected.size or sha256(content)!=expected.sha256):
        raise ValueError("RECOVERY_CONTENT_CHANGED")
    write_exclusive(target,content)
    return {"size":len(content),"sha256":sha256(content)}


def checked_database_path(path):
    path=plain_path(Path(path))
    for suffix in ("-wal","-shm","-journal"):
        companion=plain_path(Path(str(path)+suffix),allow_missing=True)
        if companion.exists() and not companion.is_file():raise ValueError("RECOVERY_SQLITE_COMPANION_INVALID")
    return path


@contextmanager
def sqlite_write_barrier(path):
    path=checked_database_path(path)
    connection=sqlite3.connect(path.as_uri()+"?mode=rw",uri=True,timeout=0)
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield
    finally:
        connection.rollback()
        connection.close()


@contextmanager
def readonly_database(path, *, seconds=10):
    path=checked_database_path(path)
    if path.stat().st_size>MAX_FILE_BYTES:
        raise ValueError("RECOVERY_DATABASE_TOO_LARGE")
    end=monotonic()+seconds
    connection=sqlite3.connect(path.as_uri()+"?mode=ro",uri=True,timeout=0)
    connection.set_progress_handler(lambda: int(monotonic()>end),1000)
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("BEGIN")
        yield connection
    finally:
        connection.close()


def sqlite_snapshot(source, target):
    write_exclusive(target,b"")
    end=monotonic()+10
    def progress(status, remaining, total):
        if monotonic()>end:
            raise TimeoutError("RECOVERY_DATABASE_TIMEOUT")
    with readonly_database(source) as origin:
        with closing(sqlite3.connect(target.as_uri()+"?mode=rw",uri=True,timeout=0)) as destination:
            origin.backup(destination,pages=128,progress=progress,sleep=0.01)
            destination.execute("PRAGMA journal_mode=DELETE")
    return file_fact(target,target.name)


def sync_directory(path):
    if os.name=="posix":
        descriptor=os.open(plain_path(path),os.O_RDONLY|getattr(os,"O_DIRECTORY",0))
        try:os.fsync(descriptor)
        finally:os.close(descriptor)


def sync_tree(root):
    if os.name=="posix":
        directories=[path for path in root.rglob("*") if path.is_dir()]
        for directory in sorted(directories,key=lambda value:len(value.parts),reverse=True):sync_directory(directory)
    sync_directory(root)
