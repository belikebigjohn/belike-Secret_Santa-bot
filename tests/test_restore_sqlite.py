import sqlite3
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "restore_sqlite.py"


def test_restore_preserves_previous_database(tmp_path):
    source = tmp_path / "backup.db"
    current = tmp_path / "santa.db"
    for path, value in ((source, "old"), (current, "new")):
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE gifts (name TEXT)")
            connection.execute("INSERT INTO gifts VALUES (?)", (value,))

    result = subprocess.run([sys.executable, str(SCRIPT), str(source), str(current)],
                            capture_output=True, text=True, check=True)

    assert "База восстановлена" in result.stdout
    with sqlite3.connect(current) as connection:
        assert connection.execute("SELECT name FROM gifts").fetchone() == ("old",)
    safety = list(tmp_path.glob("santa.before-restore-*.db"))
    assert len(safety) == 1
    with sqlite3.connect(safety[0]) as connection:
        assert connection.execute("SELECT name FROM gifts").fetchone() == ("new",)


def test_restore_rejects_corrupt_backup_without_changing_current(tmp_path):
    source = tmp_path / "corrupt.db"
    current = tmp_path / "santa.db"
    source.write_text("not a database", encoding="utf-8")
    with sqlite3.connect(current) as connection:
        connection.execute("CREATE TABLE gifts (name TEXT)")
        connection.execute("INSERT INTO gifts VALUES ('current')")

    result = subprocess.run([sys.executable, str(SCRIPT), str(source), str(current)],
                            check=False, capture_output=True, text=True)

    assert result.returncode != 0
    with sqlite3.connect(current) as connection:
        assert connection.execute("SELECT name FROM gifts").fetchone() == ("current",)
