import hashlib
import json
import unicodedata
from functools import lru_cache
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select

from backend.db import LlmResponse, database, insert_for
from backend.settings import Settings


def canonical(value):
    """Requests that differ only in spacing or Unicode form share one cached answer."""
    if isinstance(value, str):
        return " ".join(unicodedata.normalize("NFC", value).split())
    if isinstance(value, dict):
        return {key: canonical(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [canonical(item) for item in value]
    return value


def cache_key(settings, options, name, schema, instructions, payload):
    request = {"model": settings.llm_model, "options": options, "name": name, "schema": schema,
               "instructions": instructions, "payload": payload}
    return hashlib.sha256(json.dumps(canonical(request), ensure_ascii=False, sort_keys=True).encode()).hexdigest()


@lru_cache
def cache_sessions(url):
    return database(url)[1]


def cached_response(settings, key):
    with cache_sessions(settings.database_url)() as db:
        return db.scalar(select(LlmResponse.response).where(LlmResponse.key == key))


def remember_response(settings, key, response):
    """Store the answer unless a concurrent request stored one first; both then use the stored answer."""
    with cache_sessions(settings.database_url).begin() as db:
        db.execute(insert_for(db, LlmResponse).values(key=key, model=settings.llm_model, response=response)
                   .on_conflict_do_nothing(index_elements=["key"]))
        return db.scalar(select(LlmResponse.response).where(LlmResponse.key == key))


RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def retryable(exc):
    """A dropped connection, a busy provider or a malformed answer is asked once more.

    A timeout is not: it has already used the time the interview step has.
    """
    if isinstance(exc, httpx.TimeoutException):
        return False
    if isinstance(exc, (httpx.TransportError, ValueError)):
        return True
    return str(exc) == "LLM_INVALID_RESPONSE" or str(exc) in {f"LLM_HTTP_ERROR: {code}" for code in RETRYABLE_STATUS}


async def completion_json(settings, headers, local, body):
    async with httpx.AsyncClient(timeout=settings.llm_timeout_seconds, trust_env=not local) as client:
        response = await client.post(settings.llm_base_url.rstrip("/") + "/chat/completions",
                                     headers=headers, json=body)
    if response.status_code != 200:
        raise RuntimeError(f"LLM_HTTP_ERROR: {response.status_code}")
    try:
        return json.loads(response.json()["choices"][0]["message"]["content"])
    except (ValueError, KeyError, IndexError, TypeError):
        raise RuntimeError("LLM_INVALID_RESPONSE") from None


async def structured_completion(settings: Settings, name: str, schema: dict,
                                instructions: str, payload: dict, *, parse=None, cache=True):
    """Return the model's JSON, or parse(JSON) when given.

    Only answers that parse are cached, so a rejected answer is asked again on retry.
    A malformed answer, a dropped connection or a busy provider is asked once more right away.
    """
    if not settings.llm_base_url or not settings.llm_model:
        raise RuntimeError("LLM_NOT_CONFIGURED: set LLM_BASE_URL and LLM_MODEL in .env")
    if not settings.llm_base_url.startswith(("http://", "https://")):
        raise RuntimeError("LLM_BASE_URL must be an HTTP API root")
    headers = {"Authorization": f"Bearer {settings.llm_api_key}"} if settings.llm_api_key else {}
    endpoint = urlsplit(settings.llm_base_url)
    local = endpoint.hostname in {"localhost", "127.0.0.1", "::1"}
    options = {"reasoning_effort": settings.llm_reasoning_effort} if settings.llm_reasoning_effort else {}
    if settings.llm_temperature is not None:
        options["temperature"] = settings.llm_temperature
    if settings.llm_seed is not None:
        options["seed"] = settings.llm_seed
    parse = parse or (lambda raw: raw)
    cache = cache and settings.llm_cache
    if cache:
        key = cache_key(settings, options, name, schema, instructions, payload)
        stored = cached_response(settings, key)
        if stored is not None:
            return parse(stored)
    body = {"model": settings.llm_model, **options,
            "messages": [
                {"role": "system", "content": instructions},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            "response_format": {"type": "json_schema", "json_schema": {
                "name": name, "strict": True, "schema": schema}}}
    for attempt in range(2):
        try:
            raw = await completion_json(settings, headers, local, body)
            result = parse(raw)
            break
        except (RuntimeError, ValueError, httpx.TransportError) as exc:
            if attempt or not retryable(exc):
                raise
    return parse(remember_response(settings, key, raw)) if cache else result


async def check_llm(settings: Settings):
    """Check the configured provider with a real structured request."""
    schema = {"type": "object", "properties": {"ok": {"type": "boolean", "enum": [True]}},
              "required": ["ok"], "additionalProperties": False}
    result = await structured_completion(
        settings, "connection_check", schema, "Return the user's JSON object exactly as given.", {"ok": True},
        cache=False,
    )
    if not isinstance(result, dict) or set(result) != {"ok"} or result["ok"] is not True:
        raise RuntimeError("LLM_SCHEMA_CHECK_FAILED")
    return {"status": "ok", "model": settings.llm_model, "structured_output": True}
