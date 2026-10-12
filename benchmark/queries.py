"""Frozen search requests use exactly the rubric's professional conditions."""
from collections import OrderedDict
from pathlib import Path

from .io import digest, read_json
from .models import validate_rubric


def load_rubric(dataset: Path, role: dict) -> dict:
    record = read_json(dataset / role["rubric_path"])
    if record["role_id"] != role["role_id"] or record["snapshot_sha256"] != role["snapshot_sha256"] or record["rubric_sha256"] != digest(record["rubric"]):
        raise ValueError("Rubric lineage or content changed")
    corpus = dataset / "rubrics-manifest.json"
    if corpus.exists() and read_json(corpus)["rubric_hashes"].get(role["role_id"]) != record["rubric_sha256"]:
        raise ValueError("Frozen rubric corpus changed; use a new dataset version")
    validate_rubric(record["rubric"], read_json(dataset / role["snapshot_path"]))
    return record


def build_query(rubric: dict) -> str:
    # Deterministic rendering avoids dropping a condition in an LLM paraphrase.
    lines = ["Find candidates for this role:", rubric["role_summary"]]
    for field, label in [("must_haves", "Mandatory professional qualifications"), ("red_flags", "Explicit disqualifiers"), ("preferences", "Preferred, not mandatory")]:
        if rubric[field]:
            lines.append(label + ":")
            lines.extend("- " + item["requirement"] for item in rubric[field])
    # One location rule, stated even when it is "no filter", so the brief never
    # leaves the search to guess a geography the evaluator does not apply.
    lines.extend(["Work location and geographic eligibility:", rubric["location"]["requirement_text"]])
    query = "\n".join(lines)
    if len(query) > 20000:
        raise ValueError("Frozen search query exceeds the public API limit; review the rubric")
    return query


def select_roles(roles: list[dict], *, role_ids: list[str] | None = None, limit: int | None = None) -> list[dict]:
    if role_ids:
        if len(role_ids) != len(set(role_ids)) or set(role_ids) - {role["role_id"] for role in roles}:
            raise ValueError("Duplicate or unknown role selection")
        roles = [role for role in roles if role["role_id"] in role_ids]
    groups = OrderedDict()
    for role in roles:
        groups.setdefault(role["category"], []).append(role)
    ordered = [group[index] for index in range(max(map(len, groups.values()), default=0)) for group in groups.values() if index < len(group)]
    if limit is not None and (not isinstance(limit, int) or limit < 1):
        raise ValueError("Role limit must be positive")
    return ordered[:limit] if limit else ordered
