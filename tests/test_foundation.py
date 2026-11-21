import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app import create_app
from backend.db import CommandReceipt, LoginSession, LoginWindow, User, now
from backend.http_errors import LimitRequestBody
from backend.maintenance import prune_expired
from backend.security import hash_password, token_hash
from backend.settings import Settings


@pytest.fixture
def client(database_url, monkeypatch):
    url = database_url
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(Config("alembic.ini"), "head")
    app = create_app(Settings(database_url=url, app_origin="http://localhost:5173/, http://127.0.0.1:5173/"))
    with app.state.sessions() as db:
        db.add(User(email="tester@example.test", password_hash=hash_password("test-password-123")))
        db.commit()
    with TestClient(app) as client:
        yield client


def login(client, password="test-password-123", origin="http://localhost:5173"):
    return client.post("/api/v1/auth/login", json={"email": "tester@example.test", "password": password},
                       headers={"Origin": origin})


def test_session_lifecycle_and_csrf(client):
    assert client.app.state.app_origin == "http://localhost:5173"
    assert client.get("/api/v1/health").status_code == 200
    assert client.get("/api/v1/me").status_code == 401
    assert login(client, origin="http://127.0.0.1:5173").status_code == 200
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


def test_account_lockout(client, monkeypatch):
    assert login(client).status_code == 200
    for _ in range(5):
        assert login(client, password="wrong").status_code == 401

    def unexpected_hash(*args):
        pytest.fail("A locked account should be rejected before password verification")

    monkeypatch.setattr("backend.app.check_password", unexpected_hash)
    assert login(client).status_code == 429
    assert client.get("/api/v1/me").status_code == 401
    with client.app.state.sessions() as db:
        assert db.scalar(select(LoginSession)) is None


def test_https_enables_secure_session_and_disables_public_docs(client):
    origin = "https://modeus.example.test"
    app = create_app(Settings(database_url=client.app.state.settings.database_url, app_origin=origin,
                              cookie_secure=False))
    with TestClient(app, base_url=origin) as https_client:
        assert "Secure" in login(https_client, origin=origin).headers["set-cookie"]
        assert https_client.get("/api/v1/me").status_code == 200
        assert https_client.get("/openapi.json").status_code == 404


def test_login_window_rejects_excess_attempts(client, monkeypatch):
    stamp = 1_800_000_040
    monkeypatch.setattr("backend.auth.time", lambda: stamp)
    with client.app.state.sessions.begin() as db:
        db.add(LoginWindow(client=token_hash("testclient"), window=stamp // 60, attempts=15))
    response = login(client)
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "LOGIN_RATE_LIMIT"
    assert int(response.headers["Retry-After"]) > 0


def test_pruning_keeps_recent_receipts_and_valid_sessions(client):
    sessions = client.app.state.sessions
    stamp = now()
    with sessions.begin() as db:
        owner = db.scalar(select(User.id))
        for label, days in (("old", -8), ("new", 1)):
            db.add(LoginSession(token_hash=label, user_id=owner, expires_at=stamp + timedelta(days=days)))
            db.add(CommandReceipt(id=label, owner_id=owner, body_digest="digest", result={},
                                  created_at=stamp + timedelta(days=days)))
    prune_expired(sessions)
    with sessions() as db:
        assert list(db.scalars(select(LoginSession.token_hash))) == ["new"]
        assert list(db.scalars(select(CommandReceipt.id))) == ["new"]


def test_unexpected_errors_use_the_same_envelope(client):
    app = create_app(client.app.state.settings)

    @app.get("/test/failure")
    def broken():
        raise RuntimeError("private error detail")

    with TestClient(app, raise_server_exceptions=False) as failing_client:
        response = failing_client.get("/test/failure")
        assert response.status_code == 500
        error = response.json()["error"]
        assert error["code"] == "INTERNAL_SERVER_ERROR"
        assert error["request_id"]
        assert "private error detail" not in response.text


def test_body_size_is_checked_before_parsing(client):
    app = create_app(client.app.state.settings)
    app.add_middleware(LimitRequestBody, limit=10)
    with TestClient(app) as limited_client:
        response = limited_client.post("/api/v1/auth/login", json={"email": "tester@example.test"})
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"

    async def chunks():
        yield b'{"email":'
        yield b'"tester@example.test"}'

    async def send_chunks():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://testserver") as streamed:
            return await streamed.post("/api/v1/auth/login", content=chunks(),
                                       headers={"Content-Type": "application/json"})

    response = asyncio.run(send_chunks())
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"


def test_concurrent_interview_quota_and_idempotent_replay(client):
    assert login(client).status_code == 200
    path = "/api/v1/interviews"
    body = {"context": {"role": "QA"}}

    def create(key):
        return client.post(path, json=body, headers={"Origin": "http://localhost:5173", "Idempotency-Key": key})

    keys = [str(uuid4()) for _ in range(6)]
    with ThreadPoolExecutor(max_workers=len(keys)) as pool:
        responses = list(pool.map(create, keys))
    assert sorted(response.status_code for response in responses) == [201] * 5 + [409]
    rejected = next(response for response in responses if response.status_code == 409)
    assert rejected.json()["error"]["code"] == "ACTIVE_INTERVIEW_LIMIT"
    index = next(index for index, response in enumerate(responses) if response.status_code == 201)
    replayed = create(keys[index])
    assert replayed.status_code == 201
    assert replayed.json() == responses[index].json()

    first = client.get(path, params={"limit": 2}).json()
    second = client.get(path, params={"limit": 3, "cursor": first["next_cursor"]}).json()
    items = first["items"] + second["items"]
    assert len({item["id"] for item in items}) == 5
    assert items == sorted(items, key=lambda item: (item["created_at"], item["id"]), reverse=True)
    assert second["next_cursor"] is None


def test_invalid_password_is_not_echoed(client):
    secret = "private-value-" * 30
    response = login(client, password=secret)
    assert response.status_code == 422
    assert secret not in response.text
