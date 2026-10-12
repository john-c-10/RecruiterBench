"""Load frozen role records and validate evidence-grounded rubrics."""
from __future__ import annotations

from pathlib import Path
import jsonschema

from .io import digest, grounded, identifier, read_json, read_jsonl

ROOT = Path(__file__).resolve().parent
# Rubric extraction.
MODEL = "gpt-6.1-sol"
REASONING_EFFORT = "low"
# Both candidate reviewers.
JUDGE_MODEL = "gpt-6-luna"
JUDGE_REASONING_EFFORT = "medium"
VERSION = "1.0"


def schema(name: str) -> dict:
    return read_json(ROOT / "schemas" / f"{identifier(name)}.json")


def prompt(name: str) -> str:
    return (ROOT / "prompts" / f"{identifier(name)}.txt").read_text()


def load_roles(dataset: Path) -> list[dict]:
    manifest = read_json(dataset / "manifest.json")
    roles = read_jsonl(dataset / "roles.jsonl")
    if digest(roles) != manifest["selection_sha256"] or len(roles) != manifest["role_count"]:
        raise ValueError("Frozen role index changed")
    ids = [identifier(role["role_id"]) for role in roles]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate role IDs")
    for role in roles:
        if role["snapshot_path"] != f"snapshots/{role['role_id']}.json" or role["rubric_path"] != f"rubrics/{role['role_id']}.json":
            raise ValueError("Unexpected dataset artifact path")
        snapshot = read_json(dataset / role["snapshot_path"])
        if snapshot["role_id"] != role["role_id"] or digest(snapshot) != role["snapshot_sha256"]:
            raise ValueError("Frozen job snapshot changed")
    return roles


def role_source(snapshot: dict) -> dict:
    job = snapshot["job"]
    return {key: job.get(key) for key in ["title", "location", "address", "secondaryLocations", "workplaceType", "employmentType", "department", "team", "descriptionPlain"]}


def validate_rubric(value: dict, snapshot: dict) -> dict:
    jsonschema.validate(value, schema("rubric"))
    source = role_source(snapshot)
    seen = set()
    for key, prefix in [("must_haves", "M"), ("red_flags", "D"), ("preferences", "P")]:
        for index, criterion in enumerate(value[key], 1):
            if criterion["id"] != f"{prefix}{index}" or criterion["id"] in seen:
                raise ValueError("Rubric criterion IDs are not unique and sequential")
            seen.add(criterion["id"])
            if not grounded(criterion["source_quote"], source):
                raise ValueError("Rubric quotation is not present in the job source")
    location = value["location"]
    if location["required"] and not location["source_quotes"]:
        raise ValueError("Required location needs source evidence")
    if any(not grounded(quote, source) for quote in location["source_quotes"]):
        raise ValueError("Location quotation is not present in the job source")
    return value
