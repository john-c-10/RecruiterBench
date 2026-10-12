from pathlib import Path
import re
import unittest

from benchmark.interpretation import YEAR_RANGE, unbound_experience
from benchmark.io import read_json
from benchmark.models import load_roles
from benchmark.queries import build_query, load_rubric


class InterpretationTests(unittest.TestCase):
    def test_experience_upper_bounds_removed_but_other_ranges_preserved(self):
        self.assertEqual(unbound_experience("3–5 years or 1 to 2 years post-MBA; travel 20–30%; $100–150K"),
                         "3+ years or 1+ years post-MBA; travel 20–30%; $100–150K")
        self.assertEqual(unbound_experience("between 2 and 4 years"), "2+ years")
        self.assertEqual(unbound_experience("6–8+ years"), "6+ years")


class CorpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.derived = Path(__file__).resolve().parents[1] / "data/roles/ashby-2026-09-30-v1"
        cls.rows = load_roles(cls.derived)
        cls.records = {row["role_id"]: load_rubric(cls.derived, row) for row in cls.rows}

    def test_policy_and_frozen_rubrics(self):
        from benchmark.interpretation import POLICY
        self.assertEqual(read_json(self.derived / "manifest.json")["interpretation_policy"], POLICY)
        self.assertEqual(len(self.rows), 290)
        for row in self.rows:
            rubric = self.records[row["role_id"]]["rubric"]
            self.assertTrue((self.derived / row["snapshot_path"]).exists())
            self.assertEqual(rubric["ambiguities"], [])
            self.assertEqual(rubric["search_query"], build_query(rubric))

    def test_identifiers_are_consistent(self):
        text = "".join(path.read_text() for path in self.derived.rglob("*.json*") if path.parent.name != "snapshots")
        self.assertIsNone(re.search(r"ashby-2026-09-30(?!-v1)|role-interpretation-v(?!1\b)\d|experience-fit-v(?!1\b)\d", text))

    def test_hard_criteria_are_observable_and_ids_stay_consistent(self):
        unobservable = re.compile(r"\b(?:english|german|french|spanish|swedish|russian|ukrainian|dutch)\b|\ba portfolio\b|designation|board certification", re.I)
        for rid, record in self.records.items():
            for field in ("must_haves", "red_flags"):
                for item in record["rubric"][field]:
                    self.assertEqual(item["verification"], "profile", rid)
                    self.assertIsNone(unobservable.search(item["requirement"]), (rid, item["requirement"]))
            ids = {item["id"] for item in record["rubric"]["must_haves"]}
            for row in record["experience_context"]:
                if row["kind"] == "mandatory":
                    self.assertIn(row["criterion_id"], ids)
            for field in ("must_haves", "preferences"):
                for item in record["rubric"][field]:
                    self.assertIsNone(YEAR_RANGE.search(item["requirement"]))

    def test_changes_keep_their_reasons(self):
        for record in self.records.values():
            interpretation = record["interpretation"]
            self.assertTrue(interpretation["location"]["reason"])
            for key in ("removed_criteria", "reduced_criteria", "demoted_to_preferences"):
                for item in interpretation[key]:
                    self.assertTrue(item["reason"])

    def test_location_rules(self):
        from benchmark.interpretation import METRO_SENTENCE
        required = [r["rubric"]["location"] for r in self.records.values() if r["rubric"]["location"]["required"]]
        self.assertEqual(len(required), 277)
        self.assertTrue(all(METRO_SENTENCE in location["requirement_text"] for location in required))
        for record in self.records.values():
            location = record["rubric"]["location"]
            # One rule per role: several places only as parts of one remote region, never an office plus remote.
            self.assertTrue(len(location["allowed_locations"]) <= 1 or location["workplace_type"] == "remote", record["role_id"])
            self.assertNotIn("willingness to relocate", location["requirement_text"])
            self.assertNotIn("(OR)", location["requirement_text"])
            self.assertEqual(location["current_residence_required"], location["required"])
        agentis = self.records["a6029d48-f96c-4328-9561-a1821b93aa07"]["rubric"]["location"]
        self.assertEqual(["Greater Toronto, Canada area"], agentis["allowed_locations"])
        for rid in ("9fcedb9d-8cf3-498e-a22f-acbfa39ebbb1", "4a7be83c-5d32-46b7-9d30-cf6759922237", "9cfa5eb7-402b-488f-b8af-7fabbd602541", "b8f24465-3d29-446c-bac6-5ee49949a3b5", "32fc2a1c-775b-4118-94bb-0700dc034f01"):
            self.assertTrue(self.records[rid]["rubric"]["location"]["allowed_locations"])

    def test_specific_interpretations(self):
        records = self.records
        self.assertFalse(any("8+" in item["requirement"] for item in records["8d54bd33-ce38-4d5d-854f-bf2fec207efb"]["rubric"]["must_haves"]))
        par = records["68825129-287f-4c65-b589-378a4b1f06b2"]["rubric"]["location"]
        self.assertNotIn("Pennsylvania", par["allowed_locations"])
        self.assertTrue(any("Philadelphia" in name for name in par["allowed_locations"]))
        render = records["88cb74a4-bc28-40b1-b792-d3041e3e17d3"]["rubric"]
        self.assertIn("Experience planning and executing high-risk changes with minimal downtime.", [item["requirement"] for item in render["preferences"]])


if __name__ == "__main__":
    unittest.main()
