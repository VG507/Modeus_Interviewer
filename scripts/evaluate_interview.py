"""Evaluate synthetic SQL scenarios and pilot regressions on the configured model."""

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


def check_facts(content, scenario):
    violations = []
    for field, fragments in scenario.get("expected_facts", {}).items():
        for fragment in fragments:
            if not any(fragment in fact["text"] for fact in content[field]):
                violations.append(f"missing evidence in {field}: {fragment}")
    for field, fragments in scenario.get("forbidden_facts", {}).items():
        for fragment in fragments:
            if any(fragment in fact["text"] for fact in content[field]):
                violations.append(f"misclassified evidence in {field}: {fragment}")
    return violations


async def evaluate(settings, topic, scenario):
    scenario_id = scenario["id"]
    state = CaseState(asked_intents=["get_case"]).model_dump()
    opening = topic["opening_question"]
    if scenario_id in PRIOR_CASES:
        # Seed evidence from a synthetic earlier answer, not from evaluator commentary.
        prior = {"id": "a0", "kind": "answer", "text": "В нашем сервисе медленно строился отчёт."}
        state["facts"] = [{
            "id": "case-fact", "field": "task", "text": prior["text"],
            "supporting_message_ids": [prior["id"]],
        }]
        state["asked_intents"].append("action")
        opening = "Что в этом случае сделали вы?"
        if scenario_id == "pilot_reason":
            state["asked_intents"].append("decision")
            opening = "Почему вы выбрали именно составной индекс?"
    answer = {"id": "a1", "text": scenario["answer"]}

    start = monotonic()
    evidence = await extract(settings, opening, answer, state)
    state, extraction = apply_extraction(state, evidence, answer["id"], state["asked_intents"][-1])
    intent = choose_intent(state, refused=evidence.refused, needs_rephrase=evidence.needs_rephrase)
    question = await formulate(settings, topic["topic"]["title"], intent, answer, state) if intent else None
    turn_seconds = round(monotonic() - start, 2)
    content, _ = await synthesize(settings, topic["topic"]["title"], state)
    violations = check_facts(content, scenario)
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
    result = {
        "id": scenario_id, "expected": scenario["expected"], "forbidden": scenario["forbidden"],
        "intent": intent, "question": question, "refused": evidence.refused,
        "extraction": extraction, "card": content, "turn_seconds": turn_seconds, "violations": violations,
    }
    followup = scenario.get("followup")
    if followup and question and intent == followup["intent"]:
        reply = {"id": "a2", "text": followup["answer"]}
        state["asked_intents"].append(intent)
        model_state = {**state, "recent_history": [{"question": opening, "answer": answer["text"]}]}
        evidence = await extract(settings, question, reply, model_state)
        state, extraction = apply_extraction(state, evidence, reply["id"], intent)
        content, _ = await synthesize(settings, topic["topic"]["title"], state)
        violations.extend(check_facts(content, followup))
        result["followup"] = {"answer": reply["text"], "extraction": extraction, "card": content}
    return result


def outcome(result):
    """What a repeated run must reproduce: the facts, the next step and its wording."""
    if "error" in result:
        return result["error"]
    turns = [result, *([result["followup"]] if "followup" in result else [])]
    return json.dumps([result["intent"], result["question"],
                       [[(fact["field"], fact["text"]) for fact in turn["extraction"]["facts"]] for turn in turns]],
                      ensure_ascii=False)


async def run(args):
    settings = Settings(**({"llm_cache": False} if args.no_cache else {}))
    topic = json.loads((FIXTURES / "sql_topic.json").read_text(encoding="utf-8"))
    scenarios = [
        scenario for name in ("sql_scenarios.json", "pilot_regressions.json")
        for scenario in json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    ]
    results = []
    for scenario in scenarios:
        if args.ids and scenario["id"] not in args.ids:
            continue
        runs = []
        for _ in range(args.repeat):
            try:
                async with asyncio.timeout(3 * settings.llm_timeout_seconds):
                    runs.append(await evaluate(settings, topic, {**BASELINE_CHECKS.get(scenario["id"], {}), **scenario}))
            except Exception as exc:
                runs.append({"id": scenario["id"], "error": type(exc).__name__})
        result = runs[0]
        if args.repeat > 1:
            result["stable"] = len({outcome(item) for item in runs}) == 1
        results.append(result)
        stability = "" if args.repeat == 1 else (" [стабильно]" if result["stable"] else " [РАЗНЫЕ ОТВЕТЫ]")
        print(f'{result["id"]}: {result.get("error") or (result.get("question") or "завершение")}{stability}', flush=True)
        for violation in result.get("violations", []):
            print(f"  FAIL: {violation}", flush=True)
    if args.repeat > 1:
        print(f'Стабильно при {args.repeat} прогонах: {sum(r["stable"] for r in results)} из {len(results)}', flush=True)
    if args.output:
        args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 1 if not results or any("error" in r or r.get("violations") for r in results) else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--ids", nargs="+")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--repeat", type=int, default=1, help="Run each scenario several times and compare the results")
    parser.add_argument("--no-cache", action="store_true", help="Ask the model every time to measure its own stability")
    raise SystemExit(asyncio.run(run(parser.parse_args())))
