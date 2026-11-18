"""Add the internal starting topic for open interviews."""

from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

topic = sa.table("topics", sa.column("id", sa.String), sa.column("profession_id", sa.String),
                 sa.column("title", sa.String), sa.column("source_ids", sa.JSON),
                 sa.column("opening_question", sa.Text))
OPEN_ID = "open-interview"


def upgrade():
    if op.get_bind().scalar(sa.select(topic.c.id).where(topic.c.id == OPEN_ID)):
        return
    op.bulk_insert(topic, [{
        "id": OPEN_ID, "profession_id": "python_backend",
        "title": "Рабочий случай", "source_ids": [],
        "opening_question": "Вспомните один конкретный случай из вашей работы. Что произошло и какую задачу нужно было решить?",
    }])


def downgrade():
    # Preserve the topic when existing sessions still reference it.
    sessions = sa.table("interview_sessions", sa.column("topic_id", sa.String))
    op.execute(topic.delete().where(topic.c.id == OPEN_ID).where(
        ~sa.exists(sa.select(sessions.c.topic_id).where(sessions.c.topic_id == OPEN_ID))
    ))
