from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from santa.models import Broadcast, Delivery, User, utcnow

MIGRATIONS = Path(__file__).resolve().parent.parent / "santa" / "migrations"


def migrate(connection, revision="head"):
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    config.attributes["connection"] = connection
    command.upgrade(config, revision)


def test_alembic_creates_fresh_schema_and_is_repeatable(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    with engine.begin() as connection:
        migrate(connection)
        migrate(connection)
        tables = set(inspect(connection).get_table_names())
        assert {"users", "games", "members", "assignments", "deliveries",
                "broadcasts", "alembic_version"} <= tables
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0002_retire_game_messages"


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


def test_removed_game_jobs_are_cancelled_on_upgrade(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'queued.db'}")
    with engine.begin() as connection:
        migrate(connection, "0001_existing_schema")
        with Session(connection) as db:
            db.add(User(id=7, name="Host"))
            db.flush()
            campaign = Broadcast(owner_id=7, text="Old segment", segment="hosts", due_at=utcnow())
            db.add(campaign)
            db.flush()
            db.add_all([
                Delivery(key="announcement:1:old:7", user_id=7, text="Old", state="pending"),
                Delivery(key="reminder:1:gift:7", user_id=7, text="Old", state="sending"),
                Delivery(key="daily:1:date:7", user_id=7, text="Old", state="pending"),
                Delivery(key=f"broadcast:{campaign.id}:7", user_id=7, text="Old segment"),
                Delivery(key="draw:1:7", user_id=7, text="Mission"),
            ])
            db.commit()
        migrate(connection)
        states = dict(connection.execute(text("SELECT key, state FROM deliveries")).all())
        assert states["draw:1:7"] == "pending"
        assert all(states[key] == "cancelled" for key in states if key != "draw:1:7")
