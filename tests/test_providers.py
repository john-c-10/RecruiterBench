import os
import json
import unittest
from unittest.mock import patch
import httpx

from benchmark.providers.openai import OpenAIJudge, ProviderError
from benchmark.providers import exa
from benchmark.providers.wrangle import WrangleSearch, origin, search_body
from benchmark.models import schema
from support import gate


class ProviderTests(unittest.TestCase):
    @patch.dict(os.environ, {"WRANGLE_API_KEY": "test-only"})
    def test_explicit_200_request_and_null_connector_metadata(self):
        seen = []
        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={"candidates": [], "meta": {"mode": "fast", "limit": 200, "connectors": None}})
        client = httpx.Client(transport=httpx.MockTransport(handler))
        value = WrangleSearch("https://example.test", client).search(search_body("Python", "fast"), "fixture")
        self.assertEqual(value["returned_count"], 0)
        self.assertEqual(json.loads(seen[0].content)["limit"], 200)
        self.assertEqual(seen[0].headers["Idempotency-Key"], "fixture")

    @patch.dict(os.environ, {"WRANGLE_API_KEY": "test-only"})
    def test_lone_surrogates_are_replaced(self):
        body = b'{"candidates": [{"id": "p1", "headline": "\\ud835 Founder", "experiences": [{"title": "CTO"}]}], "meta": {"mode": "fast", "limit": 200}}'
        client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body)))
        value = WrangleSearch("https://example.test", client).search(search_body("Python", "fast"), "fixture")
        self.assertEqual(value["response"]["candidates"][0]["headline"], "\ufffd Founder")
        json.dumps(value, ensure_ascii=False).encode()

    @patch.dict(os.environ, {"WRANGLE_API_KEY": "test-only"})
    def test_mode_or_limit_drift_rejected(self):
        for meta in ({"mode": "normal", "limit": 200}, {"mode": "fast", "limit": 100}):
            client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"candidates": [], "meta": meta})))
            with self.assertRaisesRegex(ProviderError, "CONTRACT_REJECTED"):
                WrangleSearch("https://example.test", client).search(search_body("Python", "fast"), "fixture")

    @patch.dict(os.environ, {"EXA_API_KEY": "test-only"})
    def test_exa_people_search_maps_person_entities_to_profile_fields(self):
        seen = []
        result = {"id": "https://www.linkedin.com/in/fixture", "url": "https://www.linkedin.com/in/fixture", "title": "Fixture",
                  "entities": [{"id": "person-1", "type": "person", "version": 1, "properties": {
                      "name": "Never judged", "location": "Austin, Texas, United States",
                      "workHistory": [{"title": "Bookkeeper", "location": None, "dates": {"from": "2019-01", "to": None},
                                       "company": {"id": "c1", "name": "Example LLC"}}],
                      "educationHistory": [{"degree": "BS Accounting", "dates": None, "institution": {"id": "s1", "name": "Example University"}}]}}]}
        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={"requestId": "r1", "results": [result, result], "searchTime": 812.5, "costDollars": {"total": 0.01}})
        client = httpx.Client(transport=httpx.MockTransport(handler))
        value = exa.ExaSearch(client=client).search(exa.search_body("Bookkeeper", "auto"), "fixture")
        sent = json.loads(seen[0].content)
        self.assertEqual((sent["category"], sent["numResults"], sent["type"]), ("people", 100, "auto"))
        self.assertEqual(seen[0].headers["x-api-key"], "test-only")
        self.assertEqual((value["returned_count"], value["api_latency_ms"], value["cost_dollars"]), (2, 812.5, 0.01))
        first, second = value["candidates"]
        self.assertEqual(first["profile"]["location"], "Austin, Texas, United States")
        self.assertEqual(first["profile"]["experiences"], [{"title": "Bookkeeper", "company": "Example LLC", "location": None,
                                                           "start_date": "2019-01", "end_date": None}])
        self.assertEqual(first["profile"]["educations"][0]["school"], "Example University")
        self.assertEqual(first["profile"]["listed_skills"], [])
        self.assertNotIn("Never judged", json.dumps(first["profile"]))
        self.assertEqual(second["duplicate_of_rank"], 1)

    @patch.dict(os.environ, {"EXA_API_KEY": "test-only"})
    def test_exa_library_page_without_entity_keeps_its_slot(self):
        result = {"url": "https://exa.ai/library/person/yhdd9gc6rfj", "entities": []}
        client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"results": [result]})))
        value = exa.ExaSearch(client=client).search(exa.search_body("Bookkeeper", "auto"), "fixture")
        self.assertEqual(value["returned_count"], 1)
        self.assertEqual(value["candidates"][0]["identity_keys"], ["person:exa-library:yhdd9gc6rfj"])
        self.assertFalse(any(value["candidates"][0]["field_coverage"].values()))

    @patch.dict(os.environ, {"EXA_API_KEY": "test-only"})
    def test_exa_rejects_unidentifiable_results_and_unknown_modes(self):
        client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"results": [{"url": "https://example.test/page"}]})))
        with self.assertRaisesRegex(ProviderError, "CONTRACT_REJECTED"):
            exa.ExaSearch(client=client).search(exa.search_body("Bookkeeper", "auto"), "fixture")
        with self.assertRaises(ValueError):
            exa.search_body("Bookkeeper", "turbo")

    def test_origin_has_no_redirect_or_embedded_secret_surface(self):
        for url in ("http://example.test", "https://key@example.test", "https://example.test/path", "https://example.test?q=key"):
            with self.assertRaises(ValueError): origin(url)

    @patch.dict(os.environ, {"OPENAI_API_KEY": "test-only"})
    def test_judge_pins_model_low_reasoning_and_rejects_fallback(self):
        import json
        requests = []
        def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={"model": "different-model", "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(gate())}}]})
        judge = OpenAIJudge(httpx.Client(transport=httpx.MockTransport(handler)))
        with self.assertRaisesRegex(ProviderError, "MODEL_MISMATCH"):
            judge.complete(system="fixture", payload={}, schema=schema("gate"), name="gate")
        self.assertEqual(requests[0]["model"], "gpt-6.1-sol")
        self.assertEqual(requests[0]["reasoning_effort"], "low")
        self.assertFalse(requests[0]["store"])

    @patch.dict(os.environ, {"OPENAI_API_KEY": "test-only"})
    def test_reviewers_use_luna_medium_while_extraction_keeps_its_model(self):
        from benchmark.models import JUDGE_MODEL, JUDGE_REASONING_EFFORT
        self.assertEqual((JUDGE_MODEL, JUDGE_REASONING_EFFORT), ("gpt-6-luna", "medium"))
        requests = []
        def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={"model": "gpt-6-luna", "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(gate())}}]})
        judge = OpenAIJudge(httpx.Client(transport=httpx.MockTransport(handler)), model=JUDGE_MODEL, reasoning_effort=JUDGE_REASONING_EFFORT)
        result = judge.complete(system="fixture", payload={}, schema=schema("gate"), name="gate")
        self.assertEqual((requests[0]["model"], requests[0]["reasoning_effort"]), ("gpt-6-luna", "medium"))
        self.assertEqual((result["requested_model"], result["reasoning_effort"]), ("gpt-6-luna", "medium"))

    @patch.dict(os.environ, {"OPENAI_API_KEY": "do-not-output"})
    @patch.dict(os.environ, {"OPENAI_API_KEY": "test-only"})
    def test_cache_key_is_sent_only_when_given(self):
        requests = []
        def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={"model": "gpt-6.1-sol", "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(gate())}}]})
        judge = OpenAIJudge(httpx.Client(transport=httpx.MockTransport(handler)))
        judge.complete(system="fixture", payload={}, schema=schema("gate"), name="gate", cache_key="gate-abc")
        judge.complete(system="fixture", payload={}, schema=schema("gate"), name="gate")
        self.assertEqual(requests[0]["prompt_cache_key"], "gate-abc")
        self.assertNotIn("prompt_cache_key", requests[1])

    @patch.dict(os.environ, {"OPENAI_API_KEY": "test-only"})
    def test_transient_failures_are_retried_then_succeed(self):
        responses = iter([httpx.ConnectTimeout("slow"), httpx.Response(500), httpx.Response(429, headers={"retry-after": "1"}),
                          httpx.Response(200, json={"model": "gpt-6.1-sol", "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(gate())}}]})])
        def handler(request):
            item = next(responses)
            if isinstance(item, Exception):
                raise item
            return item
        waits = []
        judge = OpenAIJudge(httpx.Client(transport=httpx.MockTransport(handler)), sleep=waits.append)
        result = judge.complete(system="fixture", payload={}, schema=schema("gate"), name="gate")
        self.assertEqual(result["transport_attempts"], 4)
        self.assertEqual(len(waits), 3)
        self.assertEqual(waits[2], 1.0)

    @patch.dict(os.environ, {"OPENAI_API_KEY": "test-only"})
    def test_retries_are_bounded_and_client_errors_are_not_retried(self):
        calls = []
        def failing(status):
            def handler(request):
                calls.append(status)
                return httpx.Response(status)
            return handler
        with self.assertRaisesRegex(ProviderError, "HTTP 503"):
            OpenAIJudge(httpx.Client(transport=httpx.MockTransport(failing(503))), retries=2, sleep=lambda _: None).complete(
                system="fixture", payload={}, schema=schema("gate"), name="gate")
        self.assertEqual(calls.count(503), 3)
        with self.assertRaisesRegex(ProviderError, "HTTP 400"):
            OpenAIJudge(httpx.Client(transport=httpx.MockTransport(failing(400))), sleep=lambda _: None).complete(
                system="fixture", payload={}, schema=schema("gate"), name="gate")
        self.assertEqual(calls.count(400), 1)

    def test_error_does_not_contain_upstream_body(self):
        client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(401, text="sensitive upstream body")))
        with self.assertRaises(ProviderError) as caught:
            OpenAIJudge(client).complete(system="fixture", payload={}, schema=schema("gate"), name="gate")
        self.assertNotIn("sensitive", str(caught.exception))
        self.assertNotIn("do-not-output", str(caught.exception))
