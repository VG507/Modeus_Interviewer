import asyncio
import logging
from datetime import timedelta
from time import time

from sqlalchemy import delete
from sqlalchemy.exc import SQLAlchemyError

from backend.db import CommandReceipt, LoginSession, LoginWindow, TelegramInvite, now


logger = logging.getLogger(__name__)
RECEIPT_RETENTION = timedelta(days=7)
LOGIN_WINDOW_SECONDS = 60


def current_login_window():
    return int(time()) // LOGIN_WINDOW_SECONDS


def prune_expired(sessions):
    current = now()
    with sessions.begin() as db:
        db.execute(delete(LoginSession).where(LoginSession.expires_at <= current))
        db.execute(delete(TelegramInvite).where(TelegramInvite.expires_at <= current))
        db.execute(delete(CommandReceipt).where(CommandReceipt.created_at < current - RECEIPT_RETENTION))
        db.execute(delete(LoginWindow).where(LoginWindow.window < current_login_window() - 1440))


async def schedule_pruning(sessions):
    while True:
        try:
            await asyncio.to_thread(prune_expired, sessions)
        except SQLAlchemyError as exc:
            logger.warning("Session maintenance database error: %s", type(exc).__name__)
        await asyncio.sleep(3600)
