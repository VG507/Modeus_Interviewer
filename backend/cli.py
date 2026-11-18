import argparse
import asyncio
import getpass
import json

from sqlalchemy import delete, select

from backend.db import LoginSession, User, database
from backend.llm import check_llm
from backend.security import hash_password
from backend.settings import Settings


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    user = sub.add_parser("create-user")
    user.add_argument("email")
    user.add_argument("--editor", action="store_true")
    reset = sub.add_parser("reset-password", help="Set a new password and revoke existing sessions")
    reset.add_argument("email")
    sub.add_parser("check-llm")
    args = parser.parse_args()
    settings = Settings()
    if args.command == "check-llm":
        try:
            print(json.dumps(asyncio.run(check_llm(settings))))
        except Exception as exc:
            # No exception body from HTTP clients: URLs may include credentials.
            print(str(exc) if type(exc) is RuntimeError else type(exc).__name__)
            raise SystemExit(1) from None
        return
    email = args.email.strip().lower()
    if "@" not in email or len(email) > 254:
        parser.error("Invalid email")
    local_setup = (
        settings.database_url.startswith("sqlite:///")
        and settings.app_origin in {"http://localhost:5173", "http://127.0.0.1:5173"}
        and not settings.cookie_secure
    )
    minimum_length = 4 if local_setup else 12
    password = getpass.getpass(f"Password ({minimum_length}+ characters): ")
    if not minimum_length <= len(password) <= 256:
        parser.error(f"Password must contain {minimum_length}-256 characters")
    if getpass.getpass("Repeat password: ") != password:
        parser.error("Passwords do not match")
    engine, sessions = database(settings.database_url)
    try:
        with sessions() as db:
            account = db.scalar(select(User).where(User.email == email))
            if args.command == "reset-password":
                if account is None:
                    parser.error("User not found")
                account.password_hash = hash_password(password)
                account.failed_logins = 0
                account.locked_until = None
                db.execute(delete(LoginSession).where(LoginSession.user_id == account.id))
            else:
                if account is not None:
                    parser.error("User already exists")
                db.add(User(email=email, password_hash=hash_password(password), is_editor=args.editor))
            db.commit()
        print("Password reset" if args.command == "reset-password" else "User created")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
