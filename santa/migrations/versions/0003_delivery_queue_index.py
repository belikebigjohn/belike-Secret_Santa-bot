"""Index the worker's delivery claim query.

Revision ID: 0003_delivery_queue_index
Revises: 0002_retire_game_messages
"""

from alembic import op
from sqlalchemy import inspect

revision = "0003_delivery_queue_index"
down_revision = "0002_retire_game_messages"
branch_labels = None
depends_on = None


def upgrade() -> None:
    names = {item["name"] for item in inspect(op.get_bind()).get_indexes("deliveries")}
    if "ix_deliveries_state_due" not in names:
        op.create_index("ix_deliveries_state_due", "deliveries", ["state", "due_at"])


def downgrade() -> None:
    op.drop_index("ix_deliveries_state_due", table_name="deliveries")
