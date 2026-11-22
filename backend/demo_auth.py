"""Provision a private participant/moderator pair and a sample review."""

import json
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select

from backend.db import CardEvent, InterviewSession, KnowledgeCard, Message, User
from backend.interview_schemas import OPEN_TOPIC_ID

SQL_FIXTURE = Path(__file__).resolve().parents[1] / "data" / "interview" / "sql_topic.json"


def card_owners(demo_group):
    return select(User.id).where(User.demo_group == demo_group)


def create_demo_pair(db):
    group = str(uuid4())
    participant = User(email=f"{group}-participant@demo.invalid", password_hash="", demo_group=group)
    moderator = User(email=f"{group}-moderator@demo.invalid", password_hash="", demo_group=group, is_editor=True)
    db.add_all([participant, moderator])
    db.flush()
    seed_review(db, participant.id)
    return participant, moderator


def seed_review(db, owner_id):
    fixture = json.loads(SQL_FIXTURE.read_text(encoding="utf-8"))
    source = fixture["sources"][1]
    interview = InterviewSession(owner_id=owner_id, topic_id=OPEN_TOPIC_ID, status="completed",
                                 context={"role": "Python-разработчик"})
    db.add(interview)
    db.flush()
    question = Message(session_id=interview.id, sequence=1, kind="question", status="processed",
                       text=fixture["opening_question"], market_sources=[{
                           "used": True, "title": "Python/Django-разработчик · hh.ru",
                           "url": source["url"], "task_excerpt": source["text"],
                       }])
    db.add(question)
    db.flush()
    task = "В отчёте по заказам SQL-запрос выполнялся около восьми секунд."
    action = "Я использовал EXPLAIN ANALYZE в PostgreSQL и обнаружил последовательное чтение таблицы."
    verification = "После добавления индекса я повторил замер на том же наборе данных: запрос занял 400 мс."
    answer = Message(session_id=interview.id, sequence=2, kind="answer", status="processed",
                     text=f"{task} {action} {verification}", reply_to_question_id=question.id)
    db.add(answer)
    db.flush()

    title = f"Демо · {fixture['topic']['title']}"
    facts = {section: [{"id": str(uuid4()), "text": quote, "source": "interview",
                        "supporting_message_ids": [answer.id],
                        **({"responsibility": "personal"} if section == "actions" else {})}]
             for section, quote in (("task", task), ("actions", action), ("verification", verification))}
    stamp = interview.created_at
    consent = {"content_version": 1, "actor_id": owner_id, "at": stamp.isoformat() + "Z",
               "show_author": False, "author_name": None, "share_evidence": True}
    card = KnowledgeCard(session_id=interview.id, owner_id=owner_id, origin="human", completion_version=1,
                         version=2, content_version=1, publication_status="in_review", first_submitted_at=stamp,
                         consent=consent, content={"title": title, **facts},
                         text=f"{title}\n\nЗадача\n{task}\n\nДействия\n{action}\n\nПроверка\n{verification}")
    db.add(card)
    db.flush()
    db.add(CardEvent(id=str(uuid4()), card_id=card.id, actor_id=owner_id, action="submit_review",
                     version=card.version, content_version=card.content_version, details=consent))
