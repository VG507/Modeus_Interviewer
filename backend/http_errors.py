import logging
import secrets
from http import HTTPStatus

from fastapi.responses import JSONResponse


logger = logging.getLogger(__name__)
MESSAGES = {
    "ACCOUNT_TEMPORARILY_LOCKED": "Аккаунт временно заблокирован.",
    "ACTIVE_INTERVIEW_LIMIT": "Завершите одно из активных интервью, чтобы начать новое.",
    "ANSWER_NOT_FOUND": "Ответ не найден.",
    "CARD_NOT_FOUND": "Карточка не найдена.",
    "CARD_UNCHANGED": "В карточке нет изменений.",
    "CURSOR_INVALID": "Откройте первую страницу списка заново.",
    "DATABASE_UNAVAILABLE": "База данных временно недоступна.",
    "DISCOVERY_BUSY": "Сбор из этого источника уже выполняется.",
    "DISCOVERY_ITEM_NOT_FOUND": "Источник для этого профиля не найден.",
    "DISCOVERY_QUERY_INVALID": "Проверьте параметры сбора источников.",
    "DISCOVERY_RATE_LIMIT": "Источник временно ограничил запросы.",
    "DISCOVERY_TIMEOUT": "Источник не ответил вовремя.",
    "DISCOVERY_UNAVAILABLE": "Источник временно недоступен.",
    "DOCUMENTATION_QUOTE_INVALID": "Цитата или деталь не подтверждается текстом источника.",
    "DOCUMENTATION_REVISION_CHANGED": "Редакция источника изменилась.",
    "DOCUMENTATION_TARGET_INVALID": "Выберите источник из списка официальной документации.",
    "EDITOR_REQUIRED": "Для этого действия нужен доступ редактора.",
    "HH_ALREADY_RUNNING": "Сбор вакансий уже выполняется.",
    "HH_NOT_CONFIGURED": "Сбор вакансий не настроен.",
    "IDEMPOTENCY_CONFLICT": "Ключ запроса уже использован с другими данными.",
    "INTERNAL_SERVER_ERROR": "Внутренняя ошибка сервера.",
    "INTERVIEW_NOT_FOUND": "Интервью не найдено.",
    "INVALID_CREDENTIALS": "Проверьте почту и пароль.",
    "INVALID_STATE": "Действие недоступно в текущем состоянии.",
    "LOGIN_RATE_LIMIT": "Слишком много попыток входа. Повторите позже.",
    "LOGIN_TEMPORARILY_LOCKED": "Повторите попытку входа через 15 минут.",
    "ORIGIN_REJECTED": "Запрос отправлен с недопустимого адреса.",
    "PAYLOAD_TOO_LARGE": "Тело запроса превышает 2 МиБ.",
    "PROCESSING_IN_PROGRESS": "Ответ уже обрабатывается.",
    "PROFILE_EXISTS": "Профиль с таким названием уже существует.",
    "PROFILE_INVALID_TITLE": "Укажите название профессии.",
    "PROFILE_NOT_FOUND": "Профиль профессии не найден.",
    "PROFILE_REVIEW_NOT_READY": "Для подтверждения нужна свежая выборка вакансий.",
    "TOPIC_NOT_FOUND": "Тема больше недоступна.",
    "UNAUTHENTICATED": "Войдите в аккаунт.",
    "VALIDATION_ERROR": "Проверьте заполнение и формат полей.",
}


def error_response(status, detail, headers=None):
    if not isinstance(detail, dict):
        try:
            label = HTTPStatus(status).phrase
        except ValueError:
            label = "Request failed"
        code = detail if isinstance(detail, str) and detail in MESSAGES else label.upper().replace(" ", "_")
        detail = {"code": code, "message": MESSAGES.get(code, label)}
    return JSONResponse(status_code=status, headers=headers, content={"error": {
        "retryable": bool(headers and "Retry-After" in headers), "request_id": secrets.token_hex(8), **detail,
    }})


async def unhandled_error(request, exc):
    response = error_response(500, "INTERNAL_SERVER_ERROR")
    logger.error("Unhandled %s request error: %s", request.method, type(exc).__name__)
    return response


class LimitRequestBody:
    def __init__(self, app, limit=2 * 1024 * 1024):
        self.app = app
        self.limit = limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in {"POST", "PUT", "PATCH", "DELETE"}:
            return await self.app(scope, receive, send)
        for name, value in scope["headers"]:
            if name == b"content-length" and value.isdigit() and (
                len(value) > 10 or int(value) > self.limit
            ):
                return await error_response(413, "PAYLOAD_TOO_LARGE")(scope, receive, send)
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > self.limit:
                return await error_response(413, "PAYLOAD_TOO_LARGE")(scope, receive, send)
            body.extend(chunk)
            if not message.get("more_body", False):
                break

        async def read_body():
            nonlocal body
            if body is None:
                return await receive()
            message = {"type": "http.request", "body": bytes(body), "more_body": False}
            body = None
            return message

        await self.app(scope, read_body, send)
