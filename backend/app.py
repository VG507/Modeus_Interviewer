import asyncio
import secrets
from contextlib import asynccontextmanager, suppress
from datetime import timedelta

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field
from sqlalchemy import case, delete, or_, select, update
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend.db import LoginSession, User, database, now
from backend.auth import current_user, limit_login
from backend.card_api import router as card_router
from backend.discovery_api import router as discovery_router
from backend.documentation import schedule_documentation
from backend.embeddings import configured as embeddings_configured, schedule_card_index
from backend.github_evidence import schedule_github_evidence
from backend.http_errors import LimitRequestBody, error_response, unhandled_error
from backend.interview_api import router as interview_router
from backend.interview_worker import run_worker
from backend.maintenance import schedule_pruning
from backend.security import check_password, hash_password, token_hash
from backend.settings import Settings
from backend.telegram_bot import run_bot
from backend.vacancy_api import router as vacancy_router
from backend.vacancy_import import configured, schedule_collections


class LoginBody(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=256)


def create_app(settings: Settings | None = None):
    settings = settings or Settings()
    engine, sessions = database(settings.database_url)
    dummy_hash = hash_password(secrets.token_urlsafe(24))

    @asynccontextmanager
    async def lifespan(app):
        workers = [asyncio.create_task(run_worker(sessions, settings)), asyncio.create_task(schedule_pruning(sessions))]
        if settings.hh_weekly_enabled and configured(settings):
            workers.append(asyncio.create_task(schedule_collections(sessions, settings)))
        if settings.documentation_weekly_enabled:
            workers.append(asyncio.create_task(schedule_documentation(sessions)))
        workers.append(asyncio.create_task(schedule_github_evidence(sessions, settings)))
        if embeddings_configured(settings):
            workers.append(asyncio.create_task(schedule_card_index(sessions, settings)))
        if settings.telegram_bot_token:
            workers.append(asyncio.create_task(run_bot(app, settings)))
        try:
            yield
        finally:
            for worker in workers:
                worker.cancel()
            for worker in workers:
                with suppress(asyncio.CancelledError):
                    await worker
            engine.dispose()

    app = FastAPI(title="Modeus MVP", version="0.1.0", lifespan=lifespan,
                  openapi_url="/openapi.json" if settings.local_docs else None)
    app.add_middleware(LimitRequestBody)
    app.add_exception_handler(Exception, unhandled_error)
    app.state.engine = engine
    app.state.sessions = sessions
    app.state.settings = settings
    app.state.app_origin = next(iter(settings.origin_list), "")
    app.state.app_origins = settings.app_origins
    app.include_router(interview_router)
    app.include_router(card_router)
    app.include_router(vacancy_router)
    app.include_router(discovery_router)

    @app.exception_handler(StarletteHTTPException)
    async def error_handler(request, exc):
        return error_response(exc.status_code, exc.detail, exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request, exc):
        return error_response(422, "VALIDATION_ERROR")

    def require_origin(request):
        if request.headers.get("origin") not in app.state.app_origins:
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
        limit_login(sessions, request)
        with sessions() as db:
            user = db.scalar(select(User).where(User.email == body.email.strip().lower()))
            stamp = now()
            if user and user.locked_until and user.locked_until > stamp:
                raise HTTPException(429, "LOGIN_TEMPORARILY_LOCKED")
            valid = check_password(body.password, user.password_hash if user else dummy_hash)
            if not user:
                raise HTTPException(401, "INVALID_CREDENTIALS")
            unlocked = update(User).where(
                User.id == user.id, User.password_hash == user.password_hash,
                or_(User.locked_until.is_(None), User.locked_until <= stamp),
            ).execution_options(synchronize_session=False)
            if not valid:
                attempts = case((User.locked_until.is_not(None), 1), else_=User.failed_logins + 1)
                lock = case((attempts >= 5, stamp + timedelta(minutes=15)), else_=None)
                locked_until = db.execute(unlocked.values(failed_logins=attempts, locked_until=lock)
                                          .returning(User.locked_until)).scalar()
                if locked_until is not None:
                    db.execute(delete(LoginSession).where(LoginSession.user_id == user.id))
                db.commit()
                raise HTTPException(401, "INVALID_CREDENTIALS")
            if not db.execute(unlocked.values(failed_logins=0, locked_until=None)).rowcount:
                raise HTTPException(401, "INVALID_CREDENTIALS")
            token = secrets.token_urlsafe(32)
            db.add(LoginSession(token_hash=token_hash(token), user_id=user.id,
                                expires_at=now() + timedelta(hours=settings.session_hours)))
            db.commit()
            response.set_cookie("modeus_session", token, httponly=True, secure=settings.secure_cookie,
                                samesite="lax", path="/", max_age=settings.session_hours * 3600)
            return {"id": user.id, "is_editor": user.is_editor}

    @app.get("/api/v1/me")
    def me(user=Depends(current_user)):
        return user

    @app.post("/api/v1/auth/logout", status_code=204)
    def logout(request: Request):
        require_origin(request)
        token = request.cookies.get("modeus_session")
        if token:
            with sessions.begin() as db:
                db.execute(delete(LoginSession).where(LoginSession.token_hash == token_hash(token)))
        response = Response(status_code=204)
        response.delete_cookie("modeus_session", path="/", secure=settings.secure_cookie, httponly=True)
        return response

    return app
