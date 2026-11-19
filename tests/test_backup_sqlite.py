import sqlite3

import pytest

from scripts.backup_sqlite import backup


def test_backup_copies_live_database_without_overwriting(tmp_path):
    source = tmp_path / "santa.db"
    destination = tmp_path / "backups" / "santa.db"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE gifts (name TEXT)")
        connection.execute("INSERT INTO gifts VALUES ('подарок')")

    backup(source, destination)

    with sqlite3.connect(destination) as connection:
        assert connection.execute("SELECT name FROM gifts").fetchone() == ("подарок",)
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    with pytest.raises(ValueError, match="уже существует"):
        backup(source, destination)
