import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from benchmark.io import atomic_json, digest, read_json
from benchmark.providers.metaview import MCPClient, MetaviewSearch, candidate, search_body
from benchmark.providers.openai import ProviderError


def profile(i):
    return {"candidate_id": str(i), "linkedin_url": f"https://linkedin.com/in/person-{i}",
            "summary": [{"description": "Do not use provider assessments"}],
            "profile": {"name": "Not evidence", "headline": "Not evidence", "location": "London",
                        "experience": [{"job_title": "Engineer", "description": "Python systems"}],
                        "education": [{"degree": "BSc"}], "skills": ["Python"], "email": "private@example.test"}}


class Server:
    def __init__(self, *, total=53, phases=None, create_error=None, read_error=False):
        self.calls, self.creates, self.offsets = [], 0, []
        self.total, self.phases = total, list(phases or ["busy", "idle"])
        self.create_error, self.read_error = create_error, read_error

    def __call__(self, request):
        payload = json.loads(request.content)
        self.calls.append(payload)
        method = payload["method"]
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "initialize":
            result = {"protocolVersion": "2025-03-26"}
        else:
            name, args = payload["params"]["name"], payload["params"]["arguments"]
            if name == "get_user_context":
                value = {"workspace_name": "Fixture"}
            elif name == "send_sourcing_message":
                self.creates += 1
                if self.create_error:
                    return httpx.Response(self.create_error, text="secret upstream body")
                value = {"search_id": "search-1", "status": "message_sent"}
            elif name == "get_search_details":
                if self.read_error:
                    return httpx.Response(503)
                phase = self.phases.pop(0) if len(self.phases) > 1 else self.phases[0]
                value = {"search_id": "search-1", "mode": "source", "phase": phase}
            elif name == "list_sourcing_candidates":
                offset = args["offset"]
                self.offsets.append(offset)
                end = min(offset + args["limit"], self.total)
                value = {"search_id": "search-1", "total_count": self.total, "remaining_credits": "unlimited",
                         "candidates": [profile(i) for i in range(offset, end)], "has_more": end < self.total}
            else:
                raise AssertionError(name)
            result = {"content": [{"type": "text", "text": json.dumps(value)}], "isError": False}
        data = {"jsonrpc": "2.0", "id": payload["id"], "result": result}
        return httpx.Response(200, text="event: message\ndata: " + json.dumps(data) + "\n\n",
                              headers={"Content-Type": "text/event-stream", "mcp-session-id": "session-secret"})


