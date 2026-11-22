import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


@pytest.fixture(autouse=True)
def no_embedding_model(monkeypatch):
    """Tests never reach the embedding model from .env; a test that needs one configures and fakes it."""
    monkeypatch.setenv("EMBEDDING_BASE_URL", "")


@pytest.fixture(autouse=True)
def no_live_max_bot(monkeypatch):
    monkeypatch.setenv("MAX_BOT_TOKEN", "")


@pytest.fixture
def database_url(tmp_path):
    """A fresh empty database: SQLite by default, PostgreSQL when TEST_DATABASE_URL points to a server."""
    server = os.environ.get("TEST_DATABASE_URL")
    if not server:
        yield f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
        return
    name = f"test_{uuid4().hex}"
    admin = create_engine(server, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        yield make_url(server).set(database=name).render_as_string(hide_password=False)
    finally:
        with admin.connect() as connection:
            # FORCE would also try to stop autovacuum, which an ordinary role may not do; DROP waits for it itself.
            connection.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                                    "WHERE datname = :name AND usename = current_user"), {"name": name})
            connection.execute(text(f'DROP DATABASE "{name}"'))
        admin.dispose()
