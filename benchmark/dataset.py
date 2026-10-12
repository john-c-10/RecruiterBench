"""Offline dataset validation and joined, portable role exports."""
from collections import Counter
from pathlib import Path

from .io import digest, read_json, read_jsonl
from .models import load_roles
from .queries import build_query, load_rubric


def joined_roles(dataset: Path) -> list[dict]:
    output = []
    for role in load_roles(dataset):
        record = load_rubric(dataset, role)
        snapshot = read_json(dataset / role["snapshot_path"])
        output.append({**role, "source": snapshot["source"], "description_plain": snapshot["job"]["descriptionPlain"],
                       "rubric": record["rubric"], "rubric_sha256": record["rubric_sha256"],
                       "extraction": record["extraction"], "curation": record.get("curation"),
                       **({"interpretation": record["interpretation"], "experience_context": record.get("experience_context", [])} if "interpretation" in record else {}),
                       "benchmark_query": build_query(record["rubric"])})
    return output


def validate_dataset(dataset: Path) -> dict:
    rows = joined_roles(dataset)
    exported = dataset / "roles-with-rubrics.jsonl"
    if exported.exists() and digest(read_jsonl(exported)) != digest(rows):
        raise ValueError("Joined role export is stale or changed")
    counts = Counter(row["category"] for row in rows)
    manifest = read_json(dataset / "manifest.json")
    if counts != {row["name"]: row["count"] for row in manifest["categories"]} or len(counts) != manifest["category_count"]:
        raise ValueError("Category counts do not match manifest")
    rubrics_manifest = dataset / "rubrics-manifest.json"
    hashes = {row["role_id"]: row["rubric_sha256"] for row in rows}
    if rubrics_manifest.exists() and read_json(rubrics_manifest)["rubric_hashes"] != hashes:
        raise ValueError("Frozen rubric corpus changed")
    return {"dataset_id": dataset.name, "roles": len(rows), "categories": len(counts),
            "must_haves": sum(len(row["rubric"]["must_haves"]) for row in rows),
            "red_flags": sum(len(row["rubric"]["red_flags"]) for row in rows),
            "required_locations": sum(row["rubric"]["location"]["required"] for row in rows),
            "joined_roles_sha256": digest(rows), "rubric_hashes": hashes,
            "roles_with_ambiguities": [{"role_id": row["role_id"], "title": row["title"], "company": row["company"],
                                         "ambiguities": row["rubric"]["ambiguities"]} for row in rows if row["rubric"]["ambiguities"]],
            "confirmation_requirements": [{"role_id": row["role_id"], "criterion_id": item["id"], "requirement": item["requirement"]}
                                          for row in rows for field in ("must_haves", "red_flags") for item in row["rubric"][field]
                                          if item["verification"] == "candidate_confirmation"],
            "review_status": ("User-directed interpretation applied; source quotes, IDs and hashes validated. Original sources preserved; not a human-labeled gold standard."
                              if manifest.get("interpretation_policy") else "Machine-extracted; schema, source quotes, IDs and hashes validated. Ambiguities retained for review; not a human-labeled gold standard.")}
