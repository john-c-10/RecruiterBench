from pathlib import Path
import tempfile
import unittest

from benchmark.browser_evaluation import aggregate, evaluate_collection, read_collection, read_result
from benchmark.evaluators import Evaluator
from benchmark.io import atomic_json
from support import Judge, rubric


class BrowserEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.role = {"role_id": "role", "title": "Engineer", "rubric": rubric(),
                     "job_description": "Python experience", "search_body": {"query": "Frozen brief"}}
        self.judge = Judge()
        self.reviewer = Evaluator(self.judge, self.root, "2026-09-30")

    def page(self, n, candidates, complete=False, **extra):
        page = {"role_id": "role", "page": n, "query": "Frozen brief",
                "query_started_at": "2026-10-15T00:00:00Z", "collected_at": f"2026-10-15T00:00:{n:02d}Z",
                "source_url": "https://app.juicebox.ai/project/example", "complete": complete,
                "candidates": candidates, **extra}
        atomic_json(self.root / "browser" / "role" / f"page-{n:03d}.json", page)
        return page

    def candidate(self, identity="p1"):
        return {"id": identity, "location": "United States", "experiences": ["Built Python services"],
                "educations": [], "listed_skills": ["Python"]}

    def test_pages_judge_incrementally_and_duplicates_keep_rank(self):
        self.page(1, [self.candidate()])
        first = read_collection(self.root, self.role)
        evaluate_collection(self.root, self.role, first, self.reviewer, "plan", 2)
        initial_calls = len(self.judge.calls)
        self.page(2, [self.candidate(), self.candidate("p2")], True, termination_reason="results_exhausted")
        second = read_collection(self.root, self.role)
        self.assertEqual(second["candidates"][1]["duplicate_of_rank"], 1)
        evaluate_collection(self.root, self.role, second, self.reviewer, "plan", 2)
        result = read_result(self.root, "role", "plan")
        self.assertEqual([row["rank"] for row in result["judgments"]], [1, 2, 3])
        self.assertEqual(result["judgments"][1]["label"], "duplicate")
        self.assertEqual(result["search_status"], "complete")
        # Profile evidence is identical, so the existing content cache is reused.
        self.assertEqual(len(self.judge.calls), initial_calls)
        self.assertFalse(evaluate_collection(self.root, self.role, second, self.reviewer, "plan", 2))

    def test_incomplete_collection_is_not_a_successful_search(self):
        self.page(1, [self.candidate()])
        collection = read_collection(self.root, self.role)
        evaluate_collection(self.root, self.role, collection, self.reviewer, "plan", 1)
        result = read_result(self.root, "role", "plan")
        self.assertEqual(result["search_status"], "collecting")
        summary = aggregate([{**result, "scoreable": False, "evaluated_count": 1}])
        self.assertEqual(summary["successful_searches"], 0)
        self.assertIsNone(summary["metrics"]["p@200"])
        self.assertEqual(summary["evaluated_profiles"], 1)
        self.assertIsNone(summary["avg_latency_ms"])

    def test_imported_page_changes_are_rejected_before_paid_calls(self):
        self.page(1, [self.candidate()])
        evaluate_collection(self.root, self.role, read_collection(self.root, self.role), self.reviewer, "plan", 1)
        before = len(self.judge.calls)
        self.page(1, [self.candidate("new")])
        with self.assertRaisesRegex(ValueError, "page changed"):
            evaluate_collection(self.root, self.role, read_collection(self.root, self.role), self.reviewer, "plan", 1)
        self.assertEqual(len(self.judge.calls), before)

    def test_cannot_mark_short_collection_target_reached(self):
        self.page(1, [self.candidate()], True, termination_reason="target_reached")
        with self.assertRaisesRegex(ValueError, "exactly 200"):
            read_collection(self.root, self.role)

    def test_query_must_match_frozen_brief(self):
        self.page(1, [self.candidate()], query="Different brief")
        with self.assertRaisesRegex(ValueError, "frozen"):
            read_collection(self.root, self.role)

    def test_cannot_skip_pages_or_append_after_completed_page(self):
        self.page(2, [self.candidate()])
        with self.assertRaisesRegex(ValueError, "contiguous"):
            read_collection(self.root, self.role)
        self.page(1, [self.candidate()], True, termination_reason="results_exhausted")
        with self.assertRaisesRegex(ValueError, "append"):
            read_collection(self.root, self.role)

    def test_no_experience_contract_is_rejected(self):
        candidate = self.candidate()
        candidate["experiences"] = []
        self.page(1, [candidate])
        with self.assertRaisesRegex(ValueError, "experience"):
            read_collection(self.root, self.role)


if __name__ == "__main__":
    unittest.main()
