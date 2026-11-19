"""Stop queued game announcements and reminders after feature removal.

Revision ID: 0002_retire_game_messages
Revises: 0001_existing_schema
"""

from alembic import op
from sqlalchemy import text

revision = "0002_retire_game_messages"
down_revision = "0001_existing_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("""
        UPDATE deliveries SET state = 'cancelled', claimed_at = NULL
        WHERE state IN ('pending', 'sending')
          AND (key LIKE 'announcement:%' OR key LIKE 'reminder:%' OR key LIKE 'daily:%')
    """))
    # Old segmented campaigns no longer match the sole all-users audience.
    old_campaigns = connection.execute(text(
        "SELECT id FROM broadcasts WHERE segment <> 'all' AND state <> 'cancelled'"
    )).scalars().all()
    for campaign_id in old_campaigns:
        connection.execute(text("""
            UPDATE deliveries SET state = 'cancelled', claimed_at = NULL
            WHERE state IN ('pending', 'sending') AND key LIKE :prefix
        """), {"prefix": f"broadcast:{campaign_id}:%"})
        connection.execute(text("UPDATE broadcasts SET state = 'cancelled' WHERE id = :id"),
                           {"id": campaign_id})
    connection.execute(text("""
        UPDATE deliveries
        SET text = replace(text, '\n\nОтключить новости: /start → Мои напоминания.', '')
        WHERE key LIKE 'broadcast:%' AND state = 'pending'
    """))


def downgrade() -> None:
    raise NotImplementedError("Cancelled messages must not be restored automatically")
