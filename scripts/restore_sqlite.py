"""Restore a SQLite backup after first preserving the current database."""

import sqlite3
import sys
from contextlib import closing
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from backup_sqlite import backup


def restore(source: Path, destination: Path) -> Path | None:
    source = source.resolve()
    destination = destination.resolve()
    if not source.is_file():
        raise ValueError(f"Резервная копия не найдена: {source}")
    if source == destination:
        raise ValueError("Копия и рабочая база должны быть разными файлами")

    with closing(sqlite3.connect(source)) as source_db:
        if source_db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Резервная копия повреждена")

    safety_copy = None
    if destination.is_file():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        safety_copy = destination.with_name(
            f"{destination.stem}.before-restore-{stamp}-{uuid4().hex[:8]}.db"
        )
        backup(destination, safety_copy)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(source)) as source_db, closing(
        sqlite3.connect(destination)
    ) as destination_db:
        source_db.backup(destination_db)
        if destination_db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("Восстановленная база не прошла проверку целостности")
    return safety_copy


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("Использование: python scripts/restore_sqlite.py КОПИЯ РАБОЧАЯ_БАЗА")
    try:
        safety_copy = restore(Path(sys.argv[1]), Path(sys.argv[2]))
    except (OSError, sqlite3.Error, ValueError, RuntimeError) as exc:
        raise SystemExit(str(exc)) from exc
    if safety_copy:
        print(f"Прежняя рабочая база сохранена: {safety_copy}")
    print(f"База восстановлена из копии: {sys.argv[1]}")
