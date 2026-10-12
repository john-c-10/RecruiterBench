"""Pinned-model structured completions; no model fallback. The caller pins the
model: rubric extraction uses gpt-6.1-sol at low effort (the default here) and
both judge stages use gpt-6-luna at medium (models.JUDGE_*). Transient transport
failures (timeouts, 408/409/429, 5xx) are retried with backoff; the attempt count
is recorded with every judgment."""
from __future__ import annotations

import json
import os
import random
import time

import certifi
import httpx
import jsonschema

from ..models import MODEL, REASONING_EFFORT


class ProviderError(RuntimeError):
    def __init__(self, code: str, status: int | None = None):
        super().__init__(f"{code}" + (f" (HTTP {status})" if status else ""))
        self.code, self.status = code, status


RETRYABLE_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})


class OpenAIJudge:
    def __init__(self, client=None, *, model: str = MODEL, reasoning_effort: str = REASONING_EFFORT,
                 retries: int = 6, backoff_seconds: float = 2.0, sleep=time.sleep):
        self.client = client or httpx.Client(verify=certifi.where(), timeout=180, follow_redirects=False)
        self.model, self.reasoning_effort = model, reasoning_effort
        self.retries, self.backoff_seconds, self.sleep = retries, backoff_seconds, sleep

    def _wait(self, attempt: int, response=None):
        retry_after = response.headers.get("retry-after") if response is not None else None
        try:
            delay = float(retry_after) if retry_after else None
        except ValueError:
            delay = None
        if delay is None:
            delay = self.backoff_seconds * 2 ** attempt * (0.5 + random.random())
        self.sleep(min(delay, 120))

    def complete(self, *, system: str, payload: dict, schema: dict, name: str, cache_key: str | None = None) -> dict:
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            raise ProviderError("OPENAI_API_KEY_MISSING")
        request = {"model": self.model, "reasoning_effort": self.reasoning_effort, "store": False,
                   "max_completion_tokens": 16000,
                   "messages": [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
                   "response_format": {"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": schema}},
                   **({"prompt_cache_key": cache_key} if cache_key else {})}
        for attempt in range(self.retries + 1):
            started = time.perf_counter()
            try:
                response = self.client.post("https://api.openai.com/v1/chat/completions", json=request,
                                            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
            except (httpx.HTTPError, OSError):
                if attempt == self.retries:
                    raise ProviderError("OPENAI_REQUEST_UNCERTAIN") from None
                self._wait(attempt)
                continue
            elapsed_ms = (time.perf_counter() - started) * 1000
            if response.status_code in RETRYABLE_STATUS and attempt < self.retries:
                self._wait(attempt, response)
                continue
            break
        if response.status_code != 200:
            raise ProviderError("OPENAI_HTTP_ERROR", response.status_code)
        try:
            raw = response.json()
            choice = raw["choices"][0]
            if choice.get("finish_reason") != "stop" or choice["message"].get("refusal"):
                raise ProviderError("OPENAI_INCOMPLETE_OR_REFUSAL")
            returned_model = raw.get("model", "")
            if returned_model != self.model and not returned_model.startswith(self.model + "-"):
                raise ProviderError("OPENAI_MODEL_MISMATCH")
            result = json.loads(choice["message"]["content"])
            jsonschema.validate(result, schema)
        except (KeyError, IndexError, TypeError, ValueError, jsonschema.ValidationError):
            raise ProviderError("OPENAI_SCHEMA_REJECTED") from None
        return {"value": result, "model": returned_model, "requested_model": self.model,
                "reasoning_effort": self.reasoning_effort, "response_id": raw.get("id"),
                "usage": raw.get("usage", {}), "elapsed_ms": elapsed_ms, "transport_attempts": attempt + 1}
