"""Telegram front end for text interviews.

The bot acts for a linked participant through the same HTTP API as the web client,
so validation, quotas, idempotency and processing stay in one place.
"""

import asyncio
import html
import logging
import secrets
from dataclasses import dataclass, replace
from datetime import timedelta
from uuid import uuid4

import httpx
from sqlalchemy import delete, select

from backend.db import LoginSession, TelegramAccount, TelegramInvite, User, now
from backend.security import hash_password, token_hash

logger = logging.getLogger(__name__)
INVITE_HOURS = 72
ANSWER_LIMIT = 8000
AUTHOR_LIMIT = 120
CHUNK_SIZE = 3500
FOLLOW_SECONDS = 600
# Control messages that only echo the participant's own buttons.
OWN_CONTROLS = {"Пропуск вопроса", "Завершение интервью"}
INTAKE = (
    ("role", "Какая у вас роль? Например, «Python backend-разработчик».", 200),
    ("experience", "Какой у вас опыт в этой роли?", 1000),
    ("responsibilities", "За что вы отвечаете в работе?", 1000),
    ("conditions", "В каком контексте вы работаете: команда, продукт, ограничения?", 1000),
)
STAGES = {
    "initial_question": "Готовлю первый вопрос",
    "extraction": "Разбираю ответ",
    "question": "Готовлю уточнение",
    "synthesis": "Собираю карточку",
}
STATUSES = {"active": "в процессе", "completing": "собирается карточка", "completed": "завершено"}
PROCESSING_ERRORS = {
    "PROCESSING_TIMEOUT": "Модель не успела ответить.",
    "LLM_UNAVAILABLE": "Модель недоступна.",
    "INVALID_MODEL_OUTPUT": "Модель вернула ответ в неподходящем формате.",
}
ONE_SHOT = {"skip", "retry", "finish-yes", "finish-no", "intake-skip", "publish", "publish-anon", "publish-name"}
COMMANDS = [
    {"command": "new", "description": "Начать новое интервью"},
    {"command": "interviews", "description": "Мои интервью"},
    {"command": "status", "description": "Показать текущий вопрос"},
    {"command": "skip", "description": "Пропустить вопрос"},
    {"command": "finish", "description": "Завершить интервью и собрать карточку"},
    {"command": "cancel", "description": "Отменить ввод"},
    {"command": "help", "description": "Как пользоваться ботом"},
]
HELP = (
    "Modeus расспрашивает о конкретных рабочих случаях и собирает из ответов карточку знаний.\n\n"
    "/new — начать новое интервью\n"
    "/interviews — мои интервью\n"
    "/status — показать текущий вопрос\n"
    "/skip — пропустить вопрос\n"
    "/finish — завершить интервью и собрать карточку\n"
    "/cancel — отменить ввод\n\n"
    "На вопросы отвечайте обычными сообщениями."
)
NO_ACCESS = ("Доступ к Modeus выдаёт команда проекта. Попросите ссылку-приглашение и откройте её, "
             "чтобы связать Telegram с вашим аккаунтом.")
NO_INTERVIEW = "Сейчас нет открытого интервью. Начните новое командой /new."


@dataclass(frozen=True)
class Account:
    telegram_id: int
    user_id: str
    chat_id: int
    state: dict


class Notice(Exception):
    """A message for the participant instead of the requested action."""


class TelegramError(Exception):
    def __init__(self, status, description):
        super().__init__(description)
        self.status = status


class ApiFailure(Exception):
    def __init__(self, status, error):
        super().__init__(error.get("message") or "Сервер вернул ошибку.")
        self.status = status
        self.code = error.get("code", "")


def create_invite(sessions, email, hours=INVITE_HOURS):
    """Return a one-time /start code for an existing user, or None."""
    code = secrets.token_urlsafe(16)
    with sessions.begin() as db:
        user_id = db.scalar(select(User.id).where(User.email == email))
        if user_id is None:
            return None
        db.add(TelegramInvite(code_hash=token_hash(code), user_id=user_id,
                              expires_at=now() + timedelta(hours=hours)))
    return code


def invite_link(settings, code):
    if not settings.telegram_bot_token:
        return None
    try:
        response = httpx.get(f"{settings.telegram_api_url.rstrip('/')}/bot{settings.telegram_bot_token}/getMe",
                             timeout=10)
        username = response.json()["result"]["username"]
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return None
    return f"https://t.me/{username}?start={code}"


