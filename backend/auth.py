from fastapi import HTTPException, Request
from backend.db import LoginSession, User, now
from backend.security import token_hash


def current_user(request: Request):
    """Shared dependency for interview and card APIs."""
    with request.app.state.sessions() as db:
        session = db.get(LoginSession, token_hash(request.cookies.get("modeus_session", "")))
        if not session or session.expires_at <= now():
            raise HTTPException(401, "UNAUTHENTICATED")
        user = db.get(User, session.user_id)
        if not user:
            raise HTTPException(401, "UNAUTHENTICATED")
        return {"id": user.id, "is_editor": user.is_editor}
