"""Read vacancy samples while keeping each source's date and configuration."""

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import defer

from backend.db import VacancySnapshot, VacancySourceSample


FRESH_FOR = timedelta(days=14)


def trudvsem_samples(db, profession_id=None, *, include_cards=True):
    query = select(VacancySnapshot).join(VacancySourceSample, VacancySourceSample.snapshot_id == VacancySnapshot.id).where(
        VacancySourceSample.source == "trudvsem",
    )
    if profession_id is not None:
        query = query.where(VacancySourceSample.profession_id == profession_id)
    if not include_cards:
        query = query.options(defer(VacancySnapshot.cards))
    return {sample.profession_id: sample for sample in db.scalars(query)}


def sample_is_fresh(stamp, current):
    return bool(stamp and current - stamp <= FRESH_FOR)


def current_sample(stats, profile):
    return stats.get("config_version", 1) == profile.config_version


def vacancy_review(cards, approved_profile):
    grounded = [
        card for card in cards if card.get("tasks") and any(
            evidence.get("field_path") == f"tasks.{index}" and evidence.get("quote") == task
            for index, task in enumerate(card["tasks"]) for evidence in card.get("evidence", [])
        )
    ]
    employers = len({card["employer_id"] for card in grounded if card.get("employer_id")})
    adequate = len(grounded) >= 5 and employers >= 3 and 5 * len(grounded) >= 4 * len(cards)
    return {"status": "automatic" if approved_profile and adequate else "pending",
            "grounded": len(grounded), "employers": employers}


def pending_review(stats):
    return stats.get("review", {}).get("status") == "pending"


def fresh_vacancy_samples(profile, trudvsem, current):
    if sample_is_fresh(profile.updated_at, current) and current_sample(profile.stats, profile) and not pending_review(profile.stats):
        yield profile.cards, profile.updated_at
    if (trudvsem and sample_is_fresh(trudvsem.collected_at, current)
            and current_sample(trudvsem.stats, profile) and not pending_review(trudvsem.stats)):
        yield trudvsem.cards or [], trudvsem.collected_at
