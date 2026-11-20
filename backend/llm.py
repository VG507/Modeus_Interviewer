import json
from urllib.parse import urlsplit

import httpx

from backend.settings import Settings


async def structured_completion(settings: Settings, name: str, schema: dict,
                                instructions: str, payload: dict):
    if not settings.llm_base_url or not settings.llm_model:
        raise RuntimeError("LLM_NOT_CONFIGURED: set LLM_BASE_URL and LLM_MODEL in .env")
    if not settings.llm_base_url.startswith(("http://", "https://")):
        raise RuntimeError("LLM_BASE_URL must be an HTTP API root")
    headers = {"Authorization": f"Bearer {settings.llm_api_key}"} if settings.llm_api_key else {}
    endpoint = urlsplit(settings.llm_base_url)
    local = endpoint.hostname in {"localhost", "127.0.0.1", "::1"}
    options = {"reasoning_effort": settings.llm_reasoning_effort} if settings.llm_reasoning_effort else {}
    async with httpx.AsyncClient(timeout=settings.llm_timeout_seconds, trust_env=not local) as client:
        response = await client.post(settings.llm_base_url.rstrip("/") + "/chat/completions",
            headers=headers, json={"model": settings.llm_model, **options,
                "messages": [
                    {"role": "system", "content": instructions},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                "response_format": {"type": "json_schema", "json_schema": {
                    "name": name, "strict": True, "schema": schema}}})
    if response.status_code != 200:
        raise RuntimeError(f"LLM_HTTP_ERROR: {response.status_code}")
    try:
        return json.loads(response.json()["choices"][0]["message"]["content"])
    except (ValueError, KeyError, IndexError, TypeError):
        raise RuntimeError("LLM_INVALID_RESPONSE") from None


async def check_llm(settings: Settings):
    """Check the configured provider with a real structured request."""
    schema = {"type": "object", "properties": {"ok": {"type": "boolean", "enum": [True]}},
              "required": ["ok"], "additionalProperties": False}
    result = await structured_completion(
        settings, "connection_check", schema, "Return the user's JSON object exactly as given.", {"ok": True},
    )
    if not isinstance(result, dict) or set(result) != {"ok"} or result["ok"] is not True:
        raise RuntimeError("LLM_SCHEMA_CHECK_FAILED")
    return {"status": "ok", "model": settings.llm_model, "structured_output": True}