def link_account(sessions, telegram_id, chat_id, code, open_signup):
    """Link the chat by invite, by an earlier link or by open sign-up; return (user_id, invite status)."""
    with sessions.begin() as db:
        account = db.get(TelegramAccount, telegram_id)
        invite = db.get(TelegramInvite, token_hash(code)) if code else None
        status = "invalid" if code else None
        if invite is not None:
            db.delete(invite)
            if invite.expires_at > now():
                status = "used"
        if status == "used":
            user_id = invite.user_id
        elif account is not None:
            account.chat_id = chat_id
            return account.user_id, status
        elif open_signup:
            email = f"telegram-{telegram_id}@telegram.invalid"
            user_id = db.scalar(select(User.id).where(User.email == email))
            if user_id is None:
                user = User(email=email, password_hash=hash_password(secrets.token_urlsafe(24)))
                db.add(user)
                db.flush()
                user_id = user.id
        else:
            return None, status
        db.execute(delete(TelegramAccount).where(TelegramAccount.user_id == user_id,
                                                 TelegramAccount.telegram_id != telegram_id))
        if account is None:
            db.add(TelegramAccount(telegram_id=telegram_id, user_id=user_id, chat_id=chat_id, state={}))
        elif account.user_id != user_id:
            account.user_id, account.chat_id, account.state = user_id, chat_id, {}
        else:
            account.chat_id = chat_id
        return user_id, status


def load_account(sessions, telegram_id):
    with sessions() as db:
        account = db.get(TelegramAccount, telegram_id)
        if account is None:
            return None
        return Account(account.telegram_id, account.user_id, account.chat_id, dict(account.state))


def save_state(sessions, telegram_id, changes, interview_id=None):
    """Merge state changes; with interview_id, only while that interview is still open in the chat."""
    with sessions.begin() as db:
        account = db.get(TelegramAccount, telegram_id)
        if account is None or (interview_id and account.state.get("interview_id") != interview_id):
            return False
        account.state = {**account.state, **changes}
        return True


def followed_accounts(sessions):
    with sessions() as db:
        return [account.telegram_id for account in db.scalars(select(TelegramAccount))
                if account.state.get("interview_id")]


def mint_session(sessions, user_id, hours):
    token = secrets.token_urlsafe(32)
    with sessions.begin() as db:
        db.add(LoginSession(token_hash=token_hash(token), user_id=user_id,
                            expires_at=now() + timedelta(hours=hours)))
    return token


def escape(text):
    return html.escape(text or "", quote=False)


def keyboard(rows):
    return {"inline_keyboard": [[{"text": text, "callback_data": data} for text, data in row] for row in rows]}


def chunks(text, size=CHUNK_SIZE):
    parts = []
    while len(text) > size:
        cut = text.rfind("\n", 0, size)
        cut = cut if cut > 0 else size
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return [*parts, text]


def processing(detail):
    return detail["processing_status"] in {"queued", "running"}


def require_question(detail):
    if detail["status"] != "active":
        raise Notice("Это интервью уже завершено. Начните новое командой /new.")
    if processing(detail):
        raise Notice("Сообщение не отправлено: я ещё обрабатываю предыдущий шаг. "
                     "Дождитесь следующего вопроса и ответьте ещё раз.")
    if detail["processing_status"] == "failed":
        raise Notice("Обработка прервалась. Нажмите «Повторить» или отправьте /status.")
    if not detail["current_question_id"]:
        raise Notice("Сейчас нет вопроса для ответа. Отправьте /status.")


