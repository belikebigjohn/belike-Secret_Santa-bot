"""Create a consistent backup of a live SQLite database."""

import sqlite3
import sys
from contextlib import closing
from pathlib import Path


def backup(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise ValueError(f"База не найдена: {source}")
    if destination.exists():
        raise ValueError(f"Копия уже существует: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        destination.touch(exist_ok=False)
    except FileExistsError as exc:
        raise ValueError(f"Копия уже существует: {destination}") from exc
    try:
        with closing(sqlite3.connect(source)) as source_db, closing(
            sqlite3.connect(destination)
        ) as destination_db:
            source_db.backup(destination_db)
            if destination_db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Проверка целостности копии не пройдена")
    except Exception:
        destination.unlink(missing_ok=True)
        raise


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("Использование: python scripts/backup_sqlite.py ИСТОЧНИК КОПИЯ")
    try:
        backup(Path(sys.argv[1]), Path(sys.argv[2]))
    except (OSError, sqlite3.Error, ValueError, RuntimeError) as exc:
        raise SystemExit(str(exc)) from exc
    print(f"Копия создана: {sys.argv[2]}")
