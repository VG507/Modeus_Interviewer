import asyncio
import secrets
from contextlib import suppress
from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field
from sqlalchemy import select

from backend.db import LoginSession, User, database, now
from backend.auth import current_user
from backend.interview_api import router as interview_router
from backend.interview_worker import run_worker
from backend.security import check_password, hash_password, token_hash
from backend.settings import Settings


class LoginBody(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=256)


def create_app(settings: Settings | None = None):
    settings = settings or Settings()
    engine, sessions = database(settings.database_url)
    dummy_hash = hash_password(secrets.token_urlsafe(24))

    @asynccontextmanager
    async def lifespan(app):
        worker = asyncio.create_task(run_worker(sessions, settings))
        try:
            yield
        finally:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker
            engine.dispose()

    app = FastAPI(title="Modeus MVP", version="0.1.0", lifespan=lifespan)
    app.state.engine = engine
    app.state.sessions = sessions
    app.state.app_origin = settings.app_origin
    app.include_router(interview_router)

    @app.exception_handler(HTTPException)
    async def error_handler(request, exc):
        detail = exc.detail if isinstance(exc.detail, dict) else {"code": exc.detail, "message": exc.detail}
        return JSONResponse(status_code=exc.status_code, headers=exc.headers, content={"error": {
            "retryable": False, "request_id": secrets.token_hex(8), **detail,
        }})

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request, exc):
        return JSONResponse(status_code=422, content={"error": {
            "code": "VALIDATION_ERROR", "message": "Invalid request data",
            "retryable": False, "request_id": secrets.token_hex(8),
        }})

    def require_origin(request):
        if request.headers.get("origin") != settings.app_origin:
            raise HTTPException(403, "ORIGIN_REJECTED")

    @app.get("/api/v1/health")
    def health():
        try:
            with sessions() as db:
                db.execute(select(User.id).limit(1))
            return {"status": "ok", "database": "connected"}
        except Exception:
            raise HTTPException(503, "DATABASE_UNAVAILABLE") from None

    @app.post("/api/v1/auth/login")
    def login(body: LoginBody, request: Request, response: Response):
        require_origin(request)
        with sessions() as db:
            user = db.scalar(select(User).where(User.email == body.email.strip().lower()))
            valid = check_password(body.password, user.password_hash if user else dummy_hash)
            if user and user.locked_until and user.locked_until > now():
                raise HTTPException(429, "LOGIN_TEMPORARILY_LOCKED")
            if not user or not valid:
                if user:
                    user.failed_logins += 1
                    if user.failed_logins >= 5:
                        user.locked_until = now() + timedelta(minutes=15)
                    db.commit()
                raise HTTPException(401, "INVALID_CREDENTIALS")
            user.failed_logins = 0
            user.locked_until = None
            token = secrets.token_urlsafe(32)
            db.add(LoginSession(token_hash=token_hash(token), user_id=user.id,
                                expires_at=now() + timedelta(hours=settings.session_hours)))
            db.commit()
            response.set_cookie("modeus_session", token, httponly=True, secure=settings.cookie_secure,
                                samesite="lax", path="/", max_age=settings.session_hours * 3600)
            return {"id": user.id, "is_editor": user.is_editor}

    @app.get("/api/v1/me")
    def me(user=Depends(current_user)):
        return user

    @app.post("/api/v1/auth/logout", status_code=204)
    def logout(request: Request):
        require_origin(request)
        with sessions() as db:
            session = db.get(LoginSession, token_hash(request.cookies.get("modeus_session", "")))
            if session:
                db.delete(session)
                db.commit()
        response = Response(status_code=204)
        response.delete_cookie("modeus_session", path="/")
        return response

    return app
