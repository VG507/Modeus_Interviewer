import asyncio
import json

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend import interview_worker
from backend.app import create_app
from backend.db import KnowledgeCard, Message, TelegramAccount, TelegramInvite, User
from backend.interview_engine import Candidate, Evidence
from backend.security import hash_password
from backend.settings import Settings
from backend.telegram_bot import NO_ACCESS, TelegramBot, create_invite, fixed_layout, load_account

ORIGIN = "http://localhost:5173"


class FakeTelegram:
    def __init__(self):
        self.calls = []

    def __call__(self, request):
        method = request.url.path.rsplit("/", 1)[-1]
        self.calls.append((method, json.loads(request.content or b"{}")))
        result = {"message_id": len(self.calls)} if method == "sendMessage" else True
        return httpx.Response(200, json={"ok": True, "result": result})

    def texts(self):
        return [payload["text"] for method, payload in self.calls if method == "sendMessage"]

    def buttons(self):
        return [button["callback_data"] for method, payload in self.calls if method == "sendMessage"
                for row in payload.get("reply_markup", {}).get("inline_keyboard", []) for button in row]


def message(user_id, text, chat_type="private"):
    return {"update_id": 1, "message": {"message_id": 1, "from": {"id": user_id}, "text": text,
                                        "chat": {"id": user_id, "type": chat_type}}}


def press(user_id, data):
    return {"update_id": 1, "callback_query": {"id": "query", "from": {"id": user_id}, "data": data, "message": {
        "message_id": 1, "chat": {"id": user_id, "type": "private"}}}}


def make_app(tmp_path, monkeypatch, **options):
    url = f"sqlite:///{(tmp_path / 'telegram.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(Config("alembic.ini"), "head")
    settings = Settings(database_url=url, app_origin=ORIGIN, **{"telegram_open_signup": False, **options})
    return create_app(settings), settings.model_copy(update={"telegram_bot_token": "test-token"})


def make_bot(app, settings, fake):
    bot = TelegramBot(app, settings, telegram_transport=httpx.MockTransport(fake))
    bot.answer_delay = bot.poll_seconds = 0.02
    return bot


def test_interview_in_telegram(tmp_path, monkeypatch):
    app, settings = make_app(tmp_path, monkeypatch, telegram_open_signup=True)
    answers = []

    async def extract(settings, question, answer, state):
        assert state["intake"]["role"] == "Python backend-разработчик"
        answers.append(answer["text"])
        return Evidence(facts=[Candidate(field="task", quote="Медленный отчёт"),
                               Candidate(field="action", quote="добавил индекс")],
                        ambiguous=[], not_applicable=[], refused=False)

    async def formulate(settings, topic, intent, answer, state):
        return "Почему вы решили добавить индекс?"

    async def synthesize(settings, title, state):
        return {"title": title, "actions": [state["facts"][1]]}, "Добавлен индекс для отчёта."

    monkeypatch.setattr(interview_worker, "extract", extract)
    monkeypatch.setattr(interview_worker, "formulate", formulate)
    monkeypatch.setattr(interview_worker, "synthesize", synthesize)
    fake = FakeTelegram()

    async def scenario():
        bot = make_bot(app, settings, fake)

        async def send(*updates):
            for update in updates:
                await bot.dispatch(update)
            await bot.settle()

        try:
            await send(message(7, "/start"))
            assert fake.texts()[-1].startswith("Здравствуйте!")
            await send(message(7, "/new"), message(7, "Python backend-разработчик"))
            await send(press(7, "intake-skip"), press(7, "intake-skip"), press(7, "intake-skip"))
            assert fake.texts()[-1].startswith("Вспомните один конкретный случай")
            interview_id = (await asyncio.to_thread(load_account, app.state.sessions, 7)).state["interview_id"]
            assert f"finish:{interview_id}" in fake.buttons()

            await send(message(7, "Медленный отчёт,"), message(7, "добавил индекс."))
            assert answers == ["Медленный отчёт,\nдобавил индекс."]
            assert "<i>Тема случая: Медленный отчёт</i>" in fake.texts()
            assert fake.texts()[-1] == "Почему вы решили добавить индекс?"
            assert not any(method == "sendMessage" and payload["text"].startswith("⏳")
                           for method, payload in fake.calls[-2:])

            await send(press(7, f"finish:{interview_id}"), press(7, f"finish-yes:{interview_id}"))
            card_text = next(text for text in fake.texts() if text.startswith("<b>Карточка интервью</b>"))
            assert "Добавлен индекс для отчёта." in card_text
            card_id = fake.buttons()[-1].removeprefix("publish:")
            await send(press(7, f"publish:{card_id}"), press(7, f"publish-anon:{card_id}"))
            assert fake.texts()[-1].startswith("Карточка отправлена редактору")
            await send(press(7, f"publish-anon:{card_id}"))
            assert fake.texts()[-1] == "Карточка уже отправлена редактору."
        finally:
            await bot.close()

    with TestClient(app):
        asyncio.run(scenario())
    with app.state.sessions() as db:
        card = db.scalar(select(KnowledgeCard))
        assert (card.publication_status, card.show_author) == ("in_review", False)
        assert db.scalar(select(User.email)).startswith("telegram-7@")
        assert db.scalar(select(Message.status).where(Message.kind == "answer")) == "processed"


