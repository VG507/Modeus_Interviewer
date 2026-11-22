"""Include ML engineers in the initial vacancy catalogue, retaining existing profiles."""

from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        INSERT INTO vacancy_collections (profession_id, title, title_key, search_query, cards, stats)
        SELECT 'ml_engineer', 'ML-инженер', 'ml инженер',
               'NAME:("ML инженер" OR "ML engineer" OR "machine learning" OR "машинного обучения")', '[]', '{}'
        WHERE NOT EXISTS (
            SELECT 1 FROM vacancy_collections WHERE profession_id = 'ml_engineer'
               OR title_key IN ('ml инженер', 'ml engineer', 'machine learning engineer', 'инженер машинного обучения')
        )
    """)


def downgrade():
    # Profiles may already have samples, subscriptions and editorial decisions.
    pass
