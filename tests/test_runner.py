from pathlib import Path
import tempfile
from threading import Thread
import time
import unittest
import unittest.mock

from benchmark.io import atomic_json, read_json
from benchmark.profiles import prepare_candidates
from benchmark.runner import make_plan, rescore, run, validate_plan
from benchmark.providers.openai import ProviderError
from benchmark.state import Pacer
from support import Judge, dataset


class Search:
    def __init__(self, fail=False): self.calls, self.fail = [], fail
    def search(self, body, key):
        self.calls.append((body, key))
        if self.fail: raise ProviderError("WRANGLE_REQUEST_UNCERTAIN")
        return {"returned_count": 1, "elapsed_ms": 123, "api_latency_ms": 110, "backend_latency_ms": 100,
                "candidates": prepare_candidates([{"id": "fixture-person", "name": "Never exported", "skills": ["Python"], "experience": ["Built Python services"], "location": "United States"}])}


class RunnerTests(unittest.TestCase):
    def setup_run(self, root, repeats=1):
        data, role = dataset(root / "dataset")
        plan = make_plan(data, base_url="https://example.test", mode="fast", unknown_policy="zero",
                         top1_definition="top-result-relevance", as_of="2026-09-30", repeats=repeats)
        return data, plan

    def test_mocked_pipeline_resume_does_not_repeat_paid_calls(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); data, plan = self.setup_run(root)
            search, judge = Search(), Judge()
            for _ in range(2):
                result = run(plan, data, root / "runs", "fixture", search_client=search, judge=judge, interval_seconds=0, progress=lambda _: None)
            self.assertEqual(len(search.calls), 1)
            self.assertEqual(len(judge.calls), 2)
            self.assertEqual(result["overall"]["metrics"]["p@200"], .005)
            self.assertEqual(result["by_role"]["test-role"]["avg_latency_ms"], 123)
            self.assertNotIn("fixture-person", str(result))
            self.assertNotIn("Never exported", str(result))

    def test_parallel_roles_match_sequential_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); data, plan = self.setup_run(root)
            search, judge = Search(), Judge()
            result = run(plan, data, root / "runs", "fixture", search_client=search, judge=judge, parallel=4, judge_parallel=2, workers=32,
                         interval_seconds=0, progress=lambda _: None)
            self.assertEqual(len(search.calls), 1)
            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["overall"]["metrics"]["p@200"], .005)

    def test_pacer_spaces_concurrent_admissions(self):
        pacer, starts = Pacer(0.05), []
        threads = [Thread(target=lambda: (pacer.wait(), starts.append(time.monotonic()))) for _ in range(4)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        starts.sort()
        self.assertTrue(all(later - earlier >= 0.04 for earlier, later in zip(starts, starts[1:])))

    def test_rescore_replays_saved_searches_without_searching(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); data, plan = self.setup_run(root)
            search, judge = Search(), Judge()
            run(plan, data, root / "runs", "source", search_client=search, judge=judge, interval_seconds=0, progress=lambda _: None)
            class NoSearch:
                def search(self, body, key): raise AssertionError("rescore must not search")
            with unittest.mock.patch("benchmark.runner.provider_client", lambda *_: NoSearch()), \
                 unittest.mock.patch.dict("os.environ", {"WRANGLE_API_KEY": "", "OPENAI_API_KEY": "test-only"}):
                result = rescore(plan, data, root / "runs", "again", "source", judge=Judge(), progress=lambda _: None)
            self.assertEqual(len(search.calls), 1)
            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["search_replay"]["source_run_id"], "source")
            self.assertEqual(result["search_replay"]["roles_with_different_query"], [])
            with self.assertRaisesRegex(ValueError, "same search provider, origin"):
                other = make_plan(data, base_url="https://other.test", mode="fast", unknown_policy="zero",
                                  top1_definition="top-result-relevance", as_of="2026-09-30")
                rescore(other, data, root / "runs", "elsewhere", "source", judge=Judge(), progress=lambda _: None)

    def test_rescore_can_search_roles_missing_from_the_source_live(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); data, plan = self.setup_run(root)
            run(plan, data, root / "runs", "source", search_client=Search(True), judge=Judge(), interval_seconds=0, progress=lambda _: None)
            with self.assertRaisesRegex(ValueError, "search-missing"):
                rescore(plan, data, root / "runs", "again", "source", judge=Judge(), progress=lambda _: None)
            live = Search()
            with unittest.mock.patch("benchmark.runner.provider_client", lambda *_, **__: live), \
                 unittest.mock.patch.dict("os.environ", {"WRANGLE_API_KEY": "test-only", "OPENAI_API_KEY": "test-only"}):
                result = rescore(plan, data, root / "runs", "again", "source", search_missing=True, judge=Judge(),
                                 interval_seconds=0, progress=lambda _: None)
            self.assertEqual(len(live.calls), 1)
            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["search_replay"]["live_searches"], ["test-role"])

    def test_failed_search_stops_before_other_paid_searches(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); data, plan = self.setup_run(root, repeats=3)
            search, judge = Search(True), Judge()
            result = run(plan, data, root / "runs", "fixture", search_client=search, judge=judge, interval_seconds=0, progress=lambda _: None)
            self.assertEqual(len(search.calls), 1)
            self.assertEqual(len(judge.calls), 0)
            self.assertEqual(result["status"], "incomplete")
            self.assertIsNone(result["overall"]["metrics"]["top1_relevance"])

    def test_partial_credit_reaches_all_metrics_and_frozen_plan(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); data, plan = self.setup_run(root)
            result = run(plan, data, root / "runs", "fixture", search_client=Search(), judge=Judge(experience="partial"), interval_seconds=0, progress=lambda _: None)
            self.assertEqual(result["overall"]["metrics"], {"top1_relevance": .5, "p@10": .05, "p@50": .01, "p@100": .005, "p@200": .0025})
            self.assertEqual(result["searches"][0]["experience_verdict_counts"], {"pass": 0, "partial": 1, "fail": 0})
            self.assertEqual(plan["plan"]["scoring"]["verdict_credit"], {"pass": 1, "partial": .5, "fail": 0})

    def test_plan_freezes_job_description_for_second_reviewer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); data, plan = self.setup_run(root)
            self.assertEqual(plan["plan"]["roles"][0]["job_description"], "Python experience. United States.")
            judge = Judge()
            run(plan, data, root / "runs", "fixture", search_client=Search(), judge=judge, interval_seconds=0, progress=lambda _: None)
            self.assertEqual(judge.calls[1]["payload"]["role"]["job_description"], "Python experience. United States.")

    def test_plan_freezes_luna_medium_reviewers(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, plan = self.setup_run(Path(temporary))
            self.assertEqual((plan["plan"]["model"], plan["plan"]["reasoning_effort"]), ("gpt-6-luna", "medium"))

    def test_exa_plan_uses_its_public_origin_limit_and_modes(self):
        with tempfile.TemporaryDirectory() as temporary:
            data, _ = dataset(Path(temporary) / "dataset")
            plan = make_plan(data, provider="exa", mode="auto", as_of="2026-09-30")["plan"]
            self.assertEqual((plan["provider"], plan["api_base_url"], plan["limit"]), ("exa", "https://api.exa.ai", 100))
            self.assertEqual(plan["roles"][0]["search_body"]["category"], "people")
            with self.assertRaises(ValueError):
                make_plan(data, provider="exa", mode="fast-ish", as_of="2026-09-30")
            with self.assertRaises(ValueError):
                make_plan(data, provider="wrangle", mode="fast", as_of="2026-09-30")

    def test_report_shows_candidates_returned(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); data, plan = self.setup_run(root)
            result = run(plan, data, root / "runs", "fixture", search_client=Search(), judge=Judge(), interval_seconds=0, progress=lambda _: None)
            self.assertEqual((result["provider"], result["overall"]["avg_candidates_returned"]), ("wrangle", 1))

    def test_selected_scoring_is_default_and_obsolete_policy_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            data, _ = dataset(Path(temporary) / "dataset")
            kwargs = dict(base_url="https://example.test", mode="fast", as_of="2026-09-30")
            plan = make_plan(data, **kwargs)
            self.assertEqual(plan["plan"]["scoring"]["unknown_policy"], "zero")
            with self.assertRaises(ValueError):
                make_plan(data, unknown_policy="incomplete", **kwargs)

    def test_changed_dataset_rejects_existing_plan(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); data, plan = self.setup_run(root)
            path = data / "snapshots/test-role.json"
            saved = read_json(path); saved["job"]["title"] = "Different"
            atomic_json(path, saved)
            with self.assertRaisesRegex(ValueError, "snapshot changed"):
                validate_plan(plan, data)

    def test_judge_retry_does_not_resend_uncertain_search(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); data, plan = self.setup_run(root)
            run(plan, data, root / "runs", "fixture", search_client=Search(True), judge=Judge(), interval_seconds=0, progress=lambda _: None)
            search = Search()
            result = run(plan, data, root / "runs", "fixture", search_client=search, judge=Judge(), interval_seconds=0,
                         retry_uncertain=["judge"], progress=lambda _: None)
            self.assertEqual(search.calls, [])
            self.assertEqual(result["status"], "incomplete")
            run(plan, data, root / "runs", "fixture", search_client=search, judge=Judge(), interval_seconds=0,
                retry_uncertain=["search"], progress=lambda _: None)
            self.assertEqual(len(search.calls), 1)
            for invalid in (True, "search", ["searches"]):
                with self.assertRaises(ValueError):
                    run(plan, data, root / "runs", "fixture", search_client=search, judge=Judge(), retry_failed=invalid)

    def test_profiles_without_experience_stop_before_judging(self):
        class NoExperience(Search):
            def search(self, body, key):
                self.calls.append((body, key))
                return {**super().search(body, key), "candidates": prepare_candidates([{"id": "p", "workHistory": ["Built Python services"], "location": "United States"}])}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); data, plan = self.setup_run(root)
            judge = Judge()
            result = run(plan, data, root / "runs", "fixture", search_client=NoExperience(), judge=judge, interval_seconds=0, progress=lambda _: None)
            self.assertEqual(judge.calls, [])
            self.assertEqual(result["searches"][0]["search_status"], "profile_contract_rejected")
            self.assertEqual(result["overall"]["field_coverage_counts"]["experiences"], 0)
            self.assertEqual(result["overall"]["field_coverage_counts"]["location"], 1)

    def test_report_includes_field_coverage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); data, plan = self.setup_run(root)
            result = run(plan, data, root / "runs", "fixture", search_client=Search(), judge=Judge(), interval_seconds=0, progress=lambda _: None)
            self.assertEqual(result["overall"]["field_coverage_counts"], {"location": 1, "educations": 0, "experiences": 1, "listed_skills": 1})
            self.assertEqual(result["overall"]["returned_profiles"], 1)

    def test_invalid_identifier_cannot_write_outside_run_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); data, plan = self.setup_run(root)
            with self.assertRaises(ValueError):
                run(plan, data, root / "runs", "../../outside", search_client=Search(), judge=Judge())
