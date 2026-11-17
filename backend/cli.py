import argparse
import asyncio
import getpass
import json

from sqlalchemy import select

from backend.db import User, database
from backend.llm import check_llm
from backend.security import hash_password
from backend.settings import Settings


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    user = sub.add_parser("create-user")
    user.add_argument("email")
    user.add_argument("--editor", action="store_true")
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
    password = getpass.getpass("Password (12+ characters): ")
    if len(password) < 12 or len(password) > 256:
        parser.error("Password must contain 12–256 characters")
    if getpass.getpass("Repeat password: ") != password:
        parser.error("Passwords do not match")
    engine, sessions = database(settings.database_url)
    try:
        with sessions() as db:
            if db.scalar(select(User).where(User.email == email)):
                parser.error("User already exists")
            db.add(User(email=email, password_hash=hash_password(password), is_editor=args.editor))
            db.commit()
        print("User created")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
