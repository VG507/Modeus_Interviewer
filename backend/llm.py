import json

import httpx

from backend.settings import Settings


async def check_llm(settings: Settings):
    """A real, tiny structured-output request. Never substitutes a mock."""
    if not settings.llm_base_url or not settings.llm_model:
        raise RuntimeError("LLM_NOT_CONFIGURED: set LLM_BASE_URL and LLM_MODEL in .env")
    if not settings.llm_base_url.startswith(("http://", "https://")):
        raise RuntimeError("LLM_BASE_URL must be an HTTP API root")
    headers = {"Authorization": f"Bearer {settings.llm_api_key}"} if settings.llm_api_key else {}
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}},
              "required": ["ok"], "additionalProperties": False}
    async with httpx.AsyncClient(timeout=settings.llm_timeout_seconds) as client:
        response = await client.post(settings.llm_base_url.rstrip("/") + "/chat/completions",
            headers=headers, json={"model": settings.llm_model,
                "messages": [{"role": "user", "content": "Return the JSON object with ok set to true."}],
                "response_format": {"type": "json_schema", "json_schema": {
                    "name": "connection_check", "strict": True, "schema": schema}}})
    if response.status_code != 200:
        raise RuntimeError(f"LLM_HTTP_ERROR: {response.status_code}")
    try:
        result = json.loads(response.json()["choices"][0]["message"]["content"])
    except (ValueError, KeyError, IndexError, TypeError):
        raise RuntimeError("LLM_INVALID_RESPONSE") from None
    if result != {"ok": True}:
        raise RuntimeError("LLM_SCHEMA_CHECK_FAILED")
    return {"status": "ok", "model": settings.llm_model, "structured_output": True}
