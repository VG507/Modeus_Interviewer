from sqlalchemy import func, select
from sqlalchemy.orm import aliased

from backend.db import InterviewSession, KnowledgeCard, Topic, VacancyCollection
from backend.interview_schemas import OPEN_TOPIC_ID
from backend.vacancy_parser import profile_matches


def catalog_topic_columns(query, *columns):
    active = aliased(Topic)
    return (query.add_columns(*columns, func.coalesce(Topic.id, active.id).label("topic_id"),
                              func.coalesce(Topic.title, active.title).label("topic_title"),
                              func.coalesce(Topic.profession_id, active.profession_id).label("topic_profession"))
            .outerjoin(InterviewSession, InterviewSession.id == KnowledgeCard.session_id)
            .outerjoin(Topic, (Topic.id == InterviewSession.topic_id) & (Topic.id != OPEN_TOPIC_ID))
            .outerjoin(active, (active.id == InterviewSession.case_state["active_topic_id"].as_string()) &
                       (active.id != OPEN_TOPIC_ID)))


def catalog_profiles(db):
    return db.execute(select(VacancyCollection.profession_id, VacancyCollection.title, VacancyCollection.aliases)).all()


def resolve_catalog_profession(topic_profession, role, profiles):
    if topic_profession:
        return topic_profession
    if not isinstance(role, str) or not role.strip():
        return "unknown"
    matches = [profile for profile in profiles if profile_matches(profile.profession_id, role, profile)]
    matches = [profile for profile in matches if profile.aliases is not None] or matches
    return matches[0].profession_id if len(matches) == 1 else "unknown"


def profession_for_card(db, card_id):
    row = db.execute(catalog_topic_columns(select(KnowledgeCard.id), InterviewSession.context["role"].as_string())
                     .where(KnowledgeCard.id == card_id)).first()
    return resolve_catalog_profession(row.topic_profession, row[1], catalog_profiles(db))
