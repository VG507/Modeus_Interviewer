from datetime import timedelta

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app import create_app
from backend.db import LoginSession, User, now
from backend.security import hash_password
from backend.settings import Settings


@pytest.fixture
def client(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(Config("alembic.ini"), "head")
    app = create_app(Settings(database_url=url, app_origin="http://localhost:5173"))
    with app.state.sessions() as db:
        db.add(User(email="tester@example.test", password_hash=hash_password("test-password-123")))
        db.commit()
    with TestClient(app) as client:
        yield client


def login(client, password="test-password-123", origin="http://localhost:5173"):
    return client.post("/api/v1/auth/login", json={"email": "tester@example.test", "password": password},
                       headers={"Origin": origin})


def test_session_lifecycle_and_csrf(client):
    assert client.get("/api/v1/health").status_code == 200
    assert client.get("/api/v1/me").status_code == 401
    assert login(client, origin="http://attacker.test").status_code == 403
    response = login(client)
    assert response.status_code == 200
    assert "token" not in response.json()
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie
    assert client.get("/api/v1/me").status_code == 200
    old_cookie = client.cookies.get("modeus_session")
    assert client.post("/api/v1/auth/logout").status_code == 403
    assert client.post("/api/v1/auth/logout", headers={"Origin": "http://localhost:5173"}).status_code == 204
    client.cookies.set("modeus_session", old_cookie)
    assert client.get("/api/v1/me").status_code == 401


def test_expiry_and_token_hash_storage(client):
    login(client)
    raw = client.cookies.get("modeus_session")
    with client.app.state.sessions() as db:
        session = db.scalar(select(LoginSession))
        assert session.token_hash != raw
        session.expires_at = now() - timedelta(seconds=1)
        db.commit()
    assert client.get("/api/v1/me").status_code == 401


def test_account_lockout(client):
    for _ in range(5):
        assert login(client, password="wrong").status_code == 401
    assert login(client).status_code == 429


def test_invalid_password_is_not_echoed(client):
    secret = "private-value-" * 30
    response = login(client, password=secret)
    assert response.status_code == 422
    assert secret not in response.text
