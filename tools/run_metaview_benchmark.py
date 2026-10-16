"""Run Hard once, then derive Lite exactly from its frozen subset, without rejudging."""
import argparse
import json
import os
from pathlib import Path
import shlex
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from benchmark.evaluators import Evaluator
from benchmark.io import atomic_json, digest, identifier, lock, now, read_json
from benchmark.metrics import summarize
from benchmark.models import JUDGE_MODEL, JUDGE_REASONING_EFFORT
from benchmark.providers.metaview import MetaviewSearch
from benchmark.providers.openai import OpenAIJudge, ProviderError
from benchmark.runner import build_report, field_coverage_counts, make_plan, run, validate_plan
from benchmark.suites import DATASET, load_suite


class StartupPollingSearch(MetaviewSearch):
    """Retry reads of a known job during Metaview's brief pre-start idle state.

    This operational retry wrapper leaves the frozen request, normalization,
    scoring engine, and original checkpoint untouched. It never retries create.
    """
    def search(self, body, idempotency_key):
        while True:
            try:
                return super().search(body, idempotency_key)
            except ProviderError as error:
                if error.code != "METAVIEW_EMPTY_OR_AWAITING_INPUT_INSPECT_SEARCH":
                    raise
                path = self.state_root / f"{digest(idempotency_key)}.json"
                record = read_json(path)
                if (not record.get("search_id") or record.get("body_sha256") != digest(body)
                        or self.wall_clock() - record["started_epoch"] >= 120):
                    raise
                self.sleep(self.poll_seconds)


def validate_startup_recovery(root, plan_hash):
    """Allow only failed startup reads backed by a matching known remote job."""
    recovering = []
    for path in (root / "units" / "search").glob("*.json"):
        unit = read_json(path)
        if unit["status"] == "complete":
            continue
        checkpoint = root / "provider-state" / f"{digest(unit['key'])}.json"
        record = read_json(checkpoint) if checkpoint.exists() else {}
        if (unit["status"] != "uncertain"
                or unit.get("error", {}).get("code") != "METAVIEW_EMPTY_OR_AWAITING_INPUT_INSPECT_SEARCH"
                or unit["inputs"]["plan_sha256"] != plan_hash
                or not record.get("search_id")
                or record.get("body_sha256") != digest(unit["inputs"]["body"])):
            raise ValueError("Unsafe startup recovery; inspect the non-completed search unit")
        recovering.append({"unit_key": unit["key"], "search_id": record["search_id"]})
    return recovering


def load_keys():
    # Parse only the two intended keys, without executing shell code or logging values.
    for line in Path(".env").read_text().splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        if key.strip() in ("METAVIEW_API_KEY", "OPENAI_API_KEY"):
            parts = shlex.split(value, comments=True)
            if parts:
                os.environ[key.strip()] = parts[0]
    if not all(os.environ.get(key) for key in ("METAVIEW_API_KEY", "OPENAI_API_KEY")):
        raise ValueError("Set METAVIEW_API_KEY and OPENAI_API_KEY in .env")


def derive_lite(hard: dict, dataset: Path) -> dict:
    suite = load_suite("recruiterbench-lite-v1", dataset)
    selected = set(suite["role_ids"])
    rows = [row for row in hard["searches"] if row["role_id"] in selected]
    return {key: value for key, value in hard.items() if key not in (
        "benchmark", "status", "expected_searches", "overall", "by_role", "by_category", "searches")} | {
        "benchmark": {"name": suite["name"], "suite_sha256": digest(suite)}, "generated_at": now(),
        "derivation": {"source_benchmark": hard["benchmark"], "source_plan_sha256": hard["plan_sha256"],
                       "method": "exact frozen Lite subset of Hard; original searches and judgments reused"},
        **summarize(rows, suite["role_count"])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", default="hard-v1-metaview-source-20261015")
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--report-only", action="store_true")
    parser.add_argument("--resume-startup-polls", action="store_true",
                        help="Resume only inspected empty-startup failures that have saved remote search IDs")
    args = parser.parse_args()
    root = Path("data/runs") / identifier(args.run_id)
    plan_path = root / "plan.json"
    envelope = (read_json(plan_path) if plan_path.exists() else
                make_plan(DATASET, provider="metaview", mode="source", benchmark="recruiterbench-hard-v1", as_of="2026-09-30"))
    plan = validate_plan(envelope, DATASET)
    if not args.report_only:
        load_keys()
        searcher = StartupPollingSearch(state_root=root / "provider-state", start_interval_seconds=20)
        judge = OpenAIJudge(model=JUDGE_MODEL, reasoning_effort=JUDGE_REASONING_EFFORT)
        with lock(root):
            if args.resume_startup_polls:
                recovered = validate_startup_recovery(root, envelope["plan_sha256"])
                atomic_json(root / "startup-recovery.json", {"created_at": now(), "plan_sha256": envelope["plan_sha256"],
                    "searches": recovered, "launcher_sha256": digest(Path(__file__).read_text()),
                    "note": "Resume reads of known jobs after premature idle responses; no search creations replayed. "
                            "New jobs tolerate empty startup responses for up to 120 seconds."})
            if not plan_path.exists():
                atomic_json(plan_path, envelope)
            role = plan["roles"][0]
            print(json.dumps({"event": "preflight_started", "role_id": role["role_id"], "title": role["title"], "time": now()}), flush=True)
            result = searcher.search(role["search_body"], f"{role['role_id']}__1")
            coverage = field_coverage_counts(result["candidates"])
            evaluator = Evaluator(judge, root, plan["as_of"])
            evaluated = evaluator.evaluate(role, role["rubric"], result["candidates"][0])
            record = {"event": "preflight_passed", "search_id": result["search_id"], "returned_count": result["returned_count"],
                      "field_coverage_counts": coverage, "first_judgment_status": evaluated["status"],
                      "searches_per_minute": 3, "time": now()}
            atomic_json(root / "preflight.json", record)
            print(json.dumps(record), flush=True)
        if args.preflight_only:
            return 0
        report = run(envelope, DATASET, Path("data/runs"), args.run_id, search_client=searcher, judge=judge,
                     parallel=16, judge_parallel=4, workers=8, interval_seconds=20,
                     retry_uncertain=("search",) if args.resume_startup_polls else (),
                     progress=lambda message: print(now(), message, flush=True))
    else:
        report = build_report(root, plan, envelope["plan_sha256"])
    lite = derive_lite(report, DATASET)
    atomic_json(root / "summary-hard.json", report)
    atomic_json(root / "summary-lite.json", lite)
    print(json.dumps({"hard": {"status": report["status"], "overall": report["overall"]},
                      "lite": {"status": lite["status"], "overall": lite["overall"]}}), flush=True)
    return int(report["status"] != "complete")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        # ProviderError strings contain only safe codes, never provider bodies.
        from benchmark.providers.openai import ProviderError
        print(json.dumps({"event": "stopped", "error_type": type(error).__name__,
                          "code": error.code if isinstance(error, ProviderError) else "LOCAL_ERROR"}), flush=True)
        raise SystemExit(2)
