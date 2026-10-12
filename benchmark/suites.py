"""Named, frozen benchmark suites: fixed role selections from one corpus version."""
from pathlib import Path

from .io import digest, identifier, read_json
from .models import load_roles
from .queries import select_roles

DATASET = Path("data/roles/ashby-2026-09-30-v1")
LITE = "One role per category: the first role of each category in the frozen role index."
HARD = "Every role in the corpus."
# name -> (description, roles per category or None for all, corpus it is frozen on)
DEFINITIONS = {
    "recruiterbench-lite-v1": (LITE, 1, "ashby-2026-09-30-v1"),
    "recruiterbench-hard-v1": (HARD, None, "ashby-2026-09-30-v1"),
}


def build_suite(name: str, dataset: Path = DATASET) -> dict:
    description, per_category, _ = DEFINITIONS[name]
    roles = load_roles(dataset)
    categories = len({role["category"] for role in roles})
    selected = select_roles(roles, limit=categories * per_category if per_category else None)
    return {"schema_version": "1.0", "name": name, "description": description,
            "dataset_id": dataset.name, "dataset_manifest_sha256": digest(read_json(dataset / "manifest.json")),
            "role_count": len(selected), "category_count": len({role["category"] for role in selected}),
            "role_ids": [role["role_id"] for role in selected]}


def suite_path(name: str, dataset: Path) -> Path:
    # data/roles/<dataset>/ -> data/benchmarks/<name>.json
    return dataset.parent.parent / "benchmarks" / f"{identifier(name)}.json"


def load_suite(name: str, dataset: Path) -> dict:
    suite = read_json(suite_path(name, dataset))
    if suite["name"] != name or suite["dataset_id"] != dataset.name:
        raise ValueError(f"Benchmark {name} is defined on dataset {suite['dataset_id']}; pass --dataset data/roles/{suite['dataset_id']}")
    if suite["dataset_manifest_sha256"] != digest(read_json(dataset / "manifest.json")):
        raise ValueError(f"Dataset changed since benchmark {name} was frozen")
    known = {role["role_id"] for role in load_roles(dataset)}
    ids = suite["role_ids"]
    if len(ids) != suite["role_count"] or len(set(ids)) != len(ids) or not set(ids) <= known:
        raise ValueError(f"Benchmark {name} role selection is invalid")
    return suite
