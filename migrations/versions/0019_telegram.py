"""Link Telegram chats to participants and store one-time invites."""

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade():
    migration = op.get_context()
    if not migration.as_sql:
        tables = set(sa.inspect(op.get_bind()).get_table_names())
        # The standalone Telegram branch also used revision 0017. Alembic
        # skips the Hacker News migration on databases stamped by that branch.
        if {"telegram_accounts", "telegram_invites"} <= tables:
            if "hacker_news_items" not in tables:
                migration.script.get_revision("0017").module.upgrade()
            return

    op.create_table(
        "telegram_accounts",
        sa.Column("telegram_id", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False, unique=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "telegram_invites",
        sa.Column("code_hash", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_telegram_invites_user_id", "telegram_invites", ["user_id"])
    op.create_index("ix_telegram_invites_expires_at", "telegram_invites", ["expires_at"])


def downgrade():
    op.drop_index("ix_telegram_invites_expires_at", table_name="telegram_invites")
    op.drop_index("ix_telegram_invites_user_id", table_name="telegram_invites")
    op.drop_table("telegram_invites")
    op.drop_table("telegram_accounts")