def test_invite_links_existing_user(tmp_path, monkeypatch):
    app, settings = make_app(tmp_path, monkeypatch)
    with app.state.sessions.begin() as db:
        db.add(User(email="owner@example.test", password_hash=hash_password("test-password-123")))
    code = create_invite(app.state.sessions, "owner@example.test")
    assert create_invite(app.state.sessions, "missing@example.test") is None
    fake = FakeTelegram()

    async def scenario():
        bot = make_bot(app, settings, fake)
        try:
            await bot.dispatch(message(5, "/new", chat_type="group"))
            assert not fake.calls
            await bot.dispatch(message(5, "/new"))
            assert fake.texts()[-1] == NO_ACCESS
            await bot.dispatch(message(5, "/start wrong-code"))
            assert fake.texts()[-2:] == ["Ссылка-приглашение недействительна или устарела.", NO_ACCESS]
            await bot.dispatch(message(5, f"/start {code}"))
            assert fake.texts()[-1].startswith("Telegram связан с вашим аккаунтом Modeus.")
            await bot.dispatch(message(6, f"/start {code}"))
            assert fake.texts()[-1] == NO_ACCESS
            await bot.dispatch(message(5, "/interviews"))
            assert fake.texts()[-1] == "Интервью пока нет."
            await bot.dispatch(message(5, "Ответ без интервью"))
            await bot.settle()
            assert fake.texts()[-1].startswith("Сейчас нет открытого интервью")
        finally:
            await bot.close()

    asyncio.run(scenario())
    with app.state.sessions() as db:
        owner = db.scalar(select(User.id).where(User.email == "owner@example.test"))
        assert db.get(TelegramAccount, 5).user_id == owner
        assert db.get(TelegramAccount, 6) is None
        assert db.scalar(select(TelegramInvite)) is None


def test_unknown_profession_lists_the_catalog(tmp_path, monkeypatch):
    app, settings = make_app(tmp_path, monkeypatch, telegram_open_signup=True)
    fake = FakeTelegram()

    async def scenario():
        bot = make_bot(app, settings, fake)
        try:
            await bot.dispatch(message(9, "/start"))
            await bot.dispatch(message(9, "/new"))
            await bot.dispatch(message(9, "Повар"))
            assert fake.texts()[-1].startswith("Профессии «Повар» у нас пока нет.")
            assert "• Python backend" in fake.texts()[-1]
            assert {"role:python_backend", "role:frontend", "role:qa", "role:devops"} <= set(fake.buttons())
            assert (await asyncio.to_thread(load_account, app.state.sessions, 9)).state["step"] == "role"
            await bot.dispatch(message(9, "Я разработчик"))
            assert fake.texts()[-1].startswith("Уточните, в каком направлении вы работаете.")
            assert "• Python backend" in fake.texts()[-1]
            await bot.dispatch(message(9, "Java-разработчик"))
            assert fake.texts()[-1].startswith("Профессии «Java-разработчик» у нас пока нет.")
            await bot.dispatch(press(9, "role:qa"))
            assert fake.texts()[-1] == "Какой у вас опыт в этой роли?"
        finally:
            await bot.close()

    asyncio.run(scenario())
    with app.state.sessions() as db:
        state = db.get(TelegramAccount, 9).state
        assert (state["step"], state["intake"]) == ("experience", {"role": "QA"})


