from pathlib import Path
from benchmark.io import atomic_json, atomic_jsonl, digest
from benchmark.interpretation import POLICY


def rubric():
    return {"role_summary": "Software engineer working on Python systems.", "search_query": "Python software engineer",
            "must_haves": [{"id": "M1", "requirement": "Python experience", "source_quote": "Python experience", "verification": "profile"}],
            "red_flags": [], "preferences": [], "location": {"required": True, "requirement_text": "United States",
            "workplace_type": "remote", "allowed_locations": ["United States"], "remote_scope": "United States",
            "relocation": "unspecified", "current_residence_required": True, "source_quotes": ["United States"]},
            "soft_requirements": [], "ambiguities": []}


def dataset(root: Path):
    root.mkdir(parents=True)
    snapshot = {"role_id": "test-role", "source": {"verified_at": "2026-09-30T00:00:00Z"},
                "job": {"title": "Engineer", "descriptionPlain": "Python experience. United States."}}
    role = {"role_id": "test-role", "category": "Software", "company": "Example", "title": "Engineer",
            "job_url": "https://jobs.ashbyhq.com/example/test-role", "snapshot_sha256": digest(snapshot),
            "snapshot_path": "snapshots/test-role.json", "rubric_path": "rubrics/test-role.json"}
    atomic_json(root / role["snapshot_path"], snapshot)
    atomic_jsonl(root / "roles.jsonl", [role])
    atomic_json(root / "manifest.json", {"role_count": 1, "category_count": 1, "categories": [{"name": "Software", "count": 1}], "selection_sha256": digest([role]), "interpretation_policy": POLICY})
    atomic_json(root / role["rubric_path"], {"role_id": role["role_id"], "snapshot_sha256": role["snapshot_sha256"],
                                           "rubric_sha256": digest(rubric()), "rubric": rubric(), "extraction": {}})
    return root, role


def gate(verdict="pass"):
    return {"verdict": verdict, "failed_criteria": ["M1"] if verdict == "fail" else [],
            "implied_criteria": ["M1"] if verdict == "partial" else [], "description": "Fixture"}


class Judge:
    def __init__(self, verdict="pass", experience="pass", overqualified=False, underqualified=False):
        self.calls, self.verdict, self.experience = [], verdict, experience
        self.overqualified, self.underqualified = overqualified, underqualified

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        value = gate(self.verdict) if kwargs["name"].startswith("gate") else {
            "verdict": self.experience, "underqualified": self.underqualified, "obviously_overqualified": self.overqualified, "description": "Experience adequacy fixture"}
        return {"value": value, "model": "gpt-6.1-sol", "elapsed_ms": 1, "usage": {}}
