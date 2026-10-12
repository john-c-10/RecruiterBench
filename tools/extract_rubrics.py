"""Run source-grounded role extraction; no candidate searches are performed."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmark.rubrics import extract


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--private-root", type=Path, default=Path("data/private"))
    parser.add_argument("--limit", type=int)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--role-ids", nargs="+")
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--retry-uncertain", action="store_true")
    args = parser.parse_args()
    report = extract(args.dataset, args.private_root, limit=args.limit, workers=args.workers,
                     retry_failed=args.retry_failed, retry_uncertain=args.retry_uncertain,
                     role_ids=set(args.role_ids) if args.role_ids else None)
    raise SystemExit(0 if report["completed"] == report["selected"] else 1)


if __name__ == "__main__":
    main()