@pytest.mark.parametrize("text,expected", [
    ("ghbdtn", "привет"),
    ("Python hfphf,jnxbr", "Python разработчик"),
    ("hf,jnfk c k8s b Docker", "работал с k8s и Docker"),
    ("Python backend developer", None),
    ("SQL", None),
    ("html css", None),
    ("https://github.com/x", None),
    ("Привет", None),
])
def test_wrong_layout_is_detected_word_by_word(text, expected):
    assert fixed_layout(text) == expected


def test_role_typos_and_wrong_layout(tmp_path, monkeypatch):
    app, settings = make_app(tmp_path, monkeypatch, telegram_open_signup=True)
    fake = FakeTelegram()

    async def scenario():
        bot = make_bot(app, settings, fake)
        try:
            await bot.dispatch(message(4, "/start"))
            await bot.dispatch(message(4, "/new"))
            await bot.dispatch(message(4, "Ntcnbhjdobr"))
            assert fake.texts()[-1] == ("Похоже, вы писали в английской раскладке. Вы имели в виду: "
                                        "«Тестировщик»?")
            assert {"layout-yes", "layout-no"} <= set(fake.buttons())
            await bot.dispatch(press(4, "layout-yes"))
            assert fake.texts()[-1] == "Какой у вас опыт в этой роли?"
            await bot.dispatch(message(4, "/new"))
            await bot.dispatch(message(4, "Тестеровщик"))
            assert fake.texts()[-1] == "Записал профессию: QA.\n\nКакой у вас опыт в этой роли?"
        finally:
            await bot.close()

    asyncio.run(scenario())
    with app.state.sessions() as db:
        assert db.get(TelegramAccount, 4).state["intake"] == {"role": "QA"}


def test_interview_stops_circling_without_new_cases(tmp_path, monkeypatch):
    app, settings = make_app(tmp_path, monkeypatch, telegram_open_signup=True)

    async def extract(settings, question, answer, state):
        if "индекс" in answer["text"]:
            return Evidence(facts=[Candidate(field="task", quote="Медленный отчёт")], ambiguous=[],
                            not_applicable=[], refused=False, topic_title="Медленный отчёт")
        return Evidence(facts=[], ambiguous=[], not_applicable=[], refused=False, needs_rephrase=True,
                        assessment="unclear")

    async def formulate(settings, topic, intent, answer, state):
        return f"Уточнение {intent}?"

    async def synthesize(settings, title, state):
        return {"title": title, "actions": []}, "Карточка по отчёту."

    monkeypatch.setattr(interview_worker, "extract", extract)
    monkeypatch.setattr(interview_worker, "formulate", formulate)
    monkeypatch.setattr(interview_worker, "synthesize", synthesize)
    fake = FakeTelegram()

    async def scenario():
        bot = make_bot(app, settings, fake)

        async def send(*updates):
            for update in updates:
                await bot.dispatch(update)
            await bot.settle()

        async def interview(user, first=None, replies=0):
            await send(message(user, "/start"), message(user, "/new"), message(user, "Python-разработчик"))
            await send(*[press(user, "intake-skip")] * 3)
            if first:
                await send(message(user, first))
            for _ in range(replies):
                await send(message(user, "Ну так"))

        try:
            await interview(3, "Медленный отчёт, добавил индекс", replies=2)
            assert "Похоже, про случай «Медленный отчёт» главное мы выяснили" in fake.texts()[-1]
            for _ in range(6):
                await send(message(3, "Ну так"))
            assert "<i>Похоже, новые случаи сейчас не вспоминаются — собираю карточку из того, что вы уже рассказали.</i>" \
                in fake.texts()
            assert any(text.startswith("<b>Карточка интервью</b>") for text in fake.texts())

            start = len(fake.texts())
            await interview(13, replies=9)
            texts = fake.texts()[start:]
            assert sum(text.startswith("Похоже, сейчас сложно вспомнить подходящий случай") for text in texts) == 1
            assert texts[-1] == ("<i>Подходящий случай пока не вспомнился, поэтому я перестаю задавать вопросы. "
                                 "Когда вспомните, начните новое интервью.</i>")
            await send(message(13, "Ну так"))
            assert fake.texts()[-1] == "Сейчас нет вопроса для ответа. Отправьте /status."
        finally:
            await bot.close()

    with TestClient(app):
        asyncio.run(scenario())