class TelegramBot:
    def __init__(self, app, settings, *, telegram_transport=None):
        self.sessions = app.state.sessions
        self.settings = settings
        self.origin = app.state.app_origin
        self.telegram = httpx.AsyncClient(
            base_url=f"{settings.telegram_api_url.rstrip('/')}/bot{settings.telegram_bot_token}/",
            timeout=httpx.Timeout(15, read=40), transport=telegram_transport)
        self.api = httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                                     base_url="http://modeus.internal/api/v1", timeout=60)
        self.tokens = {}
        self.locks = {}
        self.buffers = {}
        self.flushers = {}
        self.followers = {}
        self.pending = set()
        self.tasks = set()
        self.answer_delay = 1.0
        self.poll_seconds = 1.0

    async def close(self):
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.telegram.aclose()
        await self.api.aclose()

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def settle(self):
        """Wait for buffered answers and followers to finish."""
        while self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)

    def lock(self, telegram_id):
        return self.locks.setdefault(telegram_id, asyncio.Lock())

    async def call(self, method, **params):
        for attempt in range(2):
            response = await self.telegram.post(method, json=params)
            try:
                data = response.json()
            except ValueError:
                data = None
            if not isinstance(data, dict):
                raise TelegramError(response.status_code, "Invalid response")
            if data.get("ok"):
                return data.get("result")
            retry_after = (data.get("parameters") or {}).get("retry_after")
            if response.status_code == 429 and isinstance(retry_after, int) and not attempt:
                await asyncio.sleep(min(retry_after, 30))
                continue
            raise TelegramError(response.status_code, data.get("description", ""))

    async def quietly(self, method, **params):
        try:
            return await self.call(method, **params)
        except TelegramError as exc:
            logger.warning("Telegram %s failed: %s %s", method, exc.status, exc)
        except httpx.HTTPError as exc:
            # The request URL contains the bot token.
            logger.warning("Telegram %s failed: %s", method, type(exc).__name__)
        return None

    async def send(self, chat_id, text, buttons=None):
        params = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                  "link_preview_options": {"is_disabled": True}}
        if buttons:
            params["reply_markup"] = keyboard(buttons)
        return await self.quietly("sendMessage", **params)

    async def request(self, user_id, method, path, body=None):
        cached = self.tokens.get(user_id)
        for token in [cached, None] if cached else [None]:
            if token is None:
                token = self.tokens[user_id] = await asyncio.to_thread(
                    mint_session, self.sessions, user_id, self.settings.session_hours)
            headers = {"Cookie": f"modeus_session={token}"}
            if method != "GET":
                headers.update({"Origin": self.origin, "Idempotency-Key": str(uuid4())})
            response = await self.api.request(method, path, json=body, headers=headers)
            if response.status_code != 401:
                break
        try:
            data = response.json()
        except ValueError:
            data = {}
        if response.is_success:
            return data
        error = data.get("error") if isinstance(data, dict) else None
        raise ApiFailure(response.status_code, error if isinstance(error, dict) else {})

    async def versioned(self, user_id, path, action, body=None, check=None):
        """Send a command with the current version and re-read once after a conflict."""
        for attempt in range(2):
            current = await self.request(user_id, "GET", path)
            if check:
                check(current)
            try:
                return await self.request(user_id, "POST", f"{path}/{action}",
                                          {"expected_version": current["version"], **(body or {})})
            except ApiFailure as exc:
                if exc.code != "VERSION_CONFLICT" or attempt:
                    raise

    async def set_state(self, account, **changes):
        await asyncio.to_thread(save_state, self.sessions, account.telegram_id, changes)
        return replace(account, state={**account.state, **changes})

    async def run(self):
        try:
            await self.call("deleteWebhook")
            await self.call("setMyCommands", commands=COMMANDS)
        except TelegramError as exc:
            if exc.status in {401, 404}:
                logger.error("Telegram rejected TELEGRAM_BOT_TOKEN")
                return
            logger.warning("Telegram setup failed: %s %s", exc.status, exc)
        except httpx.HTTPError as exc:
            logger.warning("Telegram setup failed: %s", type(exc).__name__)
        for telegram_id in await asyncio.to_thread(followed_accounts, self.sessions):
            self.follow(telegram_id)
        offset = None
        while True:
            params = {"timeout": 25, "allowed_updates": ["message", "callback_query"]}
            if offset is not None:
                params["offset"] = offset
            try:
                updates = await self.call("getUpdates", **params)
            except TelegramError as exc:
                if exc.status in {401, 404}:
                    logger.error("Telegram rejected TELEGRAM_BOT_TOKEN")
                    return
                logger.warning("Telegram polling failed: %s %s", exc.status, exc)
                await asyncio.sleep(5)
                continue
            except httpx.HTTPError as exc:
                logger.warning("Telegram polling failed: %s", type(exc).__name__)
                await asyncio.sleep(5)
                continue
            for update in updates if isinstance(updates, list) else []:
                if isinstance(update, dict) and isinstance(update.get("update_id"), int):
                    offset = update["update_id"] + 1
                    self.spawn(self.dispatch(update))

    async def dispatch(self, update):
        query, message = update.get("callback_query"), update.get("message")
        source = query or message
        if not isinstance(source, dict):
            return
        chat = (query.get("message") or {}).get("chat", {}) if query else message.get("chat", {})
        telegram_id, chat_id = source.get("from", {}).get("id"), chat.get("id")
        if not isinstance(telegram_id, int) or not isinstance(chat_id, int) or chat.get("type") != "private":
            return
        async with self.lock(telegram_id):
            await self.guarded(chat_id, self.handle(telegram_id, chat_id, message, query))

    async def guarded(self, chat_id, operation):
        try:
            await operation
        except (Notice, ApiFailure) as exc:
            await self.send(chat_id, escape(str(exc)))
        except Exception:
            logger.exception("Telegram update failed")
            await self.send(chat_id, "Что-то пошло не так. Попробуйте ещё раз.")

    async def handle(self, telegram_id, chat_id, message, query):
        if query:
            await self.quietly("answerCallbackQuery", callback_query_id=query.get("id"))
        text = "" if query else (message.get("text") or "").strip()
        command, _, argument = text.partition(" ") if text.startswith("/") else ("", "", "")
        command = command.split("@")[0].lower()
        if command == "/start":
            return await self.start(telegram_id, chat_id, argument.strip()[:64])
        account = await asyncio.to_thread(load_account, self.sessions, telegram_id)
        if account is None:
            return await self.send(chat_id, NO_ACCESS)
        if query:
            await self.flush(account)
            return await self.on_button(account, query)
        if not text:
            return await self.send(chat_id, "Я понимаю только текстовые сообщения.")
        step = account.state.get("step")
        if command:
            await self.flush(account)
            if step:
                account = await self.set_state(account, step=None)
            return await self.on_command(account, command)
        if step == "author_name":
            if len(text) > AUTHOR_LIMIT:
                raise Notice(f"Имя должно быть не длиннее {AUTHOR_LIMIT} символов.")
            card_id = account.state.get("card_id")
            account = await self.set_state(account, step=None, card_id=None)
            return await self.publish(account, card_id, text)
        if step:
            return await self.intake_answer(account, text)
        self.buffer(account, text)

    async def start(self, telegram_id, chat_id, code):
        user_id, invite = await asyncio.to_thread(
            link_account, self.sessions, telegram_id, chat_id, code, self.settings.telegram_open_signup)
        if invite == "invalid":
            await self.send(chat_id, "Ссылка-приглашение недействительна или устарела.")
        if user_id is None:
            return await self.send(chat_id, NO_ACCESS)
        greeting = "Telegram связан с вашим аккаунтом Modeus." if invite == "used" else "Здравствуйте!"
        await self.send(chat_id, f"{greeting}\n\n{escape(HELP)}", [[("Начать интервью", "new")]])

    async def on_command(self, account, command):
        if command in {"/help", "/cancel"}:
            text = "Ввод отменён." if command == "/cancel" else escape(HELP)
            return await self.send(account.chat_id, text)
        if command == "/new":
            return await self.start_intake(account)
        if command == "/interviews":
            return await self.list_interviews(account)
        if command == "/status":
            return await self.show_current(account)
        if command == "/skip":
            return await self.skip(account)
        if command == "/finish":
            return await self.confirm_finish(account, account.state.get("interview_id"))
        await self.send(account.chat_id, f"Не знаю такой команды.\n\n{escape(HELP)}")

    async def on_button(self, account, query):
        action, _, value = str(query.get("data") or "").partition(":")
        message = query.get("message") or {}
        if action in ONE_SHOT and message.get("message_id"):
            await self.quietly("editMessageReplyMarkup", chat_id=account.chat_id,
                               message_id=message["message_id"], reply_markup={"inline_keyboard": []})
        if action == "new":
            await self.start_intake(account)
        elif action == "intake-skip" and account.state.get("step") in {name for name, *_ in INTAKE[1:]}:
            await self.intake_answer(account, "")
        elif action == "skip":
            await self.skip(account, value)
        elif action == "finish":
            await self.confirm_finish(account, value)
        elif action == "finish-yes":
            await self.finish(account, value)
        elif action == "finish-no":
            await self.send(account.chat_id, "Продолжаем. Ответьте на текущий вопрос.")
        elif action == "retry":
            await self.retry(account, value)
        elif action == "open":
            await self.show_current(account, value)
        elif action == "publish":
            await self.confirm_publish(account, value)
        elif action == "publish-anon":
            await self.publish(account, value)
        elif action == "publish-name":
            await self.set_state(account, step="author_name", card_id=value)
            await self.send(account.chat_id, f"Как подписать карточку? Напишите имя или псевдоним "
                                             f"(до {AUTHOR_LIMIT} символов).")

    def buffer(self, account, text):
        # Telegram splits long messages; join parts that arrive together into one answer.
        self.buffers.setdefault(account.telegram_id, []).append(text)
        timer = self.flushers.get(account.telegram_id)
        if timer:
            timer.cancel()
        self.flushers[account.telegram_id] = self.spawn(self.flush_later(account.telegram_id, account.chat_id))

    async def flush_later(self, telegram_id, chat_id):
        await asyncio.sleep(self.answer_delay)
        async with self.lock(telegram_id):
            account = await asyncio.to_thread(load_account, self.sessions, telegram_id)
            if account is not None:
                await self.guarded(chat_id, self.flush(account))

    async def flush(self, account):
        """Submit buffered answer parts; the caller holds the participant's lock."""
        timer = self.flushers.pop(account.telegram_id, None)
        if timer is not None and timer is not asyncio.current_task():
            timer.cancel()
        parts = self.buffers.pop(account.telegram_id, None)
        if parts:
            await self.submit_answer(account, "\n".join(parts))

    def interview_path(self, account, interview_id=None):
        current = account.state.get("interview_id")
        if not current:
            raise Notice(NO_INTERVIEW)
        if interview_id and interview_id != current:
            raise Notice("Эта кнопка относится к другому интервью. Откройте его через /interviews.")
        return f"/interviews/{current}"

    async def submit_answer(self, account, text):
        path = self.interview_path(account)
        if len(text) > ANSWER_LIMIT:
            raise Notice(f"Ответ длиннее {ANSWER_LIMIT} символов. Сократите его и отправьте снова.")
        for attempt in range(2):
            detail = await self.request(account.user_id, "GET", path)
            require_question(detail)
            question_id = detail["current_question_id"]
            draft = next((item for item in detail["messages"]
                          if item["status"] == "draft" and item["reply_to_question_id"] == question_id), None)
            try:
                if draft:
                    saved = await self.request(account.user_id, "PATCH", f"{path}/answers/{draft['id']}",
                                               {"expected_version": detail["version"], "text": text})
                else:
                    saved = await self.request(account.user_id, "POST", f"{path}/answers", {
                        "expected_version": detail["version"], "question_id": question_id, "text": text})
                await self.request(account.user_id, "POST", f"{path}/answers/{saved['answer']['id']}/submit",
                                   {"expected_version": saved["interview"]["version"]})
                break
            except ApiFailure as exc:
                if exc.code != "VERSION_CONFLICT" or attempt:
                    raise
        self.follow(account.telegram_id)

    async def start_intake(self, account):
        await self.set_state(account, step=INTAKE[0][0], intake={})
        await self.send(account.chat_id, "Сначала несколько вопросов о вашей работе: они помогут задавать "
                                         f"точные уточнения.\n\n{escape(INTAKE[0][1])}")

    async def intake_answer(self, account, value):
        names = [name for name, *_ in INTAKE]
        index = names.index(account.state["step"])
        name, prompt, limit = INTAKE[index]
        if not value and index == 0:
            raise Notice(prompt)
        if len(value) > limit:
            raise Notice(f"Ответ должен быть не длиннее {limit} символов.")
        intake = {**account.state.get("intake", {}), name: value}
        if index + 1 < len(INTAKE):
            await self.set_state(account, step=names[index + 1], intake=intake)
            return await self.send(account.chat_id, escape(INTAKE[index + 1][1]), [[("Пропустить", "intake-skip")]])
        account = await self.set_state(account, step=None, intake=None)
        created = await self.request(account.user_id, "POST", "/interviews", {"context": intake})
        await self.set_state(account, interview_id=created["id"], seen=0)
        await self.send(account.chat_id, "Интервью началось. Отвечайте на вопросы обычными сообщениями, "
                                         "подробно, как рассказали бы коллеге. Под вопросом есть кнопки "
                                         "«Пропустить» и «Завершить интервью».")
        self.follow(account.telegram_id)

    async def skip(self, account, question_id=None):
        path = self.interview_path(account)

        def check(detail):
            require_question(detail)
            if question_id and detail["current_question_id"] != question_id:
                raise Notice("Этот вопрос уже неактуален.")

        await self.versioned(account.user_id, path, "skip", check=check)
        self.follow(account.telegram_id)

    async def confirm_finish(self, account, interview_id):
        self.interview_path(account, interview_id)
        await self.send(account.chat_id, "Собрать карточку из отправленных ответов и завершить интервью?", [[
            ("Собрать карточку", f"finish-yes:{account.state['interview_id']}"), ("Продолжить", "finish-no"),
        ]])

    async def finish(self, account, interview_id):
        def check(detail):
            if detail["status"] != "active":
                raise Notice("Интервью уже завершено.")

        await self.versioned(account.user_id, self.interview_path(account, interview_id), "finish", check=check)
        self.follow(account.telegram_id)

    async def retry(self, account, interview_id):
        def check(detail):
            if detail["processing_status"] != "failed":
                raise Notice("Обработка уже продолжается.")

        await self.versioned(account.user_id, self.interview_path(account, interview_id), "retry", check=check)
        self.follow(account.telegram_id)

    async def list_interviews(self, account):
        page = await self.request(account.user_id, "GET", "/interviews?limit=10")
        items = sorted(page["items"], key=lambda item: item["updated_at"], reverse=True)
        if not items:
            return await self.send(account.chat_id, "Интервью пока нет.", [[("Начать интервью", "new")]])
        await self.send(account.chat_id, "<b>Мои интервью</b>\nВыберите интервью, чтобы продолжить его "
                                         "или открыть карточку.", [
            [(f"{(item['current_topic_title'] or 'Интервью')[:40]} · {STATUSES[item['status']]}",
              f"open:{item['id']}")] for item in items
        ])

    async def show_current(self, account, interview_id=None):
        interview_id = interview_id or account.state.get("interview_id")
        if not interview_id:
            raise Notice(NO_INTERVIEW)
        detail = await self.request(account.user_id, "GET", f"/interviews/{interview_id}")
        account = await self.set_state(account, interview_id=detail["id"],
                                       seen=max((item["sequence"] for item in detail["messages"]), default=0))
        title = escape(detail["current_topic_title"] or "Интервью")
        await self.send(account.chat_id, f"<b>{title}</b> · {STATUSES[detail['status']]}")
        if processing(detail):
            self.follow(account.telegram_id)
        elif detail["processing_status"] == "failed":
            await self.send_failure(account, detail)
        elif detail["card_id"]:
            await self.send_card(account, detail["card_id"])
        elif question := next((item for item in detail["messages"]
                               if item["id"] == detail["current_question_id"]), None):
            await self.send_question(account, detail, question)

    def follow(self, telegram_id):
        self.pending.add(telegram_id)
        task = self.followers.get(telegram_id)
        if task is None or task.done():
            self.followers[telegram_id] = self.spawn(self.watch(telegram_id))

    async def watch(self, telegram_id):
        # A follow() request during a pass schedules one more pass.
        while telegram_id in self.pending:
            self.pending.discard(telegram_id)
            await self.track(telegram_id)

    async def track(self, telegram_id):
        """Deliver new interview messages until processing stops."""
        loop = asyncio.get_running_loop()
        deadline, typed = loop.time() + FOLLOW_SECONDS, 0.0
        chat_id = status = label = None
        try:
            while True:
                account = await asyncio.to_thread(load_account, self.sessions, telegram_id)
                interview_id = account and account.state.get("interview_id")
                if not interview_id:
                    return
                chat_id = account.chat_id
                detail = await self.request(account.user_id, "GET", f"/interviews/{interview_id}")
                await self.deliver(account, detail)
                if not processing(detail):
                    if detail["processing_status"] == "failed":
                        await self.send_failure(account, detail, repeat=False)
                    elif detail["card_id"] and account.state.get("card_sent") != detail["card_id"]:
                        await self.send_card(account, detail["card_id"])
                    return
                text = f"⏳ {STAGES.get(detail['processing_stage'], 'Обрабатываю интервью')}…"
                if label is None:
                    status = await self.send(chat_id, text)
                elif text != label and status:
                    await self.quietly("editMessageText", chat_id=chat_id, message_id=status["message_id"], text=text)
                label = text
                if loop.time() - typed >= 4:
                    await self.quietly("sendChatAction", chat_id=chat_id, action="typing")
                    typed = loop.time()
                if loop.time() > deadline:
                    await self.send(chat_id, "Обработка идёт дольше обычного. Проверьте позже командой /status.")
                    return
                await asyncio.sleep(self.poll_seconds)
        except ApiFailure as exc:
            await self.send(chat_id, escape(str(exc)))
        except Exception:
            logger.exception("Telegram follower failed")
        finally:
            if status:
                await self.quietly("deleteMessage", chat_id=chat_id, message_id=status["message_id"])

    async def deliver(self, account, detail):
        fresh = [item for item in detail["messages"]
                 if item["sequence"] > account.state.get("seen", 0) and item["status"] != "draft"]
        if not fresh:
            return
        for item in fresh:
            if item["kind"] == "control" and item["text"] not in OWN_CONTROLS:
                await self.send(account.chat_id, f"<i>{escape(item['text'])}</i>")
            elif item["kind"] == "question" and item["id"] == detail["current_question_id"]:
                await self.send_question(account, detail, item)
        await asyncio.to_thread(save_state, self.sessions, account.telegram_id,
                                {"seen": fresh[-1]["sequence"]}, detail["id"])

    async def send_question(self, account, detail, question):
        text = escape(question["text"])
        source = question.get("source_quote") or question.get("vacancy_quote")
        if source:
            text += (f"\n\n<blockquote expandable>Источник уточнения: {escape(source['quote'])}\n"
                     f"<a href=\"{html.escape(source['url'])}\">{escape(source['title'])}</a></blockquote>")
        await self.send(account.chat_id, text, [[
            ("Пропустить", f"skip:{question['id']}"), ("Завершить интервью", f"finish:{detail['id']}"),
        ]])

    async def send_failure(self, account, detail, repeat=True):
        if not repeat and account.state.get("failed_version") == detail["version"]:
            return
        await asyncio.to_thread(save_state, self.sessions, account.telegram_id,
                                {"failed_version": detail["version"]}, detail["id"])
        error = detail.get("error") or {}
        text = f"Обработка прервалась. {PROCESSING_ERRORS.get(error.get('code'), '')}".strip()
        buttons = [[("Повторить", f"retry:{detail['id']}")]] if error.get("retryable") else None
        await self.send(account.chat_id, escape(text), buttons)

    async def send_card(self, account, card_id):
        card = await self.request(account.user_id, "GET", f"/cards/{card_id}")
        await self.set_state(account, card_sent=card_id)
        for index, part in enumerate(chunks(card["text"])):
            await self.send(account.chat_id, ("<b>Карточка интервью</b>\n\n" if index == 0 else "") + escape(part))
        review = card.get("review") or {}
        if card["publication_status"] == "in_review":
            return await self.send(account.chat_id, "Карточка на проверке у редактора.")
        if card["publication_status"] == "published":
            return await self.send(account.chat_id, "Карточка опубликована в каталоге.")
        note = "Карточка приватная: её видите только вы."
        if card["review_status"] == "needs_changes" and review.get("comment"):
            note = f"Редактор вернул карточку: {escape(review['comment'])}"
        await self.send(account.chat_id, f"{note}\n\nКарточку можно отправить редактору. После проверки "
                                         "она появится в публичном каталоге.",
                        [[("Отправить редактору", f"publish:{card_id}")]])

    async def confirm_publish(self, account, card_id):
        await self.send(account.chat_id, "Отправляя карточку, вы соглашаетесь на её публикацию в каталоге после "
                                         "проверки редактором. Подписать карточку?", [
            [("Без имени", f"publish-anon:{card_id}")],
            [("С именем или псевдонимом", f"publish-name:{card_id}")],
        ])

    async def publish(self, account, card_id, author_name=None):
        if not card_id:
            raise Notice("Откройте карточку через /interviews и отправьте её снова.")

        def check(card):
            if card["publication_status"] != "draft":
                raise Notice("Карточка уже отправлена редактору.")

        body = {"consent": True, "show_author": author_name is not None}
        if author_name:
            body["author_name"] = author_name
        await self.versioned(account.user_id, f"/cards/{card_id}", "submit-review", body, check=check)
        await self.send(account.chat_id, "Карточка отправлена редактору. После проверки она появится в каталоге.")


async def run_bot(app, settings):
    bot = TelegramBot(app, settings)
    try:
        await bot.run()
    finally:
        await bot.close()
