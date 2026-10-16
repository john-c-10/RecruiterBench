import json
import os
import tempfile
import unittest
from unittest.mock import patch

import httpx

from benchmark.providers.openai import ProviderError
from tools.treg_parallel_provider import ParallelSearch, normalize_receipt, search_body
from tools.treg_provider import parallel_candidate


class ParallelTests(unittest.TestCase):
    @patch.dict(os.environ, {"TREG_API_KEY": "test-only"})
    def test_native_endpoint_body_order_cache_and_private_fields(self):
        calls = []
        entities = [{"name": "Not evidence", "url": "https://linkedin.com/in/fixture",
                     "description": "Experience: Engineer 2020-present | Country ISO: US | Skills: Python",
                     "email": "private@example.test"}]
        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={"entities": entities * 2, "entity_set_id": "set-1"},
                                  headers={"X-Treg-Call-Id": "call-1", "X-Treg-Cost-Micro": "5000"})
        with tempfile.TemporaryDirectory() as temp:
            client = ParallelSearch(state_root=temp, client=httpx.Client(transport=httpx.MockTransport(handler)))
            body = search_body("Find an engineer", "parallel")
            first = client.search(body, "key")
            self.assertEqual(first, client.search(body, "key"))
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0].url.path, "/call/parallel.people.search")
            self.assertEqual(json.loads(calls[0].content), {"entity_type": "people", "objective": "Find an engineer", "match_limit": 100})
            self.assertEqual(first["candidates"][1]["duplicate_of_rank"], 1)
            self.assertEqual(first["candidates"][0]["profile"]["location"], "US")
            self.assertEqual(first["meta"]["entity_set_id"], "set-1")
            from pathlib import Path
            self.assertNotIn("private@example.test", next(Path(temp).glob("*.json")).read_text())

    def test_missing_entities_is_not_an_empty_search(self):
        for response in [{}, {"entities": None}, {"entities": []},
                         {"entities": [{}] * 101, "entity_set_id": "set"}]:
            with self.assertRaisesRegex(ProviderError, "CONTRACT_REJECTED"):
                normalize_receipt({"status": 200, "response": response})
        result = normalize_receipt({"status": 200, "elapsed_ms": 1, "headers": {},
            "response": {"entities": [], "entity_set_id": "set"}})
        self.assertEqual(result["returned_count"], 0)

    def test_country_iso_fallback_is_explicit_evidence(self):
        row = {"description": "Country ISO: DE | Inferred Location: France"}
        self.assertEqual(parallel_candidate(row)["location"], "DE")
        row["description"] += " | Country Name: Germany"
        self.assertEqual(parallel_candidate(row)["location"], "Germany")

    def test_no_silent_expansion_to_two_hundred_or_provider_fallback(self):
        body = search_body("engineer", "parallel")
        body["match_limit"] = 200
        with self.assertRaises(ValueError):
            ParallelSearch.validate_body(body)
        with self.assertRaises(ValueError):
            search_body("engineer", "routed")

    def test_position_is_explicit_work_evidence_when_history_is_absent(self):
        row = {"description": "Title: Not evidence | Position: Acme Engineer Built data pipelines | Headline: Not evidence"}
        self.assertEqual(parallel_candidate(row)["experiences"], ["Acme Engineer Built data pipelines"])
        row["description"] += " | Experience: Acme Engineer 2020-present; PriorCo Engineer 2016-2020"
        self.assertEqual(parallel_candidate(row)["experiences"], ["Acme Engineer 2020-present; PriorCo Engineer 2016-2020"])


if __name__ == "__main__":
    unittest.main()
