"""Compare embedding models for card search and similar cases against word overlap.

data/embeddings/catalog_search.json holds 30 cards and two tests:
- queries: each is worded differently from the one card it should find;
- twins: a second person's card about the same problem as one of the 30; its
  nearest card should be that original, among the 30 cards and the other twins.
data/embeddings/vacancy_tasks.json holds vacancy duties labelled with the cases they
fit; every case (card or twin) should get a fitting duty as its question hint, not a wrong one.
Themes group the 41 cards; a twin should share a theme with its original, other pairs ideally not.

For every model and card text variant the script reports how often the right card
is first (hit@1) or in the top three (hit@3), the mean reciprocal rank, the score
gap to the best wrong card, and what share of right and wrong cards each threshold keeps.

Usage: .venv/bin/python scripts/evaluate_embeddings.py [--models bge-m3 nomic-embed-text]
       [--base-url http://localhost:11434/v1]
"""

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.embeddings import (
    EMBEDDED_SECTIONS, WORD_WEIGHT, card_texts, embed, search_passes, similarity, terms, word_share,
)
from backend.knowledge import SECTIONS
from backend.settings import Settings
from backend.themes import star_themes
from backend import vacancy_context
from backend.vacancy_context import duty_fits, duty_score, keywords, task_signals

DATA = Path(__file__).resolve().parent.parent / "data" / "embeddings" / "catalog_search.json"
TASKS = DATA.with_name("vacancy_tasks.json")
# Models trained with task prefixes score worse without them.
PREFIXES = {"nomic-embed-text": ("search_query: ", "search_document: ")}
VARIANTS = {"problem": ("task",), "core": EMBEDDED_SECTIONS, "all": SECTIONS}
THRESHOLDS = (0.45, 0.5, 0.55, 0.6, 0.65, 0.7)


def card_text(content, sections):
    return "\n".join(text for _, text in card_texts(content, sections))


def rank(scores, right):
    """Rank of the right card; ties count against it, so a zero-overlap match ranks last."""
    return 1 + sum(score >= scores[right] for card, score in scores.items() if card != right)


def report(name, ranks, timing=""):
    print(f"  {name:<26} hit@1 {sum(r == 1 for r in ranks):>2}/{len(ranks)}  hit@3 {sum(r <= 3 for r in ranks):>2}/{len(ranks)}"
          f"  MRR {statistics.mean(1 / r for r in ranks):.2f}{timing}")


def thresholds(pairs):
    """pairs: (right card score, [wrong card scores]) per test item."""
    for threshold in THRESHOLDS:
        kept = sum(right >= threshold for right, _ in pairs)
        noise = statistics.mean(sum(score >= threshold for score in wrong) for _, wrong in pairs)
        print(f"    threshold {threshold:.2f}: right card kept {kept:>2}/{len(pairs)}, wrong cards above it {noise:4.1f} on average")


def duty_hint(case, duties, meaning=None):
    """The duty sources_for_question picks for the case, or None; meaning maps a duty index to its similarity."""
    task_text = "\n".join(fact["text"] for fact in case["task"])
    case_text = "\n".join(fact["text"] for section in EMBEDDED_SECTIONS for fact in case.get(section, ()))
    signals, task_words, case_words = task_signals(case_text), keywords(task_text), keywords(case_text)
    # Each duty stands for a one-duty vacancy: it must fit on its own, then the best one wins.
    scored = [(score, index) for index, duty in enumerate(duties)
              if duty_fits(score := duty_score(signals, task_words, case_words, duty, (meaning or {}).get(index, 0.0)))]
    return max(scored)[1] if scored else None


def hint_report(name, hints, fits):
    right = sum(hint is not None and hint in fit for hint, fit in zip(hints, fits))
    wrong = sum(hint is not None and hint not in fit for hint, fit in zip(hints, fits))
    print(f"  {name:<26} right hint {right:>2}/{len(hints)}  wrong hint {wrong:>2}  no hint {len(hints) - right - wrong:>2}")


