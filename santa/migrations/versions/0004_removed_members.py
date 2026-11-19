"""Remember participants removed by the host so an old invite cannot rejoin.

Revision ID: 0004_removed_members
Revises: 0003_delivery_queue_index
"""

from alembic import op
from sqlalchemy import BigInteger, Column, ForeignKey, Integer, String, UniqueConstraint, inspect

revision = "0004_removed_members"
down_revision = "0003_delivery_queue_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Revision 0001 calls Base.metadata.create_all for fresh installations.
    if not inspect(op.get_bind()).has_table("removed_members"):
        op.create_table(
            "removed_members",
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("game_id", Integer, ForeignKey("games.id", ondelete="CASCADE"), nullable=False),
            Column("user_id", BigInteger, ForeignKey("users.id"), nullable=False),
            Column("name", String(120), nullable=False),
            UniqueConstraint("game_id", "user_id"),
        )
    names = {item["name"] for item in inspect(op.get_bind()).get_indexes("removed_members")}
    if "ix_removed_members_game_id" not in names:
        op.create_index("ix_removed_members_game_id", "removed_members", ["game_id"])


def downgrade() -> None:
    op.drop_table("removed_members")
