"""MAX transport for the shared interview bot workflow."""

import asyncio
import logging
import ssl

import httpx

from backend.db import MaxAccount
from backend.telegram_bot import TelegramBot, TelegramError, followed_accounts

logger = logging.getLogger(__name__)
API_URL = "https://platform-api2.max.ru"


class MaxBot(TelegramBot):
    account_model = MaxAccount
    channel = "MAX"
    no_access = "Нажмите «Старт» или отправьте /start в личном чате с ботом."
    # Voice answers are recognised in Telegram only.
    unsupported = "Я понимаю только текстовые сообщения."

    def signup_email(self, user_id, code):
        if not code:
            return f"max-{user_id}@max.invalid"
        return None

    def __init__(self, app, settings, *, transport=None):
        tls = ssl.create_default_context()
        if settings.max_ca_file:
            tls.load_verify_locations(cafile=settings.max_ca_file)
        client = httpx.AsyncClient(
            base_url=API_URL, headers={"Authorization": settings.max_bot_token.strip()},
            verify=tls, timeout=httpx.Timeout(15, read=40), transport=transport,
        )
        super().__init__(app, settings, client=client)

    async def max_request(self, method, path, *, params=None, body=None):
        response = await self.client.request(method, path, params=params, json=body)
        try:
            result = response.json()
        except ValueError:
            raise TelegramError(response.status_code, "Invalid MAX response") from None
        if not response.is_success or not isinstance(result, dict) or result.get("success") is False:
            raise TelegramError(response.status_code, "MAX request failed")
        return result

    async def send(self, chat_id, text, buttons=None):
        body = {"text": text.replace("<blockquote expandable>", "<blockquote>"), "format": "html"}
        if buttons:
            rows = [[{"type": "callback", "text": label, "payload": data} for label, data in row] for row in buttons]
            body["attachments"] = [{"type": "inline_keyboard", "payload": {"buttons": rows}}]
        try:
            result = await self.max_request("POST", "/messages",
                                            params={"user_id": chat_id, "disable_link_preview": True}, body=body)
        except (TelegramError, httpx.HTTPError) as exc:
            logger.warning("MAX message failed: %s", getattr(exc, "status", type(exc).__name__))
            return None
        message_id = result.get("message", {}).get("body", {}).get("mid")
        return {"message_id": message_id} if message_id else None

    async def call(self, method, **params):
        if method == "answerCallbackQuery" and params.get("callback_query_id"):
            return await self.max_request("POST", "/answers", params={"callback_id": params["callback_query_id"]},
                                          body={"notification": "Принято"})
        if method == "deleteMessage":
            return await self.max_request("DELETE", "/messages", params={"message_id": params["message_id"]})
        if method == "editMessageText":
            return await self.max_request("PUT", "/messages", params={"message_id": params["message_id"]},
                                          body={"text": params["text"], "format": "html"})
        if method == "editMessageReplyMarkup":
            return await self.max_request("PUT", "/messages", params={"message_id": params["message_id"]},
                                          body={"attachments": []})
        # Progress messages already show the processing stage in MAX.
        return None

    async def dispatch(self, update):
        kind = update.get("update_type")
        if kind == "bot_started":
            user = update.get("user") or {}
            message = {"text": "/start " + (update.get("payload") or "")}
            query = None
        else:
            event_message = update.get("message") or {}
            recipient = event_message.get("recipient") or {}
            if recipient.get("chat_type") != "dialog":
                return
            if kind == "message_created":
                user = event_message.get("sender") or {}
                message = {"text": (event_message.get("body") or {}).get("text")}
                query = None
            elif kind == "message_callback":
                callback = update.get("callback") or {}
                user = callback.get("user") or {}
                message = None
                query = {"id": callback.get("callback_id"), "data": callback.get("payload"),
                         "message": {"message_id": (event_message.get("body") or {}).get("mid")}}
            else:
                return
        user_id = user.get("user_id")
        if type(user_id) is not int or user.get("is_bot"):
            return
        # Send by user ID so interview content always stays in a private dialog.
        async with self.lock(user_id):
            await self.guarded(user_id, self.handle(user_id, user_id, message, query))

    async def run(self):
        for user_id in await asyncio.to_thread(followed_accounts, self.sessions, account_model=self.account_model):
            self.follow(user_id)
        marker = None
        connected = False
        while True:
            params = {"timeout": 25}
            if marker is not None:
                params["marker"] = marker
            try:
                result = await self.max_request("GET", "/updates", params=params)
                updates = result.get("updates")
                if not isinstance(updates, list):
                    raise TelegramError(200, "Invalid MAX updates")
            except TelegramError as exc:
                if exc.status == 401:
                    logger.error("MAX rejected MAX_BOT_TOKEN")
                    return
                if exc.status == 405:
                    logger.error("MAX polling is unavailable. Check whether a webhook is already registered.")
                    return
                logger.warning("MAX polling failed: HTTP %s", exc.status)
                await asyncio.sleep(5)
                continue
            except httpx.HTTPError as exc:
                logger.warning("MAX polling failed: %s", type(exc).__name__)
                await asyncio.sleep(5)
                continue
            if not connected:
                logger.info("MAX long polling is connected")
                connected = True
            for update in updates:
                if isinstance(update, dict):
                    await self.dispatch(update)
            marker = result.get("marker", marker)


async def run_max_bot(app, settings):
    try:
        bot = MaxBot(app, settings)
    except OSError:
        logger.error("MAX could not load the TLS certificate file configured in MAX_CA_FILE")
        return
    try:
        await bot.run()
    finally:
        await bot.close()
