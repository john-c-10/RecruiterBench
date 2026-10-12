"""Freeze the named benchmark suites' role selections. Offline; never overwrites a suite."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmark.io import atomic_json
from benchmark.suites import DATASET, DEFINITIONS, build_suite, load_suite, suite_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DATASET)
    args = parser.parse_args()
    names = [name for name, (_, _, corpus) in DEFINITIONS.items() if corpus == args.dataset.name]
    for name in names:
        path = suite_path(name, args.dataset)
        if path.exists():
            raise SystemExit(f"Refusing to overwrite frozen suite {path}; define a new suite version instead")
    for name in names:
        atomic_json(suite_path(name, args.dataset), build_suite(name, args.dataset))
        suite = load_suite(name, args.dataset)
        print(json.dumps({"benchmark": name, "roles": suite["role_count"], "categories": suite["category_count"]}))


if __name__ == "__main__":
    main()
