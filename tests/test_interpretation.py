from pathlib import Path
import re
import unittest

from benchmark.interpretation import YEAR_RANGE, apply_location_policy, broaden_location, dropped_location_options, qualify_city, unbound_experience
from benchmark.io import read_json, read_jsonl
from benchmark.models import load_roles
from benchmark.queries import load_rubric
from support import rubric


class InterpretationTests(unittest.TestCase):
    def test_experience_upper_bounds_removed_but_other_ranges_preserved(self):
        self.assertEqual(unbound_experience("3–5 years or 1 to 2 years post-MBA; travel 20–30%; $100–150K"),
                         "3+ years or 1+ years post-MBA; travel 20–30%; $100–150K")
        self.assertEqual(unbound_experience("between 2 and 4 years"), "2+ years")
        self.assertEqual(unbound_experience("6–8+ years"), "6+ years")

    def test_city_expansion_does_not_broaden_country_or_state_scope(self):
        self.assertEqual(broaden_location("Sunnyvale, CA"), "San Francisco Bay Area, California")
        self.assertEqual(broaden_location("Brooklyn, NY"), "New York metropolitan area")
        for label in ("Pennsylvania", "United States, excluding California", "Canada", "New York State", "US timezone: PT"):
            self.assertEqual(broaden_location(label), label)

    def test_ambiguous_city_names_need_a_qualifier(self):
        self.assertEqual(broaden_location("Norwalk, CA"), "Greater Los Angeles area, California")
        self.assertEqual(broaden_location("Norwalk, CT"), "Greater Norwalk, CT area")
        self.assertEqual(broaden_location("Cambridge, MA"), "Greater Boston area, Massachusetts")
        self.assertEqual(broaden_location("Cambridge, United Kingdom"), "Greater Cambridge, United Kingdom area")
        self.assertEqual(broaden_location("Vancouver, British Columbia, Canada"), "Greater Vancouver, British Columbia, Canada area")
        for label in ("Vancouver", "Portland", "Kent", "Cambridge office"):
            with self.subTest(label=label), self.assertRaisesRegex(ValueError, "Ambiguous"):
                broaden_location(label)

    def test_bare_ambiguous_city_is_qualified_only_by_unique_posting_address(self):
        bc = {"addressLocality": "Vancouver", "addressRegion": "British Columbia", "addressCountry": "Canada"}
        wa = {"addressLocality": "Vancouver", "addressRegion": "Washington", "addressCountry": "United States"}
        self.assertEqual(qualify_city("Vancouver", [bc]), "Vancouver, British Columbia, Canada")
        self.assertEqual(qualify_city("Vancouver", [bc, wa]), "Vancouver")
        self.assertEqual(qualify_city("Toronto", [bc]), "Toronto")

    def test_dropped_non_geographic_options_are_reported(self):
        location = rubric()["location"]
        location["allowed_locations"] = ["Boston, MA", "Remote (geographic scope unspecified)"]
        self.assertEqual(dropped_location_options(location), ["Remote (geographic scope unspecified)"])

    def test_remote_plus_locations_is_or_with_current_geography_required(self):
        location = rubric()["location"]
        location.update(allowed_locations=["Sunnyvale, CA", "Boston, MA", "Remote (geographic scope unspecified)"], current_residence_required=False)
        effective = apply_location_policy(location)
        self.assertEqual(len(effective["allowed_locations"]), 2)
        self.assertTrue(effective["current_residence_required"])
        self.assertIn("alternatives (OR)", effective["requirement_text"])
        self.assertIn("must currently", effective["requirement_text"])

    def test_remote_without_location_does_not_invent_a_country(self):
        location = rubric()["location"]
        location.update(allowed_locations=["Remote; geographic eligibility unspecified"], current_residence_required=False)
        effective = apply_location_policy(location)
        self.assertFalse(effective["required"])
        self.assertEqual(effective["allowed_locations"], [])

    def test_explicit_attendance_requirement_survives_area_expansion(self):
        location = rubric()["location"]
        location.update(workplace_type="onsite", allowed_locations=["South San Francisco, CA"], source_quotes=["onsite at the office 5 days per week"])
        effective = apply_location_policy(location)
        self.assertIn("5 days per week", effective["requirement_text"])

    def test_relocation_alternative_remains_in_query_but_does_not_waive_remote_geofence(self):
        location = rubric()["location"]
        location.update(workplace_type="onsite", allowed_locations=["San Francisco"], relocation="offered", current_residence_required=False)
        effective = apply_location_policy(location)
        self.assertIn("willingness to relocate", effective["requirement_text"])
        self.assertFalse(effective["current_residence_required"])
        location["workplace_type"] = "remote"
        effective = apply_location_policy(location)
        self.assertNotIn("willingness to relocate", effective["requirement_text"])
        self.assertTrue(effective["current_residence_required"])


class CorpusV1Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.derived = Path(__file__).resolve().parents[1] / "data/roles/ashby-2026-09-30-v1"
        cls.rows = load_roles(cls.derived)
        cls.records = {row["role_id"]: load_rubric(cls.derived, row) for row in cls.rows}

    def test_v1_policy_and_every_question_resolved(self):
        from benchmark.interpretation import POLICY
        self.assertEqual(POLICY["version"], "role-interpretation-v1")
        self.assertEqual(read_json(self.derived / "manifest.json")["interpretation_policy"], POLICY)
        self.assertEqual(len(self.rows), 290)
        for row in self.rows:
            self.assertTrue((self.derived / row["snapshot_path"]).exists())
            self.assertEqual(self.records[row["role_id"]]["rubric"]["ambiguities"], [])
        decisions = read_json(self.derived / "clarification-resolutions.json")
        self.assertEqual(decisions["role_count"], 32)
        self.assertTrue(all(row["status"] == "resolved_by_user_policy" for row in decisions["entries"]))

    def test_no_older_version_identifiers_remain(self):
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
            for item in interpretation["observability"]["removed_criteria"] + interpretation["observability"]["reduced_criteria"]:
                self.assertTrue(item["reason"])
            for item in interpretation["implied_evidence"]["demoted_to_preferences"] + interpretation["implied_evidence"]["reduced_criteria"]:
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
        vancouver = self.records["a6029d48-f96c-4328-9561-a1821b93aa07"]["rubric"]["location"]
        self.assertEqual(["Greater Toronto, Canada area"], vancouver["allowed_locations"])
        for rid in ("9fcedb9d-8cf3-498e-a22f-acbfa39ebbb1", "4a7be83c-5d32-46b7-9d30-cf6759922237", "9cfa5eb7-402b-488f-b8af-7fabbd602541", "b8f24465-3d29-446c-bac6-5ee49949a3b5", "32fc2a1c-775b-4118-94bb-0700dc034f01"):
            self.assertTrue(self.records[rid]["rubric"]["location"]["allowed_locations"])

    def test_resolved_interpretations(self):
        records = self.records
        self.assertFalse(any("8+" in item["requirement"] for item in records["8d54bd33-ce38-4d5d-854f-bf2fec207efb"]["rubric"]["must_haves"]))
        par = records["68825129-287f-4c65-b589-378a4b1f06b2"]["rubric"]["location"]
        self.assertNotIn("Pennsylvania", par["allowed_locations"])
        self.assertTrue(any("Philadelphia" in name for name in par["allowed_locations"]))
        render = records["88cb74a4-bc28-40b1-b792-d3041e3e17d3"]["rubric"]
        self.assertIn("Experience planning and executing high-risk changes with minimal downtime.", [item["requirement"] for item in render["preferences"]])


if __name__ == "__main__":
    unittest.main()
