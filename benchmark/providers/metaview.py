"""Metaview's asynchronous MCP sourcing adapter with durable search checkpoints.

One frozen brief creates one search. Polling and pagination never send follow-up
messages, give feedback, reveal hidden candidates, or trigger outreach. Candidate
order is the MCP list order (not a documented relevance ranking).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from threading import Lock
import time

import certifi
import httpx

from ..io import atomic_json, digest, now, read_json
from ..profiles import normalize_profile, prepare_candidates
from .openai import ProviderError
from .wrangle import origin, scrub_surrogates

NAME = "metaview"
API_KEY_ENV = "METAVIEW_API_KEY"
DEFAULT_BASE_URL = "https://mcp.metaview.ai"
ENDPOINT = "/mcp"
MODES = ("source",)
MAX_RESULTS = 200
SEARCH_INSTRUCTIONS = (
    "\n\nReturn up to 200 matching candidates in this search. Proceed with the stated "
    "requirements without asking clarification questions; do not add requirements or "
    "relax mandatory qualifications. Do not send outreach, add candidates to a sequence, "
    "post to an ATS, or schedule recurring searches."
)


def search_body(query: str, mode: str) -> dict:
    if not isinstance(query, str) or not query.strip() or mode not in MODES:
        raise ValueError("Invalid Metaview query or mode")
    message = query.strip() + SEARCH_INSTRUCTIONS
    if len(message) > 10000:
        raise ValueError("Metaview brief exceeds the MCP message limit")
    return {"query": query.strip(), "mode": mode, "limit": MAX_RESULTS, "message": message}


class MCPClient:
    """A session per search; credentials and session IDs never enter artifacts."""
    def __init__(self, base_url=DEFAULT_BASE_URL, client=None, *, sleep=time.sleep):
        self.url = origin(base_url) + ENDPOINT
        key = os.environ.get(API_KEY_ENV)
        if not key:
            raise ProviderError("METAVIEW_API_KEY_MISSING")
        self._owned = client is None
        self.client = client or httpx.Client(verify=certifi.where(), timeout=60, follow_redirects=False)
        self.headers = {"Authorization": f"Bearer {key}", "Accept": "application/json, text/event-stream"}
        self.serial = 0
        self.sleep = sleep

    def close(self):
        if self._owned:
            self.client.close()

    def rpc(self, method, params=None, *, notification=False):
        self.serial += 1
        payload = {"jsonrpc": "2.0", "method": method}
        if not notification:
            payload["id"] = self.serial
        if params is not None:
            payload["params"] = params
        try:
            response = self.client.post(self.url, headers=self.headers, json=payload)
        except (httpx.HTTPError, OSError):
            raise ProviderError("METAVIEW_REQUEST_UNCERTAIN") from None
        if response.status_code not in (200, 202):
            raise ProviderError("METAVIEW_HTTP_ERROR", response.status_code)
        if response.headers.get("mcp-session-id"):
            self.headers["Mcp-Session-Id"] = response.headers["mcp-session-id"]
        if notification:
            return None
        try:
            if "text/event-stream" in response.headers.get("content-type", ""):
                messages = []
                for event in response.text.replace("\r\n", "\n").split("\n\n"):
                    data = "\n".join(line[5:].lstrip() for line in event.splitlines() if line.startswith("data:"))
                    if data:
                        messages.append(json.loads(data))
                body = next(item for item in messages if item.get("id") == payload["id"])
            else:
                body = response.json()
            if body.get("id") != payload["id"] or "error" in body:
                raise ValueError("MCP response mismatch or error")
            return scrub_surrogates(body["result"])
        except (ValueError, KeyError, TypeError, AttributeError, StopIteration):
            raise ProviderError("METAVIEW_MCP_RESPONSE_REJECTED") from None

    def initialize(self):
        value = self.rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                       "clientInfo": {"name": "RecruiterBench", "version": "1.0"}})
        if value.get("protocolVersion") != "2025-03-26":
            raise ProviderError("METAVIEW_PROTOCOL_REJECTED")
        self.headers["MCP-Protocol-Version"] = value["protocolVersion"]
        self.rpc("notifications/initialized", notification=True)
        return self.call("get_user_context", {})

    def call(self, name: str, arguments: dict, *, read_only=True) -> dict:
        arguments = {**arguments, "rationale": "Run the user-requested RecruiterBench evaluation using the frozen role brief."}
        # Reads can retry safely; a search-creation request is NEVER retried here.
        for attempt in range(4 if read_only else 1):
            try:
                result = self.rpc("tools/call", {"name": name, "arguments": arguments})
                break
            except ProviderError as error:
                retryable = error.code == "METAVIEW_REQUEST_UNCERTAIN" or error.status in (408, 429, 500, 502, 503, 504)
                if not read_only or not retryable or attempt == 3:
                    raise
                self.sleep(2 ** (attempt + 1))
        if result.get("isError"):
            raise ProviderError("METAVIEW_TOOL_ERROR")
        try:
            value = result.get("structuredContent")
            if value is None:
                text = "\n".join(item["text"] for item in result["content"] if item.get("type") == "text")
                value = json.loads(text)
            if not isinstance(value, dict):
                raise ValueError("Expected an object")
            return value
        except (ValueError, KeyError, TypeError):
            raise ProviderError("METAVIEW_TOOL_RESPONSE_REJECTED") from None


def candidate(row: dict) -> dict:
    profile = row.get("profile")
    if not isinstance(profile, dict):
        raise ProviderError("METAVIEW_PROFILE_MISSING")
    # Whitelist the same four fields as other providers. Never judge the agent's
    # match summary, headline, feedback, or other derived candidate assessments.
    return {"id": row.get("candidate_id"), "linkedin_url": row.get("linkedin_url"),
            **normalize_profile(profile)}


class MetaviewSearch:
    def __init__(self, base_url=DEFAULT_BASE_URL, client=None, *, state_root=None,
                 poll_seconds=20, timeout_seconds=1800, start_interval_seconds=20,
                 sleep=time.sleep, clock=time.monotonic, wall_clock=time.time):
        self.base_url, self.client = origin(base_url), client
        self.state_root = Path(state_root) if state_root is not None else None
        self.poll_seconds, self.timeout_seconds = poll_seconds, timeout_seconds
        self.start_interval_seconds = start_interval_seconds
        self.sleep, self.clock, self.wall_clock = sleep, clock, wall_clock
        self._start_guard, self._next_start = Lock(), 0.0

    def _start(self, mcp, body, path):
        # Pace the actual mutating request, after initialization. Holding the
        # lock through the POST prevents slow handshakes from bunching starts.
        with self._start_guard:
            rate_path = self.state_root / "_rate.json"
            previous_start = read_json(rate_path)["started_epoch"] if rate_path.exists() else 0
            wait = max(self._next_start - self.clock(), previous_start + self.start_interval_seconds - self.wall_clock())
            if wait > 0:
                self.sleep(wait)
            record = {"body_sha256": digest(body), "status": "creating", "started_at": now(),
                      "started_epoch": self.wall_clock()}
            atomic_json(path, record)
            atomic_json(rate_path, {"started_epoch": record["started_epoch"]})
            self._next_start = self.clock() + self.start_interval_seconds
            value = mcp.call("send_sourcing_message", {"message": body["message"], "mode": body["mode"]}, read_only=False)
            if not isinstance(value.get("search_id"), str) or value.get("status") != "message_sent":
                raise ProviderError("METAVIEW_SEARCH_CREATION_REJECTED")
            record.update(status="polling", search_id=value["search_id"])
            atomic_json(path, record)
            return record

    def search(self, body: dict, idempotency_key: str) -> dict:
        if body != search_body(body.get("query"), body.get("mode")):
            raise ValueError("Search body differs from the frozen public benchmark contract")
        if self.state_root is None:
            raise ValueError("Metaview requires a run-specific checkpoint directory")
        path = self.state_root / f"{digest(idempotency_key)}.json"
        record = read_json(path) if path.exists() else None
        if record and record.get("body_sha256") != digest(body):
            raise ValueError("Metaview checkpoint differs from the frozen query")
        if record and record["status"] == "complete":
            if digest(record["result"]) != record["result_sha256"]:
                raise ValueError("Metaview checkpoint was modified")
            return record["result"]
        if record and not record.get("search_id"):
            raise ProviderError("METAVIEW_CREATION_UNCERTAIN_RECONCILE_BEFORE_RESUME")
        mcp = MCPClient(self.base_url, self.client, sleep=self.sleep)
        try:
            mcp.initialize()
            if record is None:
                record = self._start(mcp, body, path)
            deadline = self.clock() + self.timeout_seconds
            while True:
                details = mcp.call("get_search_details", {"search_id": record["search_id"]})
                if details.get("search_id") != record["search_id"] or details.get("mode") != body["mode"]:
                    raise ProviderError("METAVIEW_SEARCH_ID_OR_MODE_MISMATCH")
                phase = details.get("phase")
                if phase == "idle":
                    break
                if phase != "busy":
                    raise ProviderError("METAVIEW_SEARCH_FAILED")
                if self.clock() >= deadline:
                    raise ProviderError("METAVIEW_SEARCH_TIMEOUT_RESUME_EXISTING")
                self.sleep(self.poll_seconds)
            rows, pages, total = [], [], None
            while len(rows) < MAX_RESULTS:
                page = mcp.call("list_sourcing_candidates", {"search_id": record["search_id"],
                    "limit": min(50, MAX_RESULTS - len(rows)), "offset": len(rows), "detail_level": "full"})
                if page.get("search_id") != record["search_id"] or not isinstance(page.get("candidates"), list):
                    raise ProviderError("METAVIEW_CANDIDATE_PAGE_REJECTED")
                count = page.get("total_count")
                if type(count) is not int or count < 0 or (total is not None and total != count):
                    raise ProviderError("METAVIEW_CANDIDATE_COUNT_CHANGED")
                total = count
                batch = page["candidates"]
                if len(batch) > min(50, MAX_RESULTS - len(rows)) or (not batch and page.get("has_more")):
                    raise ProviderError("METAVIEW_CANDIDATE_PAGE_REJECTED")
                rows.extend(candidate(row) for row in batch)
                pages.append({"offset": len(rows) - len(batch), "count": len(batch),
                              "has_more": page.get("has_more"), "remaining_credits": page.get("remaining_credits")})
                if not page.get("has_more"):
                    break
            # Empty/hidden rosters or an agent awaiting clarification must not
            # silently become successful zero-result searches.
            if not rows:
                raise ProviderError("METAVIEW_EMPTY_OR_AWAITING_INPUT_INSPECT_SEARCH")
            if len(rows) != min(total, MAX_RESULTS):
                raise ProviderError("METAVIEW_INCOMPLETE_CANDIDATE_PAGINATION")
            prepared = prepare_candidates(rows)
            if not any(row["field_coverage"]["experiences"] for row in prepared):
                raise ProviderError("METAVIEW_PROFILE_CONTRACT_REJECTED")
            result = {"search_id": record["search_id"], "elapsed_ms": (self.wall_clock() - record["started_epoch"]) * 1000,
                      "api_latency_ms": None, "backend_latency_ms": None, "returned_count": len(prepared),
                      "candidates": prepared, "meta": {"mode": body["mode"], "limit": MAX_RESULTS,
                      "total_surfaced": total, "candidate_order": "mcp_list_order", "pages": pages},
                      "response": {"search_id": record["search_id"], "phase": "idle", "total_count": total}}
            atomic_json(path, {**record, "status": "complete", "completed_at": now(),
                               "result": result, "result_sha256": digest(result)})
            return result
        finally:
            mcp.close()


SearchClient = MetaviewSearch
