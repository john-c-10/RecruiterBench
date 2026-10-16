import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from benchmark.io import atomic_json, digest, read_json
from benchmark.metrics import quality_metrics, summarize
from benchmark.models import JUDGE_MODEL, JUDGE_REASONING_EFFORT, schema
from benchmark.providers.metaview import search_body
from benchmark.providers.openai import ProviderError
from benchmark.runner import make_plan, run
from benchmark.state import execute_unit
from support import Judge, dataset, gate
from test_metaview import Server
from tools.resume_metaview_benchmark import (AuthorizedFailureSearch, GatewayRetryJudge,
                                            annotate_failures, validate_gateway_recovery)


@patch.dict(os.environ, {"METAVIEW_API_KEY": "test-secret"})
class RecoveryTests(unittest.TestCase):
    @patch.dict(os.environ, {"OPENAI_API_KEY": "test-only"})
    def test_gateway_retry_preserves_identical_request_and_is_bounded(self):
        requests, waits = [], []
        def handler(request):
            requests.append(json.loads(request.content))
            if len(requests) < 3:
                return httpx.Response(520, text="private upstream body")
            return httpx.Response(200, json={"model": JUDGE_MODEL, "choices": [
                {"finish_reason": "stop", "message": {"content": json.dumps(gate())}}]})
        judge = GatewayRetryJudge(httpx.Client(transport=httpx.MockTransport(handler)),
            model=JUDGE_MODEL, reasoning_effort=JUDGE_REASONING_EFFORT, sleep=waits.append)
        result = judge.complete(system="frozen", payload={"frozen": True}, schema=schema("gate"), name="gate")
        self.assertEqual(result["gateway_520_retries"], 2)
        self.assertEqual(len(waits), 2)
        self.assertTrue(all(r == requests[0] for r in requests))
        self.assertEqual((requests[0]["model"], requests[0]["reasoning_effort"]), (JUDGE_MODEL, JUDGE_REASONING_EFFORT))
        for status, expected in ((520, 3), (401, 1)):
            calls = []
            def failing(request):
                calls.append(request)
                return httpx.Response(status, text="private upstream body")
            judge = GatewayRetryJudge(httpx.Client(transport=httpx.MockTransport(failing)), sleep=lambda _: None)
            with self.assertRaises(ProviderError) as caught:
                judge.complete(system="frozen", payload={}, schema=schema("gate"), name="gate")
            self.assertEqual(len(calls), expected)
            self.assertNotIn("private upstream body", str(caught.exception))

    def test_judge_recovery_requires_all_searches_and_only_inspected_520_errors(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            plan = {"roles": [{"role_id": "test-role"}]}
            with self.assertRaisesRegex(ValueError, "all original searches"):
                validate_gateway_recovery(root, plan)
            result = {"fixture": True}
            atomic_json(root / "units/search/fixture.json", {"key": "test-role__1", "status": "complete",
                        "result": result, "result_sha256": digest(result)})
            inputs = {"model": JUDGE_MODEL, "reasoning_effort": JUDGE_REASONING_EFFORT}
            unit = {"key": "judgment", "attempt": 1, "status": "uncertain", "inputs": inputs,
                    "input_sha256": digest(inputs), "error": {"code": "OPENAI_HTTP_ERROR", "status": 520}}
            path = root / "units/relevance/fixture.json"
            atomic_json(path, unit)
            self.assertEqual(len(validate_gateway_recovery(root, plan)), 1)
            for status, error in (("started", unit["error"]), ("uncertain", {"code": "OPENAI_SCHEMA_REJECTED", "status": None})):
                atomic_json(path, {**unit, "status": status, "error": error})
                with self.assertRaisesRegex(ValueError, "need inspection"):
                    validate_gateway_recovery(root, plan)

    def adapter(self, root, body, *, total=0, key="test-role__1", plan_hash="frozen"):
        server = Server(total=total, phases=["idle"])
        checkpoint = root / "provider-state" / f"{digest(key)}.json"
        record = {"search_id": "search-1", "body_sha256": digest(body), "status": "polling",
                  "started_at": "2026-10-15T00:00:00Z", "started_epoch": 1000}
        atomic_json(checkpoint, record)
        adapter = AuthorizedFailureSearch(root=root, plan_hash=plan_hash, role_id="test-role",
            client=httpx.Client(transport=httpx.MockTransport(server)),
            wall_clock=lambda: 2000, sleep=lambda _: None)
        return adapter, server, checkpoint, record

    def test_known_failure_is_read_once_then_zeroed_without_changing_checkpoint(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            body = search_body("engineer", "source")
            adapter, server, checkpoint, original = self.adapter(root, body)
            result = adapter.search(body, "test-role__1")
            self.assertEqual(result["returned_count"], 0)
            self.assertEqual(result["meta"]["outcome"], "provider_failure_scored_zero")
            self.assertEqual(server.creates, 0)
            self.assertEqual(server.offsets, [0])
            self.assertEqual(read_json(checkpoint), original)
            calls = len(server.calls)
            self.assertEqual(adapter.search(body, "test-role__1"), result)
            self.assertEqual(len(server.calls), calls)
            self.assertNotIn("test-secret", adapter.receipt_path.read_text())
            original["search_id"] = "different-job"
            atomic_json(checkpoint, original)
            with self.assertRaisesRegex(ValueError, "receipt differs"):
                adapter.search(body, "test-role__1")

    def test_successful_retry_is_kept_and_not_zeroed(self):
        with tempfile.TemporaryDirectory() as temp:
            body = search_body("engineer", "source")
            adapter, server, _, _ = self.adapter(Path(temp), body, total=2)
            self.assertEqual(adapter.search(body, "test-role__1")["returned_count"], 2)
            self.assertFalse(adapter.receipt_path.exists())
            self.assertEqual(server.creates, 0)

    def test_unknown_creation_and_unapproved_role_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            body = search_body("engineer", "source")
            adapter, server, path, record = self.adapter(root, body)
            record.pop("search_id")
            atomic_json(path, record)
            with self.assertRaisesRegex(ValueError, "known original search"):
                adapter.search(body, "test-role__1")
            self.assertEqual(server.calls, [])
            adapter, server, _, _ = self.adapter(root, body, key="other-role__1")
            with self.assertRaisesRegex(ProviderError, "EMPTY_OR_AWAITING_INPUT"):
                adapter.search(body, "other-role__1")
            self.assertFalse(adapter.receipt_path.exists())
            self.assertEqual(server.creates, 0)

    def test_resume_retains_failed_attempt_and_scores_zero_without_judge_calls(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            data, _ = dataset(base / "dataset")
            envelope = make_plan(data, provider="metaview", mode="source", as_of="2026-09-30")
            role = envelope["plan"]["roles"][0]
            root = base / "runs" / "fixture"
            adapter, server, _, _ = self.adapter(root, role["search_body"], plan_hash=envelope["plan_sha256"])
            inputs = {"plan_sha256": envelope["plan_sha256"], "role_id": role["role_id"], "repeat": 1,
                      "base_url": envelope["plan"]["api_base_url"], "body": role["search_body"]}
            def fail(attempt):
                raise ProviderError("METAVIEW_EMPTY_OR_AWAITING_INPUT_INSPECT_SEARCH")
            with self.assertRaises(ProviderError):
                execute_unit(root, "search", "test-role__1", inputs, fail)
            judge = Judge()
            report = run(envelope, data, base / "runs", "fixture", search_client=adapter, judge=judge,
                         retry_uncertain=("search",), interval_seconds=0, progress=lambda _: None)
            annotated = annotate_failures(report, root)
            self.assertEqual(annotated["status"], "complete")
            self.assertTrue(annotated["overall"]["scoreable"])
            self.assertEqual(annotated["overall"]["successful_searches"], 0)
            self.assertEqual(annotated["overall"]["failed_searches_scored_zero"], 1)
            self.assertEqual(set(annotated["overall"]["metrics"].values()), {0})
            self.assertIsNone(annotated["overall"]["avg_latency_ms"])
            self.assertEqual(annotated["searches"][0]["search_status"], "provider_failure_scored_zero")
            unit = read_json(root / "units/search" / f"{digest('test-role__1')}.json")
            self.assertEqual(unit["previous_attempts"][0]["status"], "uncertain")
            calls = len(server.calls)
            run(envelope, data, base / "runs", "fixture", search_client=adapter, judge=judge,
                interval_seconds=0, progress=lambda _: None)
            self.assertEqual(len(server.calls), calls)
            self.assertEqual(server.creates, 0)
            self.assertEqual(judge.calls, [])

    def test_failure_stays_in_quality_denominator_but_not_latency_or_success_count(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            body = search_body("engineer", "source")
            adapter, _, _, _ = self.adapter(root, body)
            adapter.search(body, "test-role__1")
            coverage = {"location": 0, "educations": 0, "experiences": 0, "listed_skills": 0}
            failed = {"role_id": "test-role", "repeat": 1, "category": "Software", "search_status": "complete",
                      "latency_ms": 999999, "field_coverage_counts": coverage, **quality_metrics([], 0)}
            successful = {**failed, "role_id": "other", "latency_ms": 50,
                          **quality_metrics([{"rank": 1, "status": "complete", "relevance": 1}], 1)}
            report = {"plan_sha256": "frozen", **summarize([failed, successful], 2)}
            adjusted = annotate_failures(report, root)
            self.assertEqual(adjusted["overall"]["metrics"]["top1_relevance"], .5)
            self.assertEqual(adjusted["overall"]["successful_searches"], 1)
            self.assertEqual(adjusted["overall"]["avg_latency_ms"], 50)
            self.assertEqual(adjusted["overall"]["avg_candidates_returned"], 1)
            self.assertEqual(report["searches"][0]["search_status"], "complete")
            lite = {"plan_sha256": "frozen", **summarize([successful], 1)}
            self.assertEqual(annotate_failures(lite, root), lite)
