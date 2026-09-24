"""Run inside a temporary container with the stopped app's data mounted read-only.

Write only a gzip tar stream to stdout. The database snapshot includes committed WAL
transactions; uploads and database are copied during the same maintenance window.
"""
from contextlib import closing
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tarfile
import tempfile


def backup(root: Path, output) -> None:
    if not (root / "club.db").is_file():
        raise RuntimeError("找不到原有 club.db，停止更新，避免启动空数据库")
    with tempfile.TemporaryDirectory() as temp:
        copied = Path(temp) / "data"
        shutil.copytree(root, copied)
        snapshot = Path(temp) / "snapshot.db"
        with closing(sqlite3.connect(str(copied / "club.db"))) as source, closing(sqlite3.connect(str(snapshot))) as target:
            source.backup(target)
            if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("数据库完整性检查未通过")
        os.replace(snapshot, copied / "club.db")
        for suffix in ("-wal", "-shm"):
            (copied / ("club.db" + suffix)).unlink(missing_ok=True)
        with tarfile.open(fileobj=output, mode="w|gz") as archive:
            archive.add(copied, arcname="data")


if __name__ == "__main__":
    backup(Path("/app/data"), sys.stdout.buffer)
