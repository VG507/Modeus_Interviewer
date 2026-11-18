"""Evaluate the six synthetic SQL scenarios on the configured model."""

import argparse
import asyncio
import json
from pathlib import Path
from time import monotonic

from backend.interview_engine import apply_extraction, choose_intent, extract, formulate, synthesize
from backend.interview_schemas import CaseState
from backend.settings import Settings

FIXTURES = Path(__file__).resolve().parents[1] / "data" / "interview"
PRIOR_CASES = {"index", "failed_attempt", "ambiguous", "pilot_reason", "team_action"}
BASELINE_CHECKS = {
    "generic": {"allowed_intents": ["get_case"], "no_facts": True},
    "rich": {"allowed_intents": ["decision"], "required_card_sections": ["actions", "verification"]},
    "index": {"allowed_intents": ["decision", "verification"], "required_card_sections": ["actions"]},
    "failed_attempt": {"allowed_intents": ["verification"], "required_card_sections": ["errors"]},
    "declined": {"allowed_intents": ["alternative", None], "no_facts": True},
    "ambiguous": {"allowed_intents": ["clarify_verification"]},
}


async def evaluate(settings, topic, scenario):
    scenario_id = scenario["id"]
    state = CaseState(asked_intents=["get_case"]).model_dump()
    opening = topic["opening_question"]
    transcript = [{"id": "q1", "kind": "question", "text": opening}]
    if scenario_id in PRIOR_CASES:
        # Seed evidence from a synthetic earlier answer, not from evaluator commentary.
        prior = {"id": "a0", "kind": "answer", "text": "В нашем сервисе медленно строился отчёт."}
        transcript.append(prior)
        state["facts"] = [{
            "id": "case-fact", "field": "task", "text": prior["text"],
            "supporting_message_ids": [prior["id"]],
        }]
        state["asked_intents"].append("action")
        opening = "Что в этом случае сделали вы?"
        if scenario_id == "pilot_reason":
            state["asked_intents"].append("decision")
            opening = "Почему вы выбрали именно составной индекс?"
        transcript.append({"id": "q2", "kind": "question", "text": opening})
    answer = {"id": "a1", "text": scenario["answer"]}
    transcript.append({**answer, "kind": "answer"})

    start = monotonic()
    evidence = await extract(settings, opening, answer, state)
    state, extraction = apply_extraction(state, evidence, answer["id"], state["asked_intents"][-1])
    intent = choose_intent(state, refused=evidence.refused, needs_rephrase=evidence.needs_rephrase)
    question = await formulate(settings, topic["topic"]["title"], intent, answer, state) if intent else None
    turn_seconds = round(monotonic() - start, 2)
    if question:
        transcript.append({"id": "next", "kind": "question", "text": question})
    content, _ = await synthesize(settings, topic["topic"]["title"], state, transcript)
    violations = []
    if "allowed_intents" in scenario and intent not in scenario["allowed_intents"]:
        violations.append(f"unexpected intent: {intent}")
    for field in scenario.get("required_card_sections", []):
        if not content[field]:
            violations.append(f"empty card section: {field}")
    if scenario.get("no_facts") and state["facts"]:
        violations.append("generic answer produced case facts")
    if scenario.get("responsibility") and not any(
        item.get("responsibility") == scenario["responsibility"] for item in content["actions"]
    ):
        violations.append("missing action attribution")
    return {
        "id": scenario_id, "expected": scenario["expected"], "forbidden": scenario["forbidden"],
        "intent": intent, "question": question, "refused": evidence.refused,
        "extraction": extraction, "card": content, "turn_seconds": turn_seconds, "violations": violations,
    }


async def run(args):
    settings = Settings()
    topic = json.loads((FIXTURES / "sql_topic.json").read_text(encoding="utf-8"))
    scenarios = [
        scenario for name in ("sql_scenarios.json", "pilot_regressions.json")
        for scenario in json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    ]
    results = []
    for scenario in scenarios:
        if args.ids and scenario["id"] not in args.ids:
            continue
        try:
            async with asyncio.timeout(3 * settings.llm_timeout_seconds):
                result = await evaluate(settings, topic, {**BASELINE_CHECKS.get(scenario["id"], {}), **scenario})
        except Exception as exc:
            result = {"id": scenario["id"], "error": type(exc).__name__}
        results.append(result)
        print(f'{result["id"]}: {result.get("error") or (result.get("question") or "завершение")}', flush=True)
        for violation in result.get("violations", []):
            print(f"  FAIL: {violation}", flush=True)
    if args.output:
        args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 1 if not results or any("error" in r or r.get("violations") for r in results) else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--ids", nargs="+")
    parser.add_argument("--output", type=Path)
    raise SystemExit(asyncio.run(run(parser.parse_args())))
