from pathlib import Path
import tempfile
import unittest

from benchmark.io import atomic_json, read_json
from benchmark.models import load_roles
from benchmark.runner import make_plan, run, validate_plan
from benchmark.suites import build_suite, load_suite, suite_path
from support import Judge, dataset
from test_runner import Search

DATA = Path(__file__).resolve().parents[1] / "data/roles/ashby-2026-09-30-v1"


class FrozenSuiteTests(unittest.TestCase):
    def test_lite_is_one_role_per_category_and_hard_is_every_role(self):
        roles = {role["role_id"]: role for role in load_roles(DATA)}
        lite = load_suite("recruiterbench-lite-v1", DATA)
        hard = load_suite("recruiterbench-hard-v1", DATA)
        self.assertEqual(sorted(roles[rid]["category"] for rid in lite["role_ids"]), sorted({role["category"] for role in roles.values()}))
        self.assertEqual((lite["role_count"], lite["category_count"]), (58, 58))
        self.assertEqual(sorted(hard["role_ids"]), sorted(roles))
        self.assertTrue(set(lite["role_ids"]) <= set(hard["role_ids"]))

    def test_frozen_files_match_their_definitions(self):
        for name in ("recruiterbench-lite-v1", "recruiterbench-hard-v1"):
            self.assertEqual(read_json(suite_path(name, DATA)), build_suite(name, DATA))


class SuitePlanTests(unittest.TestCase):
    def setup(self, root):
        data, _ = dataset(root / "roles" / "dataset")
        atomic_json(suite_path("recruiterbench-lite-v1", data), build_suite("recruiterbench-lite-v1", data))
        return data

    def test_benchmark_plan_records_suite_and_survives_validation_and_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); data = self.setup(root)
            plan = make_plan(data, base_url="https://example.test", mode="fast", as_of="2026-09-30", benchmark="recruiterbench-lite-v1")
            self.assertEqual(plan["plan"]["benchmark"]["name"], "recruiterbench-lite-v1")
            self.assertEqual([role["role_id"] for role in plan["plan"]["roles"]], ["test-role"])
            validate_plan(plan, data)
            result = run(plan, data, root / "runs", "lite", search_client=Search(), judge=Judge(), interval_seconds=0, progress=lambda _: None)
            self.assertEqual(result["benchmark"]["name"], "recruiterbench-lite-v1")
            self.assertEqual(result["status"], "complete")

    def test_benchmark_cannot_be_combined_with_manual_selection_or_changed_suite(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); data = self.setup(root)
            kwargs = dict(base_url="https://example.test", mode="fast", as_of="2026-09-30", benchmark="recruiterbench-lite-v1")
            with self.assertRaises(ValueError):
                make_plan(data, limit=1, **kwargs)
            plan = make_plan(data, **kwargs)
            path = suite_path("recruiterbench-lite-v1", data)
            suite = read_json(path); suite["description"] = "edited"; atomic_json(path, suite)
            with self.assertRaisesRegex(ValueError, "changed"):
                validate_plan(plan, data)
            suite["dataset_manifest_sha256"] = "0" * 64; atomic_json(path, suite)
            with self.assertRaisesRegex(ValueError, "Dataset changed"):
                make_plan(data, **kwargs)


if __name__ == "__main__":
    unittest.main()
