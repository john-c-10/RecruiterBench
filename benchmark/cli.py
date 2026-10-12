"""Explicit, reviewable commands. Only extract-rubrics and run use a network."""
import argparse
from datetime import date
import json
from pathlib import Path
import sys

from .dataset import joined_roles, validate_dataset
from .io import atomic_json, atomic_jsonl, identifier, read_json
from .providers import SEARCH_PROVIDERS
from .rubrics import extract
from .runner import RETRY_STAGES, build_report, make_plan, rescore, run, validate_plan
from .scoring import TOP1_DEFINITION, UNKNOWN_POLICY
from .suites import DEFINITIONS

DEFAULT_DATASET = Path("data/roles/ashby-2026-09-30-v1")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Frozen Ashby-role recruiting benchmark. No network calls in validate, export-roles, plan, or report.")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "export-roles", "plan", "run", "rescore", "report", "extract-rubrics"):
        command = commands.add_parser(name)
        command.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
        if name in ("validate", "export-roles", "plan", "report"):
            command.add_argument("--out", type=Path, required=name in ("export-roles", "plan"))
        if name in ("plan", "extract-rubrics"):
            command.add_argument("--limit", type=int)
            command.add_argument("--role-id", action="append")
        if name == "plan":
            command.add_argument("--benchmark", choices=tuple(DEFINITIONS),
                                 help="Plan a named, frozen benchmark suite instead of --limit/--role-id")
            command.add_argument("--provider", default="wrangle", choices=tuple(SEARCH_PROVIDERS))
            command.add_argument("--api-base-url", help="Explicit HTTPS origin; required for Wrangle, defaults to the provider's public API otherwise")
            command.add_argument("--mode", required=True,
                                 help="Provider search mode: " + "; ".join(f"{name}: {', '.join(module.MODES)}" for name, module in SEARCH_PROVIDERS.items()))
            command.add_argument("--repeats", type=int, default=1)
            command.add_argument("--as-of", default=date.today().isoformat())
            command.add_argument("--unknown-policy", default=UNKNOWN_POLICY, choices=(UNKNOWN_POLICY,), help="Unknown hard constraints fail the gate and receive zero credit")
            command.add_argument("--top1-definition", default=TOP1_DEFINITION, choices=(TOP1_DEFINITION,), help="Top-1 relevance: the first result's credit (0, 0.5, or 1); precision at rank 1, not recall")
        if name in ("run", "rescore", "report"):
            command.add_argument("--runs-root", type=Path, default=Path("data/runs"))
            command.add_argument("--run-id", required=True)
        if name in ("run", "rescore"):
            command.add_argument("--plan", type=Path, required=True)
            command.add_argument("--parallel", type=int, default=1, help="Roles searched and judged concurrently")
        if name == "run":
            command.add_argument("--search-interval-seconds", type=float, default=300 / 45)
        if name == "rescore":
            command.add_argument("--source-run", required=True, help="Run whose saved searches are judged again; no searches are sent unless --search-missing")
            command.add_argument("--search-missing", action="store_true", help="Search live for planned roles the source run has no completed search for")
        if name in ("run", "rescore", "extract-rubrics"):
            command.add_argument("--workers", type=int, default=8)
        if name in ("run", "rescore"):
            command.add_argument("--retry-failed", action="append", default=[], choices=RETRY_STAGES,
                                 help="Repeat failed units of this stage (search or judge); repeatable")
            command.add_argument("--retry-uncertain", action="append", default=[], choices=RETRY_STAGES,
                                 help="Explicitly repeat uncertain paid requests of this stage after inspecting their private state; repeatable")
        if name == "extract-rubrics":
            command.add_argument("--retry-failed", action="store_true")
            command.add_argument("--retry-uncertain", action="store_true", help="Explicitly repeat uncertain paid requests after inspecting their private state")
        if name == "extract-rubrics":
            command.add_argument("--private-root", type=Path, default=Path("data/private"))
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            result = validate_dataset(args.dataset)
            if args.out:
                atomic_json(args.out, result)
            print(json.dumps({key: value for key, value in result.items() if key not in ("rubric_hashes", "roles_with_ambiguities", "confirmation_requirements")}, indent=2))
        elif args.command == "export-roles":
            rows = joined_roles(args.dataset)
            atomic_jsonl(args.out, rows)
            print(json.dumps({"roles": len(rows), "output": str(args.out)}))
        elif args.command == "plan":
            result = make_plan(args.dataset, base_url=args.api_base_url, mode=args.mode, provider=args.provider, unknown_policy=args.unknown_policy,
                               top1_definition=args.top1_definition, as_of=args.as_of, repeats=args.repeats,
                               limit=args.limit, role_ids=args.role_id, benchmark=args.benchmark)
            if args.out.exists() and read_json(args.out) != result:
                raise ValueError("Refusing to overwrite a different plan; choose a new output path")
            atomic_json(args.out, result)
            print(json.dumps({"plan": str(args.out), "plan_sha256": result["plan_sha256"], "benchmark": args.benchmark,
                              **result["plan"]["workload"]}, indent=2))
        elif args.command == "extract-rubrics":
            result = extract(args.dataset, args.private_root, limit=args.limit, workers=args.workers,
                             role_ids=set(args.role_id) if args.role_id else None,
                             retry_failed=args.retry_failed, retry_uncertain=args.retry_uncertain)
            return int(bool(result["failed"]))
        elif args.command == "run":
            result = run(read_json(args.plan), args.dataset, args.runs_root, args.run_id, workers=args.workers, parallel=args.parallel,
                         interval_seconds=args.search_interval_seconds, retry_failed=args.retry_failed, retry_uncertain=args.retry_uncertain)
            print(json.dumps({"status": result["status"], "overall": result["overall"]}, indent=2))
            return int(result["status"] != "complete")
        elif args.command == "rescore":
            result = rescore(read_json(args.plan), args.dataset, args.runs_root, args.run_id, args.source_run, search_missing=args.search_missing, workers=args.workers,
                             parallel=args.parallel, retry_failed=args.retry_failed, retry_uncertain=args.retry_uncertain)
            print(json.dumps({"status": result["status"], "overall": result["overall"]}, indent=2))
            return int(result["status"] != "complete")
        elif args.command == "report":
            root = args.runs_root / identifier(args.run_id)
            envelope = read_json(root / "plan.json")
            plan = validate_plan(envelope, args.dataset)
            result = build_report(root, plan, envelope["plan_sha256"])
            if args.out:
                atomic_json(args.out, result)
            print(json.dumps({"status": result["status"], "overall": result["overall"]}, indent=2))
            return int(result["status"] != "complete")
        return 0
    except (ValueError, RuntimeError, KeyError, OSError) as error:
        # Avoid dumping upstream bodies, profile content, or credential values.
        print(f"Benchmark stopped: {type(error).__name__}. {str(error) if isinstance(error, (ValueError, RuntimeError)) else 'Inspect local artifact paths and schemas.'}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
