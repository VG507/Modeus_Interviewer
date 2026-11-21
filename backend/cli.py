import argparse
import asyncio
import getpass
import json
from pathlib import Path

import httpx
from pydantic import ValidationError
from sqlalchemy import delete, select

from backend.agent_skills import collect_skills
from backend.db import LlmResponse, LoginSession, User, VacancyCollection, copy_database, database
from backend.discovery import candidate_output, collect, item_entry, list_candidates
from backend.discovery_fetch import CollectionError
from backend.documentation import collect_documentation
from backend.github_evidence import process_pending
from backend.hn import HackerNewsUnavailable, collect_hn
from backend.llm import check_llm
from backend.openalex import collect_research
from backend.security import hash_password
from backend.settings import Settings
from backend.source_context import import_excerpts
from backend.telegram_bot import INVITE_HOURS, create_invite, invite_link
from backend.vacancy_import import claim_collection, run_collection


DISCOVERY_COMMANDS = {"fetch-github": "github", "fetch-trends": "trends"}


def discovery_command(args, settings):
    engine, sessions = database(settings.database_url)
    try:
        if args.command == "fetch-skills":
            return collect_skills(sessions, settings, args.profession_id)
        if args.command == "fetch-openalex":
            return collect_research(sessions, settings, args.profession_id, overrides=args.query or ())
        if args.command == "fetch-hn":
            return collect_hn(sessions, args.profession_id, technologies=args.technology, feed_limit=args.feed_limit,
                              limit=args.limit, comment_limit=args.comment_limit)
        if args.command == "fetch-docs":
            with sessions() as db:
                if args.profession_id and db.get(VacancyCollection, args.profession_id) is None:
                    raise CollectionError("Unknown profession")
            return collect_documentation(sessions, args.profession_id)
        if args.command in DISCOVERY_COMMANDS:
            provider = DISCOVERY_COMMANDS[args.command]
            result = collect(sessions, settings, provider, args.profession_id, args.values, args.pages,
                             args.limit, getattr(args, "min_stars", 100), getattr(args, "label", None))
            if provider == "github":
                asyncio.run(process_pending(sessions, settings, profession=args.profession_id, limit=args.limit))
            return result
        with sessions() as db:
            if not args.id:
                return list_candidates(db, args.profession_id, args.limit)
            item, candidate = item_entry(db, args.profession_id, args.id)
            if item is None or candidate is None:
                raise CollectionError("Unknown discovery candidate")
            return candidate_output(db, item, candidate, include_text=True)
    finally:
        engine.dispose()


