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


def fresh_vacancy_samples(profile, trudvsem, current):
    if sample_is_fresh(profile.updated_at, current) and current_sample(profile.stats, profile):
        yield profile.cards, profile.updated_at
    if trudvsem and sample_is_fresh(trudvsem.collected_at, current) and current_sample(trudvsem.stats, profile):
        yield trudvsem.cards or [], trudvsem.collected_at
