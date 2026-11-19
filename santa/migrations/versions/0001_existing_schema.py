"""Bridge installations created before Alembic, without dropping data.

Revision ID: 0001_existing_schema
Revises:
"""

from alembic import op
from sqlalchemy import inspect, text

from santa.models import Base

revision = "0001_existing_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    Base.metadata.create_all(connection)

    def add_missing(table: str, columns: tuple[tuple[str, str], ...]) -> None:
        present = {column["name"] for column in inspect(connection).get_columns(table)}
        for name, sql_type in columns:
            if name not in present:
                connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}"))

    add_missing("users", (("created_at", "TIMESTAMP"),))
    add_missing("games", (
        ("rules", "TEXT DEFAULT '' NOT NULL"),
        ("cover_file_id", "VARCHAR(255) DEFAULT '' NOT NULL"),
        ("registration_start", "TIMESTAMP"),
        ("draw_at", "TIMESTAMP"),
        ("wishes_deadline", "TIMESTAMP"),
        ("shipping_deadline", "TIMESTAMP"),
        ("max_members", "INTEGER"),
        ("approval_required", "BOOLEAN DEFAULT FALSE NOT NULL"),
        ("team_policy", "VARCHAR(12) DEFAULT 'mixed' NOT NULL"),
        ("advent_enabled", "BOOLEAN DEFAULT FALSE NOT NULL"),
    ))
    add_missing("members", (
        ("received", "BOOLEAN DEFAULT FALSE NOT NULL"),
        ("team", "VARCHAR(80) DEFAULT '' NOT NULL"),
        ("approved", "BOOLEAN DEFAULT TRUE NOT NULL"),
    ))
    add_missing("deliveries", (
        ("parse_mode", "VARCHAR(16) DEFAULT '' NOT NULL"),
        ("claimed_at", "TIMESTAMP"),
        ("media_type", "VARCHAR(12) DEFAULT '' NOT NULL"),
        ("media_file_id", "VARCHAR(255) DEFAULT '' NOT NULL"),
        ("button_text", "VARCHAR(64) DEFAULT '' NOT NULL"),
        ("button_url", "VARCHAR(500) DEFAULT '' NOT NULL"),
    ))
    add_missing("broadcasts", (
        ("media_type", "VARCHAR(12) DEFAULT '' NOT NULL"),
        ("media_file_id", "VARCHAR(255) DEFAULT '' NOT NULL"),
        ("button_text", "VARCHAR(64) DEFAULT '' NOT NULL"),
        ("button_url", "VARCHAR(500) DEFAULT '' NOT NULL"),
    ))
    add_missing("questions", (("reported", "BOOLEAN DEFAULT FALSE NOT NULL"),))


def downgrade() -> None:
    raise NotImplementedError("Schema downgrade would discard existing game data")
