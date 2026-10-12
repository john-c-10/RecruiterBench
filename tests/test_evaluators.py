from pathlib import Path
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

from benchmark.evaluators import Evaluator, location_alternatives, validate_gate, validate_relevance
import jsonschema
from benchmark.profiles import prepare_candidates
from benchmark.state import ValidationRejected
from support import Judge, gate, rubric


class EvaluatorTests(unittest.TestCase):
    def setUp(self):
        self.profile = prepare_candidates([{"id": "person-1", "location": "United States", "skills": ["Python"],
                                           "experience": ["Built Python services"],
                                           "name": "Hidden", "matchScore": 100, "evaluation": "PASS"}])[0]
        self.role = {"title": "Engineer", "job_description": "Build Python services. Fluent English and 3-5 years required."}

    def candidate(self, person):
        return prepare_candidates([{"id": person, "location": "United States", "experience": [f"Built Python services at {person}"]}])[0]

    def test_gate_failure_skips_second_reviewer(self):
        with tempfile.TemporaryDirectory() as tmp:
            judge = Judge("fail")
            result = Evaluator(judge, Path(tmp), "2026-09-30").evaluate(self.role, rubric(), self.profile)
            self.assertEqual(result["label"], "gate_fail")
            self.assertEqual(result["failed_criteria"], ["M1"])
            self.assertEqual(len(judge.calls), 1)
            self.assertIsNone(result["review"])
            self.assertEqual(result["relevance"], 0.0)
            self.assertIsNone(result["experience_verdict"])

    def test_passing_profile_gets_blind_second_stage_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            judge = Judge()
            result = Evaluator(judge, Path(tmp), "2026-09-30").evaluate(self.role, rubric(), self.profile)
            self.assertEqual(result["relevance"], 1)
            self.assertEqual(len(judge.calls), 2)
            payload = judge.calls[1]["payload"]
            self.assertEqual(set(payload), {"as_of", "role", "profile"})
            self.assertEqual(set(payload["profile"]), {"location", "educations", "experiences", "listed_skills"})
            self.assertNotIn("gate", payload)
            self.assertEqual(set(payload["role"]), {"title", "role_summary", "must_haves", "experience_context", "job_description"})
            self.assertEqual(payload["role"]["must_haves"], [{"id": "M1", "requirement": "Python experience", "verification": "profile"}])
            self.assertEqual(payload["role"]["job_description"], self.role["job_description"])
            self.assertNotIn("preferences", payload["role"])
            self.assertNotIn("rank", str(payload))
            self.assertNotIn("Hidden", str(payload))

    def test_gate_verdict_must_match_failed_criteria(self):
        for value in ({"verdict": "fail", "failed_criteria": [], "implied_criteria": [], "description": "x"},
                      {"verdict": "pass", "failed_criteria": ["M1"], "implied_criteria": [], "description": "x"},
                      {"verdict": "fail", "failed_criteria": ["M9"], "implied_criteria": [], "description": "x"},
                      {"verdict": "fail", "failed_criteria": ["M1", "M1"], "implied_criteria": [], "description": "x"},
                      {"verdict": "maybe", "failed_criteria": [], "implied_criteria": [], "description": "x"},
                      {"verdict": "partial", "failed_criteria": [], "implied_criteria": [], "description": "x"},
                      {"verdict": "pass", "failed_criteria": [], "implied_criteria": ["M1"], "description": "x"},
                      {"verdict": "partial", "failed_criteria": ["M1"], "implied_criteria": ["M1"], "description": "x"},
                      {"verdict": "partial", "failed_criteria": [], "implied_criteria": ["D1"], "description": "x"}):
            with self.subTest(value=value), self.assertRaises((ValueError, jsonschema.ValidationError)):
                validate_gate(value, rubric())
        self.assertEqual(validate_gate({"verdict": "fail", "failed_criteria": ["location"], "implied_criteria": [], "description": "x"}, rubric())["verdict"], "fail")
        unrestricted = rubric(); unrestricted["location"]["required"] = False
        with self.assertRaises(ValueError):
            validate_gate({"verdict": "fail", "failed_criteria": ["location"], "implied_criteria": [], "description": "x"}, unrestricted)

    def test_partial_gate_runs_second_review_and_caps_credit_at_half(self):
        for experience, credit in [("pass", .5), ("partial", .5), ("fail", 0.0)]:
            with self.subTest(experience=experience), tempfile.TemporaryDirectory() as tmp:
                judge = Judge("partial", experience=experience)
                result = Evaluator(judge, Path(tmp), "2026-09-30").evaluate(self.role, rubric(), self.profile)
                self.assertEqual(len(judge.calls), 2)
                self.assertEqual((result["gate_outcome"], result["implied_criteria"], result["relevance"]), ("partial", ["M1"], credit))
        self.assertEqual(validate_gate({"verdict": "partial", "failed_criteria": [], "implied_criteria": ["M1"], "description": "x"}, rubric())["verdict"], "partial")

    def test_location_alternatives_are_explicit_or_rules_without_arrangement_narrowing(self):
        alternatives = location_alternatives(["United States (remote)", "SF Bay Area (hybrid option)", "Greater Berlin, Germany area", "France, preferred"])
        self.assertEqual([(a["place"], a["rule"].split()[0]) for a in alternatives],
                         [("United States", "anywhere"), ("SF Bay Area", "within"), ("Greater Berlin, Germany area", "within"), ("France", "anywhere")])
        self.assertEqual(alternatives[0]["arrangement"], "remote")
        with tempfile.TemporaryDirectory() as tmp:
            judge = Judge()
            Evaluator(judge, Path(tmp), "2026-09-30").evaluate(self.role, rubric(), self.profile)
            self.assertEqual(judge.calls[0]["payload"]["role"]["rubric"]["location"]["alternatives"],
                             [{"place": "United States", "rule": "anywhere inside this country, state, or region"}])

    def test_fail_may_note_implied_criteria_without_changing_zero_credit(self):
        value = {"verdict": "fail", "failed_criteria": ["M1"], "implied_criteria": ["M1"], "description": "x"}
        self.assertEqual(validate_gate(value, rubric())["verdict"], "fail")

    def test_coarse_location_can_be_a_partial_gate(self):
        value = {"verdict": "partial", "failed_criteria": [], "implied_criteria": ["location"], "description": "x"}
        self.assertEqual(validate_gate(value, rubric())["verdict"], "partial")
        unrestricted = rubric(); unrestricted["location"]["required"] = False
        with self.assertRaises(ValueError):
            validate_gate(value, unrestricted)

    def test_duplicate_is_zero_without_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            judge = Judge(); candidate = {**self.profile, "duplicate_of_rank": 1}
            result = Evaluator(judge, Path(tmp), "2026-09-30").evaluate(self.role, rubric(), candidate)
            self.assertEqual(result["relevance"], 0)
            self.assertEqual(judge.calls, [])

    def test_experience_verdicts_map_to_requested_credit(self):
        for verdict, credit in [("fail", 0.0), ("partial", .5), ("pass", 1.0)]:
            with self.subTest(verdict=verdict), tempfile.TemporaryDirectory() as tmp:
                judge = Judge(experience=verdict)
                result = Evaluator(judge, Path(tmp), "2026-09-30").evaluate(self.role, rubric(), self.profile)
                self.assertEqual(result["experience_verdict"], verdict)
                self.assertEqual(result["label"], f"experience_{verdict}")
                self.assertEqual(result["relevance"], credit)
                self.assertEqual(len(judge.calls), 2)

    def test_invalid_experience_verdict_rejected(self):
        for verdict in ("maybe", 5, None, True):
            with self.subTest(verdict=verdict), self.assertRaises(jsonschema.ValidationError):
                validate_relevance({"verdict": verdict, "underqualified": False, "obviously_overqualified": False, "description": "x"})

    def test_obvious_overqualification_fails_only_after_passing_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            judge = Judge(experience="fail", overqualified=True)
            result = Evaluator(judge, Path(tmp), "2026-09-30").evaluate(self.role, rubric(), self.profile)
            self.assertEqual(len(judge.calls), 2)
            self.assertEqual(result["relevance"], 0)
            self.assertEqual(result["label"], "experience_overqualified")
            self.assertTrue(result["obviously_overqualified"])

    def test_overqualified_flag_cannot_receive_partial_or_full_credit(self):
        for verdict in ("partial", "pass"):
            with self.assertRaises(ValueError):
                validate_relevance({"verdict": verdict, "underqualified": False, "obviously_overqualified": True, "description": "x"})

    def test_underqualification_fails_after_passing_gate_and_is_labeled(self):
        with tempfile.TemporaryDirectory() as tmp:
            judge = Judge(experience="fail", underqualified=True)
            result = Evaluator(judge, Path(tmp), "2026-09-30").evaluate(self.role, rubric(), self.profile)
            self.assertEqual(len(judge.calls), 2)
            self.assertEqual(result["relevance"], 0)
            self.assertEqual(result["label"], "experience_underqualified")
            self.assertTrue(result["underqualified"])

    def test_underqualified_flag_requires_fail_and_excludes_overqualified(self):
        for verdict, over in (("partial", False), ("pass", False), ("fail", True)):
            with self.subTest(verdict=verdict, over=over), self.assertRaises(ValueError):
                validate_relevance({"verdict": verdict, "underqualified": True, "obviously_overqualified": over, "description": "x"})

    def test_inconsistent_gate_gets_one_repair_then_definite_failure(self):
        class Inconsistent(Judge):
            def __init__(self, bad_calls):
                super().__init__(); self.bad_calls = bad_calls
            def complete(self, **kwargs):
                result = super().complete(**kwargs)
                if kwargs["name"].startswith("gate") and len(self.calls) <= self.bad_calls:
                    result["value"]["failed_criteria"] = ["M1"]
                return result
        with tempfile.TemporaryDirectory() as tmp:
            judge = Inconsistent(1)
            result = Evaluator(judge, Path(tmp), "2026-09-30").evaluate(self.role, rubric(), self.profile)
            self.assertEqual(result["relevance"], 1)
            self.assertEqual(result["gate"]["validation_repairs"], 1)
            self.assertIn("repair_request", judge.calls[1]["payload"])
            self.assertEqual(len(judge.calls), 3)
        with tempfile.TemporaryDirectory() as tmp:
            judge = Inconsistent(2)
            with self.assertRaises(ValidationRejected):
                Evaluator(judge, Path(tmp), "2026-09-30").evaluate(self.role, rubric(), self.profile)
            self.assertEqual(len(judge.calls), 2)
            self.assertEqual(len(list(Path(tmp, "rejected", "gate").iterdir())), 2)

    def test_gate_sees_effective_requirements_without_source_quotes(self):
        with tempfile.TemporaryDirectory() as tmp:
            judge = Judge()
            Evaluator(judge, Path(tmp), "2026-09-30").evaluate(self.role, rubric(), self.profile)
            sent = judge.calls[0]["payload"]["role"]["rubric"]
            self.assertEqual(set(sent), {"role_summary", "must_haves", "red_flags", "location"})
            self.assertEqual(set(sent["must_haves"][0]), {"id", "requirement", "verification"})
            self.assertNotIn("source_quotes", sent["location"])

    def test_cache_key_is_shared_per_role_and_stage_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            judge = Judge(); evaluator = Evaluator(judge, Path(tmp), "2026-09-30")
            for person in ("a", "b"):
                evaluator.evaluate(self.role, rubric(), self.candidate(person))
            other = {**self.role, "title": "Other"}
            evaluator.evaluate(other, rubric(), self.candidate("c"))
            keys = [(call["name"], call["cache_key"]) for call in judge.calls]
            gate_keys = [key for name, key in keys if name.startswith("gate")]
            review_keys = [key for name, key in keys if name.startswith("relevance")]
            self.assertEqual(gate_keys[0], gate_keys[1])
            self.assertNotEqual(gate_keys[0], gate_keys[2])
            self.assertEqual(review_keys[0], review_keys[1])
            self.assertNotEqual(gate_keys[0], review_keys[0])
            self.assertTrue(all(key.startswith(("gate-", "relevance-")) and len(key) <= 64 for _, key in keys))

    def test_first_call_per_prefix_runs_alone_before_fan_out(self):
        class Slow(Judge):
            def __init__(self):
                super().__init__(); self.guard = threading.Lock(); self.warming = set(); self.overlaps = 0; self.started = {}
            def complete(self, **kwargs):
                key = kwargs["cache_key"]
                with self.guard:
                    self.overlaps += key in self.warming
                    if not self.started.get(key):
                        self.warming.add(key)
                    self.started[key] = self.started.get(key, 0) + 1
                time.sleep(.02)
                with self.guard:
                    self.warming.discard(key)
                return super().complete(**kwargs)
        with tempfile.TemporaryDirectory() as tmp:
            judge = Slow(); evaluator = Evaluator(judge, Path(tmp), "2026-09-30")
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(lambda person: evaluator.evaluate(self.role, rubric(), self.candidate(person)), "abcdefgh"))
            self.assertEqual(judge.overlaps, 0)
            self.assertEqual(len(judge.calls), 16)

    def test_concurrent_identical_evidence_is_judged_once_per_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            judge = Judge(); evaluator = Evaluator(judge, Path(tmp), "2026-09-30")
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(lambda _: evaluator.evaluate(self.role, rubric(), self.profile), range(8)))
            self.assertEqual(len(judge.calls), 2)
            self.assertTrue(all(result["relevance"] == 1 for result in results))
