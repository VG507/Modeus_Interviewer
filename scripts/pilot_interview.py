"""Run a moderated text interview against the local Modeus backend."""

from getpass import getpass
from time import monotonic, sleep
from uuid import uuid4

import httpx

from backend.settings import Settings


def pilot(client, settings):
    def request(method, path, body=None):
        response = client.request(method, f"/api/v1{path}", json=body, headers={
            "Origin": settings.app_origin, "Idempotency-Key": str(uuid4()),
        })
        if response.is_error:
            error = response.json().get("error", {})
            raise RuntimeError(f'HTTP {response.status_code}: {error.get("code", "UNKNOWN")}')
        return response.json()

    def wait(path):
        deadline = monotonic() + 3 * settings.llm_timeout_seconds + 15
        while monotonic() < deadline:
            state = request("GET", path)
            if state["processing_status"] not in {"queued", "running"}:
                return state
            sleep(0.5)
        raise RuntimeError("Задание ещё обрабатывается. ID интервью указан выше.")

    request("POST", "/auth/login", {"email": input("Почта Modeus: "), "password": getpass("Пароль: ")})
    topics = request("GET", "/topics")["items"]
    if not any(topic["id"] == "sql-slow-query" for topic in topics):
        raise RuntimeError("Сначала загрузите тему: python -m scripts.seed_interview")
    print("Без имён, идентификаторов и данных работодателя. Команды /skip и /finish.")
    context = {
        "role": input("Роль: ").strip() or "Backend-разработчик",
        "experience": input("Опыт (можно пропустить): ").strip(),
        "responsibilities": input("Задачи (можно пропустить): ").strip(),
        "conditions": input("Условия работы (можно пропустить): ").strip(),
    }
    session = request("POST", "/interviews", {"topic_id": "sql-slow-query", "context": context})
    path = f'/interviews/{session["id"]}'
    print(f'ID интервью: {session["id"]}')
    while True:
        state = wait(path)
        version = {"expected_version": state["version"]}
        if state["status"] == "completed":
            print("\n" + request("GET", f'/cards/{state["card_id"]}')["text"])
            return
        if state["processing_status"] == "failed":
            print(f'Ошибка обработки: {state["error"]["code"]}.')
            if input("Введите /retry для повтора или Enter для выхода: ").strip() != "/retry":
                return
            request("POST", path + "/retry", version)
            continue
        question = next(message["text"] for message in state["messages"]
                        if message["id"] == state["current_question_id"])
        print("\n" + question)
        answer = input("> ").strip()
        if not answer:
            continue
        if answer in {"/finish", "/skip"}:
            request("POST", path + answer, version)
            continue
        draft = request("POST", path + "/answers", {
            **version, "question_id": state["current_question_id"], "text": answer,
        })
        request("POST", f'{path}/answers/{draft["answer"]["id"]}/submit', {
            "expected_version": draft["interview"]["version"],
        })


if __name__ == "__main__":
    try:
        with httpx.Client(base_url="http://127.0.0.1:8000", trust_env=False, timeout=5) as client:
            pilot(client, Settings())
    except (RuntimeError, httpx.RequestError) as exc:
        raise SystemExit(str(exc)) from None
    except (KeyboardInterrupt, EOFError):
        raise SystemExit("\nИнтервью остановлено.") from None
