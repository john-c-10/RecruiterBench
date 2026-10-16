"""Treg's routed people search, using the frozen benchmark brief unchanged."""
import os
from pathlib import Path
import re
import time

import certifi
import httpx

from benchmark.io import atomic_json, digest, now, read_json
from benchmark.profiles import prepare_candidates, strip_private
from benchmark.providers.exa import candidate as exa_candidate
from benchmark.providers.openai import ProviderError
from benchmark.providers.wrangle import origin, scrub_surrogates

NAME = "treg"
API_KEY_ENV = "TREG_API_KEY"
DEFAULT_BASE_URL = "https://treg.to"
MODES = ("routed",)
MAX_RESULTS = 200
ENDPOINT = "/call/treg.people.search"
RECEIPT_HEADERS = ("x-treg-call-id", "x-treg-cost-micro", "x-treg-served-by",
                   "x-treg-providers-tried", "x-treg-route-outcome", "x-treg-ignored-filters",
                   "x-treg-idempotent-replay", "x-treg-original-cost-micro")


def parallel_candidate(row):
    """Keep explicitly labeled evidence from Parallel's flattened profiles.

    Free-form snippets, headlines, inferred attributes and popularity are not
    substitutes for the benchmark's four professional evidence surfaces.
    """
    description = row.get("description") or ""
    if not isinstance(description, str):
        raise ValueError("Malformed Parallel description")
    headings = list(re.finditer(r"(?:^|\|)\s*([A-Za-z][A-Za-z &_-]{0,79}):\s*", description))
    fields = {}
    for index, match in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(description)
        value = description[match.end():end].strip()
        if value:
            fields.setdefault(match[1].lower().strip(), []).append(value)
    country = fields.get("country name", []) or fields.get("country iso", [])
    location = ", ".join(dict.fromkeys(fields.get("locality", []) + country))
    # Parallel sometimes supplies the current employer, role, and duties under
    # Position instead of Experience. It is explicit work evidence, unlike a
    # headline. Prefer the full history when both are present to avoid counting
    # the same current job twice.
    experiences = fields.get("experience", []) or fields.get("position", [])
    return {"linkedinUrl": row.get("url"), "location": location or None,
            "experiences": experiences, "educations": fields.get("education", []),
            "listed_skills": fields.get("skills", []) + fields.get("listed skills", [])}


def search_body(query, mode):
    if not isinstance(query, str) or not query.strip() or len(query) > 20000 or mode not in MODES:
        raise ValueError("Invalid Treg query or mode")
    return {"q": query.strip(), "limit": MAX_RESULTS}


def candidate(row, served_by):
    if not isinstance(row, dict):
        raise ValueError("Malformed Treg candidate")
    if served_by == "exa.people.search":
        return exa_candidate(row)
    if served_by == "parallel.people.search":
        return parallel_candidate(row)
    if served_by == "aviato.people.search":
        # Search returns currentExperienceSummary, not a full career history.
        # Explicitly select professional evidence; no names, headlines, scores,
        # company metadata, contact details, or provider-generated assessments.
        job = row.get("currentExperienceSummary")
        if job is not None and not isinstance(job, dict):
            raise ValueError("Malformed current experience")
        company = (job or {}).get("company") or {}
        return {"id": row.get("id"), "linkedinUrl": (row.get("URLs") or {}).get("linkedin"),
                "location": row.get("location"),
                "experiences": [{"title": job.get("title"), "company": company.get("name"),
                                 "start_date": job.get("startDate"), "end_date": job.get("endDate")}] if job else []}
    # Do not invent structured experience from search snippets or silently
    # discard evidence when Treg changes the upstream response shape.
    raise ProviderError("TREG_UNSUPPORTED_PROFILE_PROVIDER")


def normalize_receipt(receipt):
    if receipt["status"] == 202:
        raise ProviderError("TREG_PENDING_INSPECT_SAVED_RECEIPT")
    if receipt["status"] != 200:
        error = ProviderError("TREG_HTTP_ERROR", receipt["status"])
        error.elapsed_ms = receipt["elapsed_ms"]
        raise error
    try:
        response = receipt["response"]
        routing = response["_treg"]
        people = response["output"]["people"]
        if not isinstance(people, list) or len(people) > MAX_RESULTS:
            raise ValueError("Malformed result list")
        if routing.get("outcome") not in ("hit", "miss"):
            raise ValueError("Search did not reach a final outcome")
        served_by = routing.get("served_by")
        rows = prepare_candidates([candidate(row, served_by) for row in people])
        headers = receipt["headers"]
        cost_micro = int(headers["x-treg-cost-micro"]) if "x-treg-cost-micro" in headers else None
        if cost_micro is not None and cost_micro < 0:
            raise ValueError("Invalid charge")
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ProviderError("TREG_RESPONSE_CONTRACT_REJECTED") from None
    return {"search_id": headers.get("x-treg-call-id"), "elapsed_ms": receipt["elapsed_ms"],
            "api_latency_ms": None, "backend_latency_ms": None,
            "cost_dollars": cost_micro / 1_000_000 if cost_micro is not None else None,
            "meta": {"served_by": served_by, "outcome": routing["outcome"],
                     "tried": routing.get("tried", []),
                     "ignored_filters": routing.get("ignored_filters", []),
                     "headers": headers},
            "returned_count": len(rows), "candidates": rows}


class TregSearch:
    endpoint = ENDPOINT

    def __init__(self, base_url=DEFAULT_BASE_URL, client=None, *, state_root):
        self.base_url = origin(base_url)
        self.state_root = Path(state_root)
        self.client = client or httpx.Client(verify=certifi.where(), timeout=300, follow_redirects=False)

    @staticmethod
    def validate_body(body):
        if body != search_body(body.get("q"), "routed"):
            raise ValueError("Search body differs from the frozen benchmark contract")

    normalize = staticmethod(normalize_receipt)

    def search(self, body, idempotency_key):
        self.validate_body(body)
        path = self.state_root / f"{digest(idempotency_key)}.json"
        if path.exists():
            saved = read_json(path)
            if saved["body_sha256"] != digest(body):
                raise ValueError("Saved search request changed")
            return self.normalize(saved)
        key = os.environ.get(API_KEY_ENV)
        if not key:
            raise ProviderError("TREG_API_KEY_MISSING")
        started = time.perf_counter()
        try:
            response = self.client.post(self.base_url + self.endpoint, json=body,
                headers={"X-Treg-Token": key, "Idempotency-Key": idempotency_key})
        except (httpx.HTTPError, OSError):
            raise ProviderError("TREG_REQUEST_UNCERTAIN") from None
        receipt = {"time": now(), "status": response.status_code, "body_sha256": digest(body),
                   "elapsed_ms": (time.perf_counter() - started) * 1000,
                   "headers": {name: response.headers[name] for name in RECEIPT_HEADERS if name in response.headers}}
        try:
            receipt["response"] = strip_private(scrub_surrogates(response.json()))
        except ValueError:
            receipt["response_type"] = response.headers.get("content-type")
        # Persist received responses before validation or judging; recovery
        # never requires silently sending another paid search.
        atomic_json(path, receipt)
        return self.normalize(receipt)


SearchClient = TregSearch
