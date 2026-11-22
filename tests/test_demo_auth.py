from uuid import uuid4

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.db import User
from backend.demo_auth import seed_review
from backend.security import hash_password
from backend.settings import Settings

ORIGIN = {"Origin": "http://localhost:5173"}
LOGIN = "/api/v1/auth/demo"
QUEUE = "/api/v1/cards?scope=review"


def demo(client, role, *, switch=False):
    response = client.post(LOGIN, headers=ORIGIN, json={"role": role, "switch": switch})
    assert response.status_code == 200, response.text
    assert response.json()["is_demo"] is True
    assert response.json()["is_editor"] == (role == "moderator")
    return response.json()["id"]


def test_demo_switch_preserves_work_and_isolates_moderation(database_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", database_url)
    command.upgrade(Config("alembic.ini"), "head")
    settings = Settings(database_url=database_url, app_origin=ORIGIN["Origin"], demo_auth_enabled=True,
                        telegram_bot_token="", hh_weekly_enabled=False, documentation_weekly_enabled=False)
    app = create_app(settings)
    with app.state.sessions.begin() as db:
        staff_user = User(email="staff@example.test", is_editor=True, password_hash=hash_password("staff-password"))
        db.add(staff_user)
        db.flush()
        seed_review(db, staff_user.id)
    with TestClient(app) as guest, TestClient(app) as other_guest, TestClient(app) as staff:
        assert guest.get(LOGIN).json() == {"enabled": True}
        assert guest.post(LOGIN, json={"role": "moderator"}).status_code == 403
        assert guest.post(LOGIN, headers=ORIGIN, json={"role": "admin"}).status_code == 422
        assert guest.post(LOGIN, headers=ORIGIN, json={"role": "moderator", "switch": True}).status_code == 401
        assert staff.post("/api/v1/auth/login", headers=ORIGIN,
                          json={"email": "staff@example.test", "password": "staff-password"}).status_code == 200

        participant_id = demo(guest, "participant")
        profile = {"role": "Бэкенд-разработчик", "tone": "business"}
        assert guest.put("/api/v1/interview-profile", headers=ORIGIN, json=profile).status_code == 200
        assert guest.get(QUEUE).status_code == 403
        original_token = guest.cookies.get("modeus_session")
        moderator_id = demo(guest, "moderator", switch=True)
        assert participant_id != moderator_id
        assert guest.get("/api/v1/me").json() == {"id": moderator_id, "is_demo": True, "is_editor": True}
        with TestClient(app) as revoked:
            revoked.cookies.set("modeus_session", original_token)
            assert revoked.get("/api/v1/me").status_code == 401
        assert guest.get("/api/v1/vacancies/overview").status_code == 403
        assert guest.post("/api/v1/vacancies/python_backend/collect", headers=ORIGIN).status_code == 403

        card, = guest.get(QUEUE).json()["items"]
        path = f"/api/v1/cards/{card['id']}"
        fact_id = card["content"]["actions"][0]["id"]
        evidence = guest.get(f"{path}/facts/{fact_id}/evidence")
        turn, = evidence.json()["turns"]
        assert turn["question"] and turn["answer_quote"] and turn["source"]["quote"]
        real_card, = staff.get(QUEUE).json()["items"]
        assert guest.get(f"/api/v1/cards/{real_card['id']}").status_code == 404
        assert staff.get(path).status_code == 404
        demo(other_guest, "moderator")
        other_card, = other_guest.get(QUEUE).json()["items"]
        assert other_card["id"] != card["id"]
        assert other_guest.get(path).status_code == 404

        headers = {**ORIGIN, "Idempotency-Key": str(uuid4())}
        result = guest.post(path + "/review", headers=headers,
                            json={"expected_version": card["version"], "decision": "return", "comment": "Проверить замер"})
        assert result.status_code == 200, result.text
        assert guest.get(QUEUE).json()["items"] == []
        assert demo(guest, "participant", switch=True) == participant_id
        assert guest.get("/api/v1/interview-profile").json()["tone"] == "business"
        current = guest.get(path).json()
        assert current["review"]["comment"] == "Проверить замер"
        result = guest.post(path + "/submit-review", headers={**ORIGIN, "Idempotency-Key": str(uuid4())},
                            json={"expected_version": current["version"], "consent": True, "show_author": False})
        assert result.status_code == 200, result.text
        assert demo(guest, "moderator", switch=True) == moderator_id
        result = guest.post(path + "/review", headers={**ORIGIN, "Idempotency-Key": str(uuid4())},
                            json={"expected_version": result.json()["version"], "decision": "accept"})
        assert result.status_code == 200, result.text
        assert guest.get("/api/v1/cards?scope=published").json()["items"][0]["id"] == card["id"]
        assert guest.get("/api/v1/catalog").json()["items"] == []
        assert other_guest.get(f"/api/v1/catalog/{card['id']}").status_code == 404
        assert other_guest.get(QUEUE).json()["items"][0]["id"] == other_card["id"]

        with TestClient(create_app(settings.model_copy(update={"demo_auth_enabled": False}))) as disabled:
            disabled.cookies.set("modeus_session", guest.cookies.get("modeus_session"))
            assert disabled.get(LOGIN).json() == {"enabled": False}
            assert disabled.get("/api/v1/me").status_code == 401
            assert disabled.post(LOGIN, headers=ORIGIN, json={"role": "moderator"}).status_code == 404
        assert guest.post("/api/v1/auth/logout", headers=ORIGIN).status_code == 204
        assert guest.get("/api/v1/me").status_code == 401
