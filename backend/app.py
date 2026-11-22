import asyncio
import secrets
from contextlib import asynccontextmanager, suppress
from datetime import timedelta
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field
from sqlalchemy import case, delete, or_, select, update
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend.db import LoginSession, User, database, now
from backend.auth import current_user, limit_login, session_user, user_view
from backend.card_api import router as card_router
from backend.discovery_api import router as discovery_router
from backend.demo_auth import create_demo_pair
from backend.documentation import schedule_documentation
from backend.documentation_evidence import schedule_documentation_evidence
from backend.embeddings import configured as embeddings_configured, schedule_card_index
from backend.github_evidence import schedule_github_evidence
from backend.http_errors import LimitRequestBody, error_response, unhandled_error
from backend.interview_api import router as interview_router
from backend.interview_worker import run_worker
from backend.maintenance import schedule_pruning
from backend.max_bot import run_max_bot
from backend.security import check_password, hash_password, token_hash
from backend.settings import Settings
from backend.telegram_bot import run_bot
from backend.vacancy_api import router as vacancy_router
from backend.vacancy_import import configured, schedule_collections


class LoginBody(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=256)


class DemoLoginBody(BaseModel):
    role: Literal["participant", "moderator"]
    switch: bool = False


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
        if settings.llm_base_url and settings.llm_model:
            workers.append(asyncio.create_task(schedule_documentation_evidence(sessions, settings)))
        workers.append(asyncio.create_task(schedule_github_evidence(sessions, settings)))
        if embeddings_configured(settings):
            workers.append(asyncio.create_task(schedule_card_index(sessions, settings)))
        if settings.telegram_bot_token:
            workers.append(asyncio.create_task(run_bot(app, settings)))
        if settings.max_bot_token.strip():
            workers.append(asyncio.create_task(run_max_bot(app, settings)))
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

    def issue_session(db, user, response):
        token = secrets.token_urlsafe(32)
        db.add(LoginSession(token_hash=token_hash(token), user_id=user.id,
                            expires_at=now() + timedelta(hours=settings.session_hours)))
        db.commit()
        response.headers["Cache-Control"] = "no-store"
        response.set_cookie("modeus_session", token, httponly=True, secure=settings.secure_cookie,
                            samesite="lax", path="/", max_age=settings.session_hours * 3600)
        return user_view(user)

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
            user = db.scalar(select(User).where(User.email == body.email.strip().lower(), User.demo_group.is_(None)))
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
            return issue_session(db, user, response)

    @app.get("/api/v1/auth/demo")
    def demo_status(response: Response):
        response.headers["Cache-Control"] = "no-store"
        return {"enabled": settings.demo_auth_enabled}

    @app.post("/api/v1/auth/demo")
    def demo_login(body: DemoLoginBody, request: Request, response: Response):
        if not settings.demo_auth_enabled:
            raise HTTPException(404, "DEMO_DISABLED")
        require_origin(request)
        limit_login(sessions, request)
        response.headers["Cache-Control"] = "no-store"
        token = request.cookies.get("modeus_session")
        moderator = body.role == "moderator"
        with sessions() as db:
            user = session_user(db, token, now())
            if user and user.demo_group:
                if user.is_editor == moderator:
                    return user_view(user)
                target = db.scalar(select(User).where(User.demo_group == user.demo_group, User.is_editor == moderator))
                revoked = db.execute(delete(LoginSession).where(
                    LoginSession.token_hash == token_hash(token),
                ).returning(LoginSession.token_hash)).scalar()
                if target is None or revoked is None:
                    raise HTTPException(401, "UNAUTHENTICATED")
                return issue_session(db, target, response)
            if body.switch:
                raise HTTPException(401, "UNAUTHENTICATED")
            participant, reviewer = create_demo_pair(db)
            return issue_session(db, reviewer if moderator else participant, response)

    @app.get("/api/v1/me")
    def me(response: Response, user=Depends(current_user)):
        response.headers["Cache-Control"] = "no-store"
        return {key: value for key, value in user.items() if key != "demo_group"}

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
