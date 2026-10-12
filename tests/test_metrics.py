import unittest

from benchmark.metrics import quality_metrics, summarize


def metrics(labels, unknown_policy="zero"):
    rows = [{"rank": i, "status": "complete", "label": "gate_fail" if label is None else "fixture", "relevance": 0 if label is None else label,
             "failed_criteria": ["M1"] if label is None else []} for i, label in enumerate(labels, 1)]
    return quality_metrics(rows, len(rows), unknown_policy=unknown_policy, top1_definition="top-result-relevance")


class MetricsTests(unittest.TestCase):
    def test_fixed_denominators_and_short_response_padding(self):
        value = metrics([1, 0, 1, 1])
        self.assertEqual(value["metrics"], {"top1_relevance": 1, "p@10": .3, "p@50": .06, "p@100": .03, "p@200": .015})
        self.assertEqual(value["coverage@200"], .02)

    def test_empty_success_is_zero_and_does_not_divide_by_zero(self):
        self.assertTrue(all(value == 0 for value in metrics([])["metrics"].values()))

    def test_gate_failures_are_counted_by_criterion(self):
        value = metrics([None, 1])
        self.assertEqual(value["gate_fail_count"], 1)
        self.assertEqual(value["failed_criteria_counts"], {"M1": 1})
        self.assertEqual(value["metrics"]["top1_relevance"], 0)
        self.assertTrue(value["scoreable"])
        with self.assertRaises(ValueError):
            metrics([None, 1], "incomplete")

    def test_partial_credit_is_not_rounded_to_a_boolean(self):
        value = metrics([.5, 1, 0, .5])
        self.assertEqual(value["metrics"], {"top1_relevance": .5, "p@10": .2, "p@50": .04, "p@100": .02, "p@200": .01})

    def test_completed_missing_or_out_of_scale_scores_are_rejected(self):
        for credit in (None, True, .25, 2, -1):
            with self.subTest(credit=credit), self.assertRaises(ValueError):
                quality_metrics([{"rank": 1, "status": "complete", "relevance": credit}], 1)

    def test_inconsistent_rating_or_failed_gate_credit_is_rejected(self):
        for row in ({"experience_verdict": "partial", "relevance": 1}, {"experience_verdict": "pass", "relevance": .5},
                    {"label": "gate_fail", "relevance": .5}, {"failed_criteria": ["M1"], "relevance": 1}):
            with self.subTest(row=row), self.assertRaises(ValueError):
                quality_metrics([{"rank": 1, "status": "complete", **row}], 1)

    def test_partial_gate_credit_is_capped_and_counted(self):
        row = {"rank": 1, "status": "complete", "gate_outcome": "partial", "implied_criteria": ["M2"], "experience_verdict": "pass", "relevance": .5}
        result = quality_metrics([row], 1)
        self.assertEqual((result["metrics"]["top1_relevance"], result["gate_partial_count"], result["implied_criteria_counts"]), (.5, 1, {"M2": 1}))
        with self.assertRaises(ValueError):
            quality_metrics([{**row, "relevance": 1}], 1)

    def test_overqualified_credit_is_zero_and_reported_separately(self):
        row = {"rank": 1, "status": "complete", "experience_verdict": "fail", "obviously_overqualified": True, "relevance": 0}
        result = quality_metrics([row], 1)
        self.assertEqual(result["overqualified_count"], 1)
        self.assertEqual(result["metrics"]["top1_relevance"], 0)
        with self.assertRaises(ValueError):
            quality_metrics([{**row, "experience_verdict": "pass", "relevance": 1}], 1)

    def test_underqualified_credit_is_zero_and_reported_separately(self):
        row = {"rank": 1, "status": "complete", "experience_verdict": "fail", "underqualified": True, "relevance": 0}
        result = quality_metrics([row], 1)
        self.assertEqual((result["underqualified_count"], result["overqualified_count"]), (1, 0))
        with self.assertRaises(ValueError):
            quality_metrics([{**row, "experience_verdict": "pass", "relevance": 1}], 1)

    def test_judge_error_is_not_zero_relevance(self):
        value = quality_metrics([{"rank": 1, "status": "error", "relevance": None}], 1,
                                unknown_policy="zero", top1_definition="top-result-relevance")
        self.assertFalse(value["scoreable"])
        self.assertIsNone(value["metrics"]["p@200"])

    def test_missing_rank_rejected(self):
        with self.assertRaises(ValueError):
            quality_metrics([{"rank": 2, "status": "complete", "relevance": 1}], 1,
                            unknown_policy="zero", top1_definition="top-result-relevance")

    def test_latency_per_role_and_incomplete_global_result(self):
        rows = [{"role_id": "r1", "category": "c1", "search_status": "complete", "latency_ms": latency, **metrics([1])} for latency in [100, 300]]
        value = summarize(rows, 2)
        # The fastest successful pull per query counts, not the mean of repetitions.
        self.assertEqual(value["by_role"]["r1"]["avg_latency_ms"], 100)
        self.assertEqual(value["status"], "complete")
        rows.append({"role_id": "r3", "category": "c1", "search_status": "complete", "latency_ms": 500, **metrics([1])})
        rows.append({"role_id": "r2", "category": "c2", "search_status": "failed", "scoreable": False})
        value = summarize(rows, 4)
        self.assertEqual(value["overall"]["avg_latency_ms"], 300)
        self.assertEqual(value["by_category"]["c1"]["avg_latency_ms"], 300)
        self.assertIsNone(value["by_role"]["r2"]["avg_latency_ms"])
        self.assertIsNone(value["overall"]["metrics"]["p@10"])
