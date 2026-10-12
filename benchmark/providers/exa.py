"""Exa's public people search (POST /search, category "people"), isolated like Wrangle's.

Exa returns structured person entities with location, work history, and
education history. Its spec has no skills field and no role descriptions, so
those profile surfaces stay empty; report per-field coverage alongside scores.
"""
from __future__ import annotations

import os
import time

import certifi
import httpx

from ..profiles import prepare_candidates, strip_private
from .openai import ProviderError
from .wrangle import origin, scrub_surrogates

NAME = "exa"
API_KEY_ENV = "EXA_API_KEY"
DEFAULT_BASE_URL = "https://api.exa.ai"
ENDPOINT = "/search"
MODES = ("instant", "fast", "auto", "deep-lite", "deep", "deep-reasoning")
# Exa's documented public maximum for numResults; /search has no pagination.
# Short lists are zero-padded to 200 by the metrics, like any other provider.
MAX_RESULTS = 100


def search_body(query: str, mode: str) -> dict:
    if not isinstance(query, str) or not query.strip() or len(query) > 20000 or mode not in MODES:
        raise ValueError("Invalid Exa query or mode")
    return {"query": query.strip(), "type": mode, "category": "people", "numResults": MAX_RESULTS}


def dates(value) -> dict:
    value = value or {}
    return {"start_date": value.get("from"), "end_date": value.get("to")}


def candidate(result: dict) -> dict:
    """Map one Exa result to the fields profiles.normalize_profile reads."""
    person = next((entity for entity in result.get("entities") or [] if entity.get("type") == "person"), {})
    properties = person.get("properties") or {}
    return {"id": person.get("id"), "linkedinUrl": result.get("url"),
            "location": properties.get("location"),
            "experiences": [{"title": role.get("title"), "company": (role.get("company") or {}).get("name"),
                             "location": role.get("location"), **dates(role.get("dates"))}
                            for role in properties.get("workHistory") or []],
            "educations": [{"degree": school.get("degree"), "school": (school.get("institution") or {}).get("name"),
                            **dates(school.get("dates"))}
                           for school in properties.get("educationHistory") or []]}


class ExaSearch:
    def __init__(self, base_url: str = DEFAULT_BASE_URL, client=None):
        self.base_url = origin(base_url)
        self.client = client or httpx.Client(verify=certifi.where(), timeout=300, follow_redirects=False)

    def search(self, body: dict, idempotency_key: str) -> dict:
        key = os.environ.get(API_KEY_ENV)
        if not key:
            raise ProviderError("EXA_API_KEY_MISSING")
        if body != search_body(body.get("query"), body.get("type")):
            raise ValueError("Search body differs from the frozen public benchmark contract")
        started = time.perf_counter()
        try:
            # Exa documents no idempotency header; local unit state prevents replays.
            response = self.client.post(self.base_url + ENDPOINT, json=body,
                                        headers={"x-api-key": key, "Content-Type": "application/json"})
        except (httpx.HTTPError, OSError):
            raise ProviderError("EXA_REQUEST_UNCERTAIN") from None
        elapsed_ms = (time.perf_counter() - started) * 1000
        if response.status_code != 200:
            error = ProviderError("EXA_HTTP_ERROR", response.status_code)
            error.elapsed_ms = elapsed_ms
            raise error
        try:
            response_body = scrub_surrogates(response.json())
            if not isinstance(response_body, dict) or not isinstance(response_body.get("results"), list):
                raise ValueError("Malformed search response")
            if len(response_body["results"]) > MAX_RESULTS:
                raise ValueError("Provider returned more results than requested")
            rows = prepare_candidates([candidate(result) for result in response_body["results"]])
        except (TypeError, ValueError, AttributeError):
            raise ProviderError("EXA_RESPONSE_CONTRACT_REJECTED") from None
        return {"search_id": response_body.get("requestId"), "elapsed_ms": elapsed_ms,
                # searchTime is gateway retrieval time and can be below end-to-end latency.
                "api_latency_ms": response_body.get("searchTime"), "backend_latency_ms": None,
                "cost_dollars": (response_body.get("costDollars") or {}).get("total"),
                "returned_count": len(rows), "candidates": rows,
                "response": strip_private(response_body)}


SearchClient = ExaSearch
