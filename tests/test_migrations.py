from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

MIGRATIONS = Path(__file__).resolve().parent.parent / "santa" / "migrations"


def migrate(connection):
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    config.attributes["connection"] = connection
    command.upgrade(config, "head")


def test_alembic_creates_fresh_schema_and_is_repeatable(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    with engine.begin() as connection:
        migrate(connection)
        migrate(connection)
        tables = set(inspect(connection).get_table_names())
        assert {"users", "games", "members", "assignments", "deliveries",
                "broadcasts", "alembic_version"} <= tables
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0001_existing_schema"


def test_alembic_upgrades_legacy_tables_without_losing_rows(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE users (id BIGINT PRIMARY KEY, name VARCHAR(120) NOT NULL, "
                                "marketing_opt_in BOOLEAN, reminders_opt_in BOOLEAN, blocked BOOLEAN)"))
        connection.execute(text("INSERT INTO users (id, name) VALUES (7, 'Legacy')"))
        connection.execute(text("CREATE TABLE deliveries (id INTEGER PRIMARY KEY, "
                                "key VARCHAR(160), user_id BIGINT, text TEXT, due_at TIMESTAMP, "
                                "state VARCHAR(20), attempts INTEGER, last_error VARCHAR(160))"))
        connection.execute(text("INSERT INTO deliveries (id, key, user_id, text, state) "
                                "VALUES (1, 'legacy-job', 7, 'Keep me', 'pending')"))
        migrate(connection)
        assert connection.scalar(text("SELECT name FROM users WHERE id=7")) == "Legacy"
        assert connection.scalar(text("SELECT text FROM deliveries WHERE id=1")) == "Keep me"
        columns = {column["name"] for column in inspect(connection).get_columns("deliveries")}
        assert {"claimed_at", "media_type", "button_url", "parse_mode"} <= columns
