"""Themes of published cases by meaning, and how taxonomy categories correlate with them.

A theme is a star: the case with the most close neighbours leads it and takes in its
unassigned neighbours, so every case of a theme is at least THEME_MIN_SCORE close to the
leading one. The matrix gives the phi coefficient between "the case is in this theme"
and "the case names a skill of this category", over all embedded published cases.
"""

import json
import math
from collections import Counter

from sqlalchemy import Float, and_, case, func, select, tuple_
from sqlalchemy.orm import aliased

from backend.db import CardEmbedding, KnowledgeCard
from backend.embeddings import case_text
from backend.knowledge import SECTIONS, cases_of, published_cards
from backend.taxonomy import SKILL_TAXONOMY, TAXONOMY
from backend.vacancy_parser import skill_hits
from backend.vectors import pgvector_column, similarity

# Star themes on the 41 evaluation cards (scripts/evaluate_embeddings.py): at 0.64, 9 of 11 twins share
# a theme with their original and no other pair does; 0.63 already joins 2 wrong pairs, 0.65 splits 2 twins.
# The optimum is narrow, so the API lets an editor pass another threshold.
THEME_MIN_SCORE = 0.64


def close_pairs(db, model, candidates, threshold):
    """{case: {other case: score}} for embedded cases of candidate cards at least threshold apart."""
    keys = [tuple(row) for row in db.execute(select(CardEmbedding.card_id, CardEmbedding.case_index).where(
        CardEmbedding.model == model, CardEmbedding.card_id.in_(candidates)))]
    neighbours = {key: {} for key in keys}
    if pgvector_column(db):
        left, right = aliased(CardEmbedding), aliased(CardEmbedding)
        score = case((func.vector_dims(left.embedding) == func.vector_dims(right.embedding),
                      1 - left.embedding.op("<=>", return_type=Float)(right.embedding))).label("score")
        pairs = db.execute(select(left.card_id, left.case_index, right.card_id, right.case_index, score).join(
            right, and_(right.model == model, tuple_(left.card_id, left.case_index) < tuple_(right.card_id, right.case_index)),
        ).where(left.model == model, left.card_id.in_(candidates), right.card_id.in_(candidates),
                score >= threshold))
        found = (((a, i), (b, j), value) for a, i, b, j, value in pairs)
    else:
        vectors = {(card_id, index): json.loads(value) for card_id, index, value in db.execute(
            select(CardEmbedding.card_id, CardEmbedding.case_index, CardEmbedding.embedding).where(
                CardEmbedding.model == model, CardEmbedding.card_id.in_(candidates)))}
        ordered = sorted(vectors)
        found = ((key, other, similarity(vectors[key], vectors[other]))
                 for position, key in enumerate(ordered) for other in ordered[position + 1:]
                 if len(vectors[key]) == len(vectors[other]))
    for key, other, value in found:
        if value >= threshold:
            neighbours[key][other] = neighbours[other][key] = value
    return neighbours


def star_themes(neighbours, case_order):
    """[(leader, members)], with ties broken by total similarity and case text."""
    unassigned = set(neighbours)
    themes = []
    for leader in sorted(neighbours, key=lambda key: (
        -len(neighbours[key]), -math.fsum(neighbours[key].values()), case_order[key], key,
    )):
        if leader not in unassigned:
            continue
        members = [leader, *sorted((other for other in neighbours[leader] if other in unassigned),
                                   key=lambda key: (case_order[key], key))]
        unassigned.difference_update(members)
        if len(members) > 1:
            themes.append((leader, members))
    return themes


def named_skills(case):
    """(canonical skills, taxonomy categories) named in a case's title or facts."""
    text = "\n".join([case.get("title", ""), *(fact["text"] for section in SECTIONS for fact in case.get(section) or ())])
    skills = {skill for skill, _ in skill_hits(text)}
    return skills, {SKILL_TAXONOMY[skill][0] for skill in skills if skill in SKILL_TAXONOMY}


def case_categories(content):
    """{case index: (canonical skills, taxonomy categories)} for each case of a card."""
    return {index: named_skills(case) for index, case in enumerate(cases_of(content))}


def phi(both, theme_only, category_only, neither):
    """Correlation of two yes/no features from their 2x2 counts; 0 when either never varies."""
    spread = (both + theme_only) * (category_only + neither) * (both + category_only) * (theme_only + neither)
    return (both * neither - theme_only * category_only) / math.sqrt(spread) if spread else 0.0


def catalog_themes(db, model, threshold=THEME_MIN_SCORE):
    """Themes with their cards and skills, and the theme x category correlation matrix."""
    candidates = published_cards().with_only_columns(KnowledgeCard.id)
    neighbours = close_pairs(db, model, candidates, threshold)
    contents = dict(db.execute(published_cards().with_only_columns(KnowledgeCard.id, KnowledgeCard.content)
                               .where(KnowledgeCard.id.in_({card_id for card_id, _ in neighbours}))).all())
    named = {(card_id, index): value for card_id, content in contents.items()
             for index, value in case_categories(content).items() if (card_id, index) in neighbours}
    titles = {(card_id, index): case.get("title") or contents[card_id]["title"]
              for card_id, content in contents.items() for index, case in enumerate(cases_of(content))}
    case_order = {(card_id, index): case_text(case, SECTIONS) for card_id, content in contents.items()
                  for index, case in enumerate(cases_of(content))}
    neighbours = {key: {other: score for other, score in edges.items() if other in named}
                  for key, edges in neighbours.items() if key in named}
    themes = star_themes(neighbours, case_order)
    categories = [name for name in TAXONOMY if any(name in found for _, found in named.values())]
    rows = []
    for leader, members in themes:
        inside = set(members)
        skills = Counter(skill for member in members for skill in named[member][0])
        cells = []
        for category in categories:
            has = {key for key, (_, found) in named.items() if category in found}
            both = len(inside & has)
            cells.append({"phi": round(phi(both, len(inside) - both, len(has) - both,
                                           len(named) - len(inside | has)), 2), "cases": both})
        rows.append({"title": titles[leader], "card_id": leader[0], "cases": len(members),
                     "card_ids": list(dict.fromkeys(card_id for card_id, _ in members)),
                     "skills": [{"skill": skill, "cases": count}
                                for skill, count in sorted(skills.items(), key=lambda item: (-item[1], item[0]))[:5]],
                     "matrix": cells})
    return {"threshold": threshold, "cases": len(named), "unthemed": len(named) - sum(len(m) for _, m in themes),
            "categories": categories, "themes": rows}