@patch.dict(os.environ, {"METAVIEW_API_KEY": "test-only-secret"})
class MetaviewTests(unittest.TestCase):
    def test_startup_idle_retry_only_reads_the_original_job(self):
        from tools.run_metaview_benchmark import StartupPollingSearch
        with tempfile.TemporaryDirectory() as temp:
            server, clock = Server(total=0, phases=["idle"]), [1000.0]
            def wait(seconds):
                clock[0] += seconds
                server.total = 2
            adapter = StartupPollingSearch(client=httpx.Client(transport=httpx.MockTransport(server)),
                state_root=temp, wall_clock=lambda: clock[0], sleep=wait)
            result = adapter.search(search_body("engineer", "source"), "unit")
            self.assertEqual(result["returned_count"], 2)
            self.assertEqual(server.creates, 1)
            self.assertEqual(server.offsets, [0, 0])
            self.assertEqual(clock[0], 1020)

    def test_startup_idle_retry_is_bounded(self):
        from tools.run_metaview_benchmark import StartupPollingSearch
        with tempfile.TemporaryDirectory() as temp:
            server, clock = Server(total=0, phases=["idle"]), [1000.0]
            adapter = StartupPollingSearch(client=httpx.Client(transport=httpx.MockTransport(server)),
                state_root=temp, wall_clock=lambda: clock[0], sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds))
            with self.assertRaisesRegex(ProviderError, "EMPTY_OR_AWAITING_INPUT"):
                adapter.search(search_body("engineer", "source"), "unit")
            self.assertEqual(server.creates, 1)
            self.assertEqual(clock[0], 1120)

    def test_startup_recovery_rejects_unknown_jobs_or_wrong_error(self):
        from tools.run_metaview_benchmark import validate_startup_recovery
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            body = search_body("engineer", "source")
            unit = {"status": "uncertain", "key": "role__1", "inputs": {"plan_sha256": "frozen", "body": body},
                    "error": {"code": "METAVIEW_EMPTY_OR_AWAITING_INPUT_INSPECT_SEARCH"}}
            atomic_json(root / "units/search/fixture.json", unit)
            with self.assertRaisesRegex(ValueError, "Unsafe startup"):
                validate_startup_recovery(root, "frozen")
            atomic_json(root / "provider-state" / f"{digest('role__1')}.json",
                        {"search_id": "known-job", "body_sha256": digest(body)})
            self.assertEqual(validate_startup_recovery(root, "frozen"), [{"unit_key": "role__1", "search_id": "known-job"}])
            unit["error"]["code"] = "METAVIEW_REQUEST_UNCERTAIN"
            atomic_json(root / "units/search/fixture.json", unit)
            with self.assertRaisesRegex(ValueError, "Unsafe startup"):
                validate_startup_recovery(root, "frozen")

    def test_runner_end_to_end_with_mocked_mcp_and_judge_resumes_without_new_calls(self):
        from benchmark.runner import make_plan, run
        from support import Judge, dataset
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data, _ = dataset(root / "dataset")
            envelope = make_plan(data, provider="metaview", mode="source", as_of="2026-09-30")
            server, judge = Server(total=2, phases=["idle"]), Judge()
            client = httpx.Client(transport=httpx.MockTransport(server))
            def factory(name, base_url, **options):
                return MetaviewSearch(base_url, client, **options)
            with patch("benchmark.runner.provider_client", factory):
                first = run(envelope, data, root / "runs", "fixture", judge=judge, interval_seconds=0, progress=lambda _: None)
                calls = len(judge.calls)
                second = run(envelope, data, root / "runs", "fixture", judge=judge, interval_seconds=0, progress=lambda _: None)
            self.assertEqual(first["status"], "complete")
            self.assertEqual(second["overall"], first["overall"])
            self.assertEqual(server.creates, 1)
            self.assertEqual(len(judge.calls), calls)
            self.assertEqual(first["overall"]["metrics"]["p@200"], .01)

    def test_distinct_repetitions_do_not_reuse_the_same_search(self):
        with tempfile.TemporaryDirectory() as temp:
            server = Server(total=1, phases=["idle"])
            adapter = MetaviewSearch(client=httpx.Client(transport=httpx.MockTransport(server)), state_root=temp,
                                    start_interval_seconds=0)
            for key in ("role__1", "role__2"):
                adapter.search(search_body("engineer", "source"), key)
            self.assertEqual(server.creates, 2)

    def test_lite_uses_exact_frozen_subset_without_new_searches_or_judgments(self):
        from benchmark.suites import DATASET, load_suite
        from tools.run_metaview_benchmark import derive_lite
        suite = load_suite("recruiterbench-lite-v1", DATASET)
        rows = [{"role_id": role_id, "category": str(i), "search_status": "missing", "scoreable": False}
                for i, role_id in enumerate(suite["role_ids"])]
        hard = {"benchmark": {"name": "recruiterbench-hard-v1"}, "plan_sha256": "source", "searches": rows + [
            {"role_id": "outside-lite", "category": "other", "search_status": "missing", "scoreable": False}]}
        result = derive_lite(hard, DATASET)
        self.assertEqual(result["expected_searches"], 58)
        self.assertEqual(result["searches"], rows)
        self.assertEqual(result["status"], "incomplete")

    def test_async_pagination_and_four_field_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            server, waits = Server(), []
            client = httpx.Client(transport=httpx.MockTransport(server))
            adapter = MetaviewSearch(client=client, state_root=Path(temp), sleep=waits.append)
            result = adapter.search(search_body("Python engineer", "source"), "unit")
            self.assertEqual(server.creates, 1)
            self.assertEqual(server.offsets, [0, 50])
            self.assertEqual(waits, [20])
            self.assertEqual(result["returned_count"], 53)
            self.assertEqual(result["candidates"][0]["profile"], {
                "location": "London", "educations": [{"degree": "BSc"}],
                "experiences": [{"job_title": "Engineer", "description": "Python systems"}], "listed_skills": ["Python"]})
            self.assertNotIn("Not evidence", json.dumps(result))
            self.assertNotIn("assessments", json.dumps(result))
            self.assertNotIn("private@example", json.dumps(result))
            before = len(server.calls)
            self.assertEqual(adapter.search(search_body("Python engineer", "source"), "unit"), result)
            self.assertEqual(len(server.calls), before)
            artifacts = "".join(path.read_text() for path in Path(temp).glob("*.json"))
            self.assertNotIn("test-only-secret", artifacts)
            self.assertNotIn("session-secret", artifacts)

    def test_caps_at_200_and_preserves_provider_order(self):
        with tempfile.TemporaryDirectory() as temp:
            server = Server(total=230, phases=["idle"])
            adapter = MetaviewSearch(client=httpx.Client(transport=httpx.MockTransport(server)), state_root=temp)
            result = adapter.search(search_body("engineer", "source"), "unit")
            self.assertEqual(server.offsets, [0, 50, 100, 150])
            self.assertEqual(result["returned_count"], 200)
            self.assertEqual(result["meta"]["total_surfaced"], 230)
            self.assertIn("person:199", result["candidates"][-1]["identity_keys"])

    def test_read_failure_resumes_existing_search_without_resubmitting(self):
        with tempfile.TemporaryDirectory() as temp:
            server = Server(read_error=True)
            adapter = MetaviewSearch(client=httpx.Client(transport=httpx.MockTransport(server)), state_root=temp, sleep=lambda _: None)
            body = search_body("engineer", "source")
            with self.assertRaisesRegex(ProviderError, "HTTP_ERROR"):
                adapter.search(body, "first")
            server.read_error = False
            result = adapter.search(body, "first")
            self.assertEqual(result["returned_count"], 53)
            self.assertEqual(server.creates, 1)

    def test_uncertain_create_cannot_be_replayed(self):
        with tempfile.TemporaryDirectory() as temp:
            server = Server(create_error=503)
            adapter = MetaviewSearch(client=httpx.Client(transport=httpx.MockTransport(server)), state_root=temp)
            body = search_body("engineer", "source")
            with self.assertRaisesRegex(ProviderError, "HTTP_ERROR"):
                adapter.search(body, "first")
            with self.assertRaisesRegex(ProviderError, "RECONCILE"):
                adapter.search(body, "first")
            self.assertEqual(server.creates, 1)

    def test_actual_search_creations_are_spaced_twenty_seconds(self):
        with tempfile.TemporaryDirectory() as temp:
            server, clock = Server(total=1, phases=["idle"]), [0.0]
            adapter = MetaviewSearch(client=httpx.Client(transport=httpx.MockTransport(server)), state_root=temp,
                                    clock=lambda: clock[0], wall_clock=lambda: 1000 + clock[0],
                                    sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds))
            for role in ("one", "two", "three"):
                adapter.search(search_body(role, "source"), role)
            self.assertEqual(clock[0], 40)
            self.assertEqual(server.creates, 3)

    def test_empty_roster_is_not_silently_scored_as_success(self):
        with tempfile.TemporaryDirectory() as temp:
            server = Server(total=0, phases=["idle"])
            adapter = MetaviewSearch(client=httpx.Client(transport=httpx.MockTransport(server)), state_root=temp)
            with self.assertRaisesRegex(ProviderError, "EMPTY_OR_AWAITING_INPUT"):
                adapter.search(search_body("engineer", "source"), "unit")

    def test_missing_profile_and_modified_checkpoint_fail_closed(self):
        with self.assertRaisesRegex(ProviderError, "PROFILE_MISSING"):
            candidate({"candidate_id": "one"})
        with tempfile.TemporaryDirectory() as temp:
            body = search_body("engineer", "source")
            path = Path(temp) / f"{digest('unit')}.json"
            atomic_json(path, {"body_sha256": digest(body), "status": "complete", "result": {}, "result_sha256": "wrong"})
            with self.assertRaisesRegex(ValueError, "modified"):
                MetaviewSearch(state_root=temp).search(body, "unit")

    def test_tool_level_error_and_redirect_do_not_expose_upstream_content(self):
        for response in (httpx.Response(302, headers={"Location": "https://other.test"}),
                         httpx.Response(200, json={"jsonrpc": "2.0", "id": 1,
                             "result": {"isError": True, "content": [{"type": "text", "text": "secret"}]}})):
            mcp = MCPClient(client=httpx.Client(transport=httpx.MockTransport(lambda _: response)))
            with self.assertRaises(ProviderError) as caught:
                mcp.call("get_user_context", {})
            self.assertNotIn("secret", str(caught.exception))

    def test_query_and_mode_validation(self):
        for query, mode in (("", "source"), ("engineer", "research"), ("x" * 10000, "source")):
            with self.assertRaises(ValueError):
                search_body(query, mode)