def theme_report(vectors, threshold):
    ids = sorted(vectors)
    neighbours = {card: {} for card in ids}
    for position, card in enumerate(ids):
        for other in ids[position + 1:]:
            if (score := similarity(vectors[card], vectors[other])) >= threshold:
                neighbours[card][other] = neighbours[other][card] = score
    themes = star_themes(neighbours)
    theme_of = {member: leader for leader, members in themes for member in members}
    twins = [card for card in ids if card.startswith("twin:")]
    together = sum(theme_of.get(twin) is not None and theme_of.get(twin) == theme_of.get(twin[5:]) for twin in twins)
    pairs = {frozenset((a, b)) for _, members in themes for i, a in enumerate(members) for b in members[i + 1:]}
    # A pair is right only when it is a card and its own twin.
    wrong = sorted(" ~ ".join(sorted(pair)) for pair in pairs if len({card.removeprefix("twin:") for card in pair}) > 1)
    print(f"    themes >= {threshold:.2f}: {len(themes)} themes, twins with their original {together}/{len(twins)},"
          f" other pairs in one theme {len(wrong)}" + (f": {', '.join(wrong[:6])}" if wrong else ""))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", nargs="+", default=["bge-m3", "nomic-embed-text"])
    parser.add_argument("--base-url", default="http://localhost:11434/v1")
    args = parser.parse_args()
    data = json.loads(DATA.read_text(encoding="utf-8"))
    cards, queries, twins = data["cards"], data["queries"], data["twins"]
    print(f"{len(cards)} cards, {len(queries)} queries, {len(twins)} twins\n")
    pool = {card["id"]: card["content"] for card in cards}
    pool.update({f"twin:{twin['of']}": twin["content"] for twin in twins})

    duties = json.loads(TASKS.read_text(encoding="utf-8"))["tasks"]
    cases = [(card["id"], card["content"]) for card in cards] + [(twin["of"], twin["content"]) for twin in twins]
    fits = [{index for index, duty in enumerate(duties) if card in duty["fits"]} for card, _ in cases]

    print("word overlap (current)")
    texts = {card: card_text(content, EMBEDDED_SECTIONS) for card, content in pool.items()}
    report("search", [rank({card: len(terms(query["text"]) & terms(texts[card])) for card in pool if not card.startswith("twin:")},
                           query["relevant"][0]) for query in queries])
    report("similar", [rank({card: len(terms(texts[f"twin:{twin['of']}"]) & terms(texts[card]))
                             for card in pool if card != f"twin:{twin['of']}"}, twin["of"]) for twin in twins])
    hint_report("vacancy hint", [duty_hint(content, [duty["text"] for duty in duties]) for _, content in cases], fits)

    for model in args.models:
        settings = Settings(_env_file=None, embedding_base_url=args.base_url, embedding_model=model)
        query_prefix, document_prefix = PREFIXES.get(model, ("", ""))
        embed(settings, ["прогрев"])  # load the model so timing shows a warm call
        started = time.perf_counter()
        query_vectors = [embed(settings, [query_prefix + query["text"]])[0] for query in queries]
        per_query = (time.perf_counter() - started) / len(queries) * 1000
        print(f"\n{model} (query {per_query:.0f} ms)")
        for variant, sections in VARIANTS.items():
            ids = list(pool)
            vectors = dict(zip(ids, embed(settings, [document_prefix + card_text(pool[card], sections) for card in ids])))
            search, similar = [], []
            for query, vector in zip(queries, query_vectors):
                scores = {card: similarity(vector, vectors[card]) for card in ids if not card.startswith("twin:")}
                right = query["relevant"][0]
                search.append((rank(scores, right), scores[right], [s for card, s in scores.items() if card != right]))
            for twin in twins:
                own = f"twin:{twin['of']}"
                scores = {card: similarity(vectors[own], vectors[card]) for card in ids if card != own}
                similar.append((rank(scores, twin["of"]), scores[twin["of"]], [s for card, s in scores.items() if card != twin["of"]]))
            report(f"{variant} / search", [r for r, *_ in search])
            if variant == "core":
                # What the catalog ships: meaning plus WORD_WEIGHT per share of the query's words.
                report("core / search + words", [rank({
                    card: similarity(vector, vectors[card]) + WORD_WEIGHT * word_share(terms(query["text"]), pool[card])
                    for card in ids if not card.startswith("twin:")}, query["relevant"][0])
                    for query, vector in zip(queries, query_vectors)])
                shown = [{card for card in ids if not card.startswith("twin:") and search_passes(
                    similarity(vector, vectors[card]), word_share(terms(query["text"]), pool[card]))}
                    for query, vector in zip(queries, query_vectors)]
                kept = sum(query["relevant"][0] in found for query, found in zip(queries, shown))
                noise = statistics.mean(len(found - set(query["relevant"])) for query, found in zip(queries, shown))
                print(f"    shipped pass rule: right card kept {kept}/{len(queries)}, wrong cards shown {noise:.1f} on average")
            report(f"{variant} / similar", [r for r, *_ in similar])
            if variant == "core":
                print("    search:")
                thresholds([(right, wrong) for _, right, wrong in search])
                print("    similar:")
                thresholds([(right, wrong) for _, right, wrong in similar])
                duty_texts = [duty["text"] for duty in duties]
                duty_vectors = embed(settings, [document_prefix + duty for duty in duty_texts])
                case_vectors = embed(settings, [query_prefix + card_text(content, sections) for _, content in cases])
                for threshold in (0.6, 0.62, 0.63, 0.64, 0.65, 0.7):
                    theme_report(vectors, threshold)
                shipped = vacancy_context.DUTY_MIN_SCORE
                for threshold in THRESHOLDS[:4]:
                    vacancy_context.DUTY_MIN_SCORE = threshold
                    hints = [duty_hint(content, duty_texts, {index: similarity(vector, duty_vector)
                                                             for index, duty_vector in enumerate(duty_vectors)})
                             for (_, content), vector in zip(cases, case_vectors)]
                    mark = " (shipped)" if threshold == shipped else ""
                    hint_report(f"vacancy hint >= {threshold:.2f}{mark}", hints, fits)
                vacancy_context.DUTY_MIN_SCORE = shipped


if __name__ == "__main__":
    main()