def number_between(minimum, maximum):
    def parsed(value):
        number = int(value)
        if not minimum <= number <= maximum:
            raise argparse.ArgumentTypeError(f"expected a number between {minimum} and {maximum}")
        return number
    return parsed


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    user = sub.add_parser("create-user")
    user.add_argument("email")
    user.add_argument("--editor", action="store_true")
    reset = sub.add_parser("reset-password", help="Set a new password and revoke existing sessions")
    reset.add_argument("email")
    invite = sub.add_parser("telegram-invite", help="Create a one-time link to the Telegram bot for a user")
    invite.add_argument("email")
    invite.add_argument("--hours", type=number_between(1, 720), default=INVITE_HOURS)
    sub.add_parser("check-llm")
    forget = sub.add_parser("clear-llm-cache", help="Forget stored model answers so the model is asked again")
    forget.add_argument("--model", help="Only answers of this model")
    copy = sub.add_parser("copy-database", help="Copy all rows from another database into the empty DATABASE_URL")
    copy.add_argument("source", help="Source database URL, e.g. sqlite:///./.work/modeus.db")
    collect = sub.add_parser("import-vacancies", help="Refresh HH vacancies for one profile")
    collect.add_argument("profession_id")
    collect.add_argument("--file", type=Path, help="Import a JSON array of saved detailed HH responses")
    sources = sub.add_parser("import-sources", help="Import reviewed source excerpts from a JSON file")
    sources.add_argument("file", type=Path)
    docs = sub.add_parser("fetch-docs", help="Refresh watched official documentation and release notes")
    docs.add_argument("profession_id", nargs="?")
    research = sub.add_parser("fetch-openalex", help="Collect and verify scholarly articles for one profession")
    research.add_argument("profession_id")
    research.add_argument("--query", action="append", help="Search by a practical task (up to four queries)")
    skills = sub.add_parser("fetch-skills", help="Collect and verify curated Agent Skills")
    skills.add_argument("profession_id")
    hn = sub.add_parser("fetch-hn", help="Collect IT stories and comments from Hacker News")
    hn.add_argument("profession_id", nargs="?")
    hn.add_argument("--technology", action="append", default=[], help="Add a technology to the selected profile")
    hn.add_argument("--feed-limit", type=number_between(1, 150), default=50, help="Maximum IDs from each feed")
    hn.add_argument("--limit", type=number_between(1, 30), default=10, help="Maximum stored stories")
    hn.add_argument("--comment-limit", type=number_between(1, 80), default=40, help="Maximum comment lookups per story")
    for name, option in (("fetch-github", "--repo"), ("fetch-trends", "--topic")):
        fetch_parser = sub.add_parser(name)
        fetch_parser.add_argument("profession_id")
        fetch_parser.add_argument(option, dest="values", action="append")
        fetch_parser.add_argument("--pages", type=number_between(1, 10), default=2)
        fetch_parser.add_argument("--limit", type=number_between(1, 100), default=20)
        if name == "fetch-github":
            fetch_parser.add_argument("--label", action="append")
        if name == "fetch-trends":
            fetch_parser.add_argument("--min-stars", type=number_between(1, 1_000_000), default=100)
    listing = sub.add_parser("list-discovery")
    listing.add_argument("profession_id")
    listing.add_argument("--id")
    listing.add_argument("--limit", type=number_between(1, 100), default=30)
    args = parser.parse_args()
    settings = Settings()
    if args.command in DISCOVERY_COMMANDS or args.command in {
        "fetch-docs", "fetch-hn", "fetch-openalex", "fetch-skills", "list-discovery",
    }:
        try:
            result = discovery_command(args, settings)
            print(json.dumps(result, ensure_ascii=True, default=str))
            if args.command in {"fetch-openalex", "fetch-skills"} and result["status"] in {"failed", "skipped"}:
                raise SystemExit(1)
        except HackerNewsUnavailable as exc:
            parser.exit(1, f"fetch-hn: {exc}\n")
        except (CollectionError, OSError) as exc:
            parser.error(str(exc))
        except (httpx.HTTPError, ValidationError, KeyError, TypeError, ValueError, RuntimeError) as exc:
            parser.error(f"Discovery failed: {type(exc).__name__}")
        return
    if args.command == "check-llm":
        try:
            print(json.dumps(asyncio.run(check_llm(settings))))
        except Exception as exc:
            # No exception body from HTTP clients: URLs may include credentials.
            print(str(exc) if type(exc) is RuntimeError else type(exc).__name__)
            raise SystemExit(1) from None
        return
    if args.command == "copy-database":
        try:
            print(json.dumps(copy_database(args.source, settings.database_url)))
        except RuntimeError as exc:
            parser.exit(1, f"copy-database: {exc}\n")
        return
    if args.command == "clear-llm-cache":
        engine, sessions = database(settings.database_url)
        try:
            with sessions.begin() as db:
                query = delete(LlmResponse)
                if args.model:
                    query = query.where(LlmResponse.model == args.model)
                count = db.execute(query).rowcount
        finally:
            engine.dispose()
        print(json.dumps({"deleted": count}))
        return
    if args.command == "telegram-invite":
        engine, sessions = database(settings.database_url)
        try:
            code = create_invite(sessions, args.email.strip().lower(), args.hours)
        finally:
            engine.dispose()
        if code is None:
            parser.error("User not found")
        print(json.dumps({"start": f"/start {code}", "link": invite_link(settings, code)}))
        return
    if args.command == "import-sources":
        engine, sessions = database(settings.database_url)
        try:
            with sessions.begin() as db:
                count = import_excerpts(db, json.loads(args.file.read_text(encoding="utf-8-sig")))
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
        finally:
            engine.dispose()
        print(json.dumps({"imported": count}))
        return
    if args.command == "import-vacancies":
        engine, sessions = database(settings.database_url)
        try:
            with sessions() as db:
                if not db.scalar(select(VacancyCollection.profession_id).where(
                    VacancyCollection.profession_id == args.profession_id
                )):
                    parser.error("PROFILE_NOT_FOUND")
            token = claim_collection(sessions, args.profession_id)
            if token is None:
                parser.error("HH_ALREADY_RUNNING")
            result = run_collection(sessions, settings, args.profession_id, token, args.file)
            print(json.dumps(result, ensure_ascii=False))
            if result["status"] != "ok":
                raise SystemExit(1)
        finally:
            engine.dispose()
        return
    email = args.email.strip().lower()
    if "@" not in email or len(email) > 254:
        parser.error("Invalid email")
    local_setup = (
        settings.database_url.startswith("sqlite:///")
        and bool(settings.app_origins)
        and settings.app_origins <= {"http://localhost:5173", "http://127.0.0.1:5173"}
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
