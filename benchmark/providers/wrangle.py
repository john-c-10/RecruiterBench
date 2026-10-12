"""Wrangle's public REST contract, isolated from application internals."""
from __future__ import annotations

import os
import re
from urllib.parse import urlsplit
import time

import certifi
import httpx

from ..profiles import prepare_candidates, strip_private
from .openai import ProviderError

NAME = "wrangle"
API_KEY_ENV = "WRANGLE_API_KEY"
# No default: plans name the Wrangle origin explicitly.
DEFAULT_BASE_URL = None
MODES = ("turbo", "fast", "normal", "deep")
MAX_RESULTS = 200
ENDPOINT = "/api/developers/search"
LONE_SURROGATE = re.compile("[\ud800-\udfff]")


def scrub_surrogates(value):
    # Profiles can carry half of a split UTF-16 pair (e.g. styled LinkedIn text);
    # it parses as JSON but cannot be written as UTF-8 artifacts.
    if isinstance(value, str):
        return LONE_SURROGATE.sub("\ufffd", value)
    if isinstance(value, dict):
        return {scrub_surrogates(key): scrub_surrogates(child) for key, child in value.items()}
    if isinstance(value, list):
        return [scrub_surrogates(child) for child in value]
    return value


def origin(value: str) -> str:
    url = urlsplit(value)
    if url.scheme != "https" or not url.hostname or url.username or url.password or url.path not in ("", "/") or url.query or url.fragment:
        raise ValueError("API base must be an explicit HTTPS origin without credentials, path, or query")
    return value.rstrip("/")


def search_body(query: str, mode: str) -> dict:
    if not isinstance(query, str) or not query.strip() or len(query) > 20000 or mode not in MODES:
        raise ValueError("Invalid Wrangle query or mode")
    return {"query": query.strip(), "mode": mode, "limit": MAX_RESULTS, "connectorIds": [],
            "dedupe": {"excludeIds": [], "excludeSavedOrHidden": False, "excludeAccountSavedOrHidden": False,
                       "excludeHidden": False, "excludeSaved": False, "excludeShortlisted": False},
            "minimumMatchThreshold": 0}


class WrangleSearch:
    def __init__(self, base_url: str, client=None):
        self.base_url = origin(base_url)
        self.client = client or httpx.Client(verify=certifi.where(), timeout=300, follow_redirects=False)

    def search(self, body: dict, idempotency_key: str) -> dict:
        key = os.environ.get("WRANGLE_API_KEY")
        if not key:
            raise ProviderError("WRANGLE_API_KEY_MISSING")
        if body != search_body(body.get("query"), body.get("mode")):
            raise ValueError("Search body differs from the frozen public benchmark contract")
        started = time.perf_counter()
        try:
            response = self.client.post(self.base_url + ENDPOINT, json=body,
                                        headers={"Authorization": f"Bearer {key}", "Idempotency-Key": idempotency_key,
                                                 "Content-Type": "application/json"})
        except (httpx.HTTPError, OSError):
            raise ProviderError("WRANGLE_REQUEST_UNCERTAIN") from None
        elapsed_ms = (time.perf_counter() - started) * 1000
        if response.status_code != 200:
            error = ProviderError("WRANGLE_HTTP_ERROR", response.status_code)
            error.elapsed_ms = elapsed_ms
            # Never persist upstream free-text errors, headers, or credentials.
            raise error
        try:
            response_body = scrub_surrogates(response.json())
            if not isinstance(response_body, dict) or not isinstance(response_body.get("candidates"), list):
                raise ValueError("Malformed search response")
            meta = response_body.get("meta", {})
            if meta.get("mode") != body["mode"] or meta.get("limit") != 200:
                raise ValueError("Provider silently changed requested mode or result limit")
            if (meta.get("connectors") or {}).get("fallback"):
                raise ValueError("Provider silently changed candidate sources")
            rows = prepare_candidates(response_body["candidates"])
        except (TypeError, ValueError, AttributeError):
            raise ProviderError("WRANGLE_RESPONSE_CONTRACT_REJECTED") from None
        return {"search_id": response_body.get("searchId"), "elapsed_ms": elapsed_ms,
                "api_latency_ms": meta.get("latencyMs"), "backend_latency_ms": meta.get("backendLatencyMs"),
                "credits_used": response_body.get("creditsUsed"), "meta": meta,
                "returned_count": len(rows), "candidates": rows,
                "response": strip_private(response_body)}


SearchClient = WrangleSearch
