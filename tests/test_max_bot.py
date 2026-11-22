import asyncio
import json

import httpx
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend import interview_worker
from backend.app import create_app
from backend.db import InterviewSession, KnowledgeCard, MaxAccount, TelegramAccount, User
from backend.interview_engine import Candidate, Evidence
from backend.max_bot import API_URL, MaxBot
from backend.settings import Settings
from backend.telegram_bot import create_invite


def test_max_invite_and_interview(database_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", database_url)
    command.upgrade(Config("alembic.ini"), "head")
    settings = Settings(database_url=database_url, app_origin="http://localhost:5173", telegram_bot_token="",
                        max_ca_file="")
    app = create_app(settings)
    with app.state.sessions.begin() as db:
        owner = User(email="expert@example.test", password_hash="unused",
                     interview_profile={"role": "Python backend", "responsibilities": "Отчёты"})
        db.add(owner)
        db.flush()
        db.add(TelegramAccount(telegram_id=7, user_id=owner.id, chat_id=42))
        owner_id = owner.id
        telegram_only = User(email="telegram-8@telegram.invalid", password_hash="unused")
        db.add(telegram_only)
        db.flush()
        db.add(TelegramAccount(telegram_id=8, user_id=telegram_only.id, chat_id=84))
        telegram_only_id = telegram_only.id
    code = create_invite(app.state.sessions, "expert@example.test")

    async def extract(settings, question, answer, state):
        assert state["intake"]["responsibilities"] == "Отчёты"
        assert state["intake"]["tone"] == "business"
        return Evidence(facts=[Candidate(field="task", quote="Медленный отчёт"),
                               Candidate(field="action", quote="добавил индекс")],
                        ambiguous=[], not_applicable=[], refused=False)

    async def formulate(*args):
        return "Почему вы решили добавить индекс?"

    async def synthesize(settings, title, state):
        return {"title": title, "actions": [state["facts"][1]]}, "Добавлен индекс для отчёта."

    monkeypatch.setattr(interview_worker, "extract", extract)
    monkeypatch.setattr(interview_worker, "formulate", formulate)
    monkeypatch.setattr(interview_worker, "synthesize", synthesize)
    calls = []

    def max_api(request):
        assert str(request.url).startswith(API_URL)
        assert request.headers["Authorization"] == "test-max-token"
        body = json.loads(request.content or b"{}")
        calls.append((request.method, request.url, body))
        if request.method == "POST" and request.url.path == "/messages":
            return httpx.Response(200, json={"message": {"body": {"mid": f"mid.{len(calls)}"}}})
        return httpx.Response(200, json={"success": True})

    def buttons():
        return [button["payload"] for method, url, body in calls if method == "POST" and url.path == "/messages"
                for attachment in body.get("attachments", []) for row in attachment["payload"]["buttons"] for button in row]

    def message(text, *, chat_type="dialog", user_id=7):
        return {"update_type": "message_created", "message": {"sender": {"user_id": user_id},
                "recipient": {"chat_type": chat_type}, "body": {"text": text}}}

    def callback(payload):
        return {"update_type": "message_callback", "callback": {"callback_id": "cb", "user": {"user_id": 7},
                "payload": payload}, "message": {"recipient": {"chat_type": "dialog"}, "body": {"mid": "mid.1"}}}

    async def scenario():
        bot = MaxBot(app, settings.model_copy(update={"max_bot_token": "test-max-token"}),
                     transport=httpx.MockTransport(max_api))
        bot.answer_delay = bot.poll_seconds = 0.02
        try:
            await bot.dispatch(message(f"/start {code}", chat_type="chat"))
            assert not calls
            await bot.dispatch({"update_type": "bot_started", "user": {"user_id": 7}, "payload": code})
            assert "new" in buttons()
            await bot.dispatch(message(f"/start {code}", user_id=8))
            assert calls[-1][1].params["user_id"] == "8"
            assert "недействительна" in calls[-2][2]["text"]
            await bot.dispatch(message("/profile"))
            await bot.dispatch(callback("intake-keep-role"))
            for _ in range(3):
                await bot.dispatch(callback("intake-skip"))
            assert buttons()[-3:] == ["tone:friendly", "tone:business", "tone:playful"]
            await bot.dispatch(callback("tone:business"))
            assert calls[-1][2]["text"] == "Анкета сохранена."
            await bot.dispatch(callback("new"))
            await bot.settle()
            finish = next(button for button in buttons() if button.startswith("finish:"))
            await bot.dispatch(message("Медленный отчёт, добавил индекс."))
            await bot.settle()
            assert any(body.get("text") == "Почему вы решили добавить индекс?" for _, _, body in calls)
            await bot.dispatch(callback(finish))
            await bot.dispatch(callback(finish.replace("finish:", "finish-yes:")))
            await bot.settle()
            assert any("Добавлен индекс для отчёта." in body.get("text", "") for _, _, body in calls)
            callback_answers = [(method, url, body) for method, url, body in calls if url.path == "/answers"]
            assert callback_answers
            for method, url, body in callback_answers:
                assert method == "POST" and url.params["callback_id"] == "cb"
                assert body == {"notification": "Принято"}
            await bot.dispatch({"update_type": "bot_started", "user": {"user_id": 8}})
            assert buttons()[-1] == "new"
            with app.state.sessions() as db:
                max_user_id = db.get(MaxAccount, 8).user_id
            await bot.dispatch(message("/start", user_id=8))
            assert buttons()[-1] == "new"
            with app.state.sessions() as db:
                assert db.get(MaxAccount, 8).user_id == max_user_id
            await bot.dispatch(message("/new", user_id=8))
            assert "Кем вы работаете" in calls[-1][2]["text"]
            assert all(url.params.get("user_id") in {"7", "8"} for method, url, _ in calls
                       if method == "POST" and url.path == "/messages")
        finally:
            await bot.close()

    with TestClient(app):
        asyncio.run(scenario())
    with app.state.sessions() as db:
        assert db.get(MaxAccount, 7).user_id == owner_id
        max_user = db.get(User, db.get(MaxAccount, 8).user_id)
        assert max_user.id != telegram_only_id
        assert max_user.email == "max-8@max.invalid"
        assert not max_user.is_editor
        assert db.get(TelegramAccount, 7).chat_id == 42
        assert db.get(User, owner_id).interview_profile["tone"] == "business"
        assert db.scalar(select(InterviewSession)).context["tone"] == "business"
        assert db.scalar(select(KnowledgeCard)).owner_id == owner_id
