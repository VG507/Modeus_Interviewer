from time import time

from fastapi import HTTPException, Request
from sqlalchemy import case, or_

from backend.db import LoginSession, LoginWindow, User, insert_for, now
from backend.maintenance import LOGIN_WINDOW_SECONDS
from backend.security import token_hash


def limit_login(sessions, request):
    address = request.client.host if request.client else "unknown"
    stamp = int(time())
    window = stamp // LOGIN_WINDOW_SECONDS
    with sessions.begin() as db:
        counter = insert_for(db, LoginWindow).values(client=token_hash(address), window=window, attempts=1)
        result = db.execute(counter.on_conflict_do_update(
            index_elements=[LoginWindow.client],
            set_={"window": window, "attempts": case(
                (LoginWindow.window < window, 1), else_=LoginWindow.attempts + 1,
            )},
            where=or_(LoginWindow.window < window, LoginWindow.attempts < 15),
        ))
    if not result.rowcount:
        retry_after = LOGIN_WINDOW_SECONDS - stamp % LOGIN_WINDOW_SECONDS
        raise HTTPException(429, "LOGIN_RATE_LIMIT", headers={"Retry-After": str(retry_after)})


def current_user(request: Request):
    """Shared dependency for interview and card APIs."""
    token = request.cookies.get("modeus_session")
    if not token:
        raise HTTPException(401, "UNAUTHENTICATED")
    stamp = now()
    with request.app.state.sessions() as db:
        session = db.get(LoginSession, token_hash(token))
        if not session or session.expires_at <= stamp:
            raise HTTPException(401, "UNAUTHENTICATED")
        user = db.get(User, session.user_id)
        if not user:
            raise HTTPException(401, "UNAUTHENTICATED")
        if user.locked_until and user.locked_until > stamp:
            raise HTTPException(401, "ACCOUNT_TEMPORARILY_LOCKED")
        return {"id": user.id, "is_editor": user.is_editor}
