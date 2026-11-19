"""Store card consent, editorial decisions and revision history."""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("knowledge_cards", sa.Column("show_author", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column("knowledge_cards", sa.Column("author_name", sa.String(120)))
    op.add_column("knowledge_cards", sa.Column("first_submitted_at", sa.DateTime()))
    op.add_column("knowledge_cards", sa.Column("consent", sa.JSON()))
    op.add_column("knowledge_cards", sa.Column("review", sa.JSON()))
    op.create_table(
        "card_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("card_id", sa.String(36), sa.ForeignKey("knowledge_cards.id"), nullable=False),
        sa.Column("actor_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("action", sa.String(24), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.UniqueConstraint("card_id", "version", name="uq_card_event_version"),
    )


def downgrade():
    op.drop_table("card_events")
    with op.batch_alter_table("knowledge_cards") as batch:
        batch.drop_column("review")
        batch.drop_column("consent")
        batch.drop_column("first_submitted_at")
        batch.drop_column("author_name")
        batch.drop_column("show_author")
