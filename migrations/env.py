from alembic import context
from backend.db import Base, database
from backend.settings import Settings


def include_object(obj, name, kind, reflected, compare_to):
    # Migration 0013 owns this retired worker's stored state.
    return not (kind == "table" and reflected and name == "arxiv_worker_slot")


def migrate(connection):
    context.configure(connection=connection, target_metadata=Base.metadata, render_as_batch=True,
                      include_object=include_object)
    with context.begin_transaction():
        context.run_migrations()


connection = context.config.attributes.get("connection")
if connection is not None:
    migrate(connection)
else:
    engine, _ = database(Settings().database_url)
    try:
        with engine.connect() as connection:
            migrate(connection)
    finally:
        engine.dispose()
