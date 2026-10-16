"""Run Hard once and calculate the exact Lite subset with the existing judges."""
import argparse
from collections import Counter
import json
import os
from pathlib import Path
import shlex
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from benchmark.evaluators import Evaluator
from benchmark.io import atomic_json, digest, identifier, lock, now, read_json
from benchmark.models import JUDGE_MODEL, JUDGE_REASONING_EFFORT
from benchmark.metrics import summarize
from benchmark.providers import SEARCH_PROVIDERS
from benchmark.providers.openai import OpenAIJudge, ProviderError
from benchmark.runner import (ReplaySearch, build_report, field_coverage_counts,
                              make_plan, run, validate_plan)
from benchmark.suites import DATASET
from tools import treg_provider
from tools.run_metaview_benchmark import derive_lite


def load_keys():
    for line in Path(".env").read_text().splitlines():
        line = line.strip().removeprefix("export ")
        if "=" not in line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        if key.strip() in ("TREG_API_KEY", "OPENAI_API_KEY"):
            parts = shlex.split(value, comments=True)
            if parts:
                os.environ[key.strip()] = parts[0]
    if not all(os.environ.get(key) for key in ("TREG_API_KEY", "OPENAI_API_KEY")):
        raise ValueError("Set TREG_API_KEY and OPENAI_API_KEY in .env")


def finalize_latency(report, recovered_roles):
    """A gateway-lost result recovered from cache has no measured search RTT."""
    excluded = [row for row in report["searches"] if row["role_id"] in recovered_roles]
    for row in excluded:
        row.setdefault("cached_retrieval_ms", row.get("latency_ms"))
        row["latency_ms"] = None
        row["latency_excluded_reason"] = "Original request lost at gateway; idempotent response retrieval is not search latency"
    measured = [row for row in report["searches"] if row.get("search_status") == "complete"
                and row["role_id"] not in recovered_roles]
    latency = summarize(measured, len(measured))
    report["overall"]["avg_latency_ms"] = latency["overall"]["avg_latency_ms"]
    report["overall"]["latency_query_count"] = len({r["role_id"] for r in measured})
    for group in ("by_role", "by_category"):
        for key, aggregate in report[group].items():
            aggregate["avg_latency_ms"] = latency[group].get(key, {}).get("avg_latency_ms")
    report["latency_excluded_roles"] = sorted({r["role_id"] for r in excluded})


def search_cost(result):
    headers = result["meta"]["headers"]
    if headers.get("x-treg-idempotent-replay") == "true":
        original = headers.get("x-treg-original-cost-micro")
        return int(original) / 1_000_000 if original is not None else None
    return result["cost_dollars"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", default="hard-v1-treg-routed-20261015")
    parser.add_argument("--report-only", action="store_true")
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--import-preflight", type=Path)
    parser.add_argument("--retry-judge", action="store_true")
    args = parser.parse_args()
    # Register locally so unrelated benchmark runs and their frozen engine
    # hashes are unaffected by this operational launcher.
    SEARCH_PROVIDERS[treg_provider.NAME] = treg_provider
    root = Path("data/runs") / identifier(args.run_id)
    path = root / "plan.json"
    envelope = read_json(path) if path.exists() else make_plan(DATASET, provider="treg", mode="routed",
        benchmark="recruiterbench-hard-v1", as_of="2026-09-30")
    plan = validate_plan(envelope, DATASET)
    source = {str(p.relative_to(Path(__file__).resolve().parents[1])): digest(p.read_text()) for p in
              (Path(__file__).resolve(), Path(treg_provider.__file__).resolve(),
               Path(__file__).resolve().with_name("run_metaview_benchmark.py"))}
    provenance_path = root / "launcher-sources.json"
    if provenance_path.exists() and read_json(provenance_path) != source:
        raise ValueError("Launcher source changed; inspect before resuming")
    if not args.report_only:
        load_keys()
        searcher = treg_provider.TregSearch(state_root=root / "provider-state")
        judge = OpenAIJudge(model=JUDGE_MODEL, reasoning_effort=JUDGE_REASONING_EFFORT)
        with lock(root):
            atomic_json(provenance_path, source)
            atomic_json(path, envelope)
            imported = root / "imported-preflight.json"
            if args.import_preflight and not imported.exists():
                request = read_json(args.import_preflight / "request.json")
                receipt = read_json(args.import_preflight / "response.json")
                role = plan["roles"][0]
                if request != {"role_id": role["role_id"], "body": role["search_body"]}:
                    raise ValueError("Preflight differs from the frozen first role")
                treg_provider.normalize_receipt(receipt)
                atomic_json(imported, {"request": request, "receipt": receipt})
            if imported.exists():
                saved = read_json(imported)
                result = treg_provider.normalize_receipt(saved["receipt"])
                searcher = ReplaySearch({digest(saved["request"]["body"]): result}, searcher)
            # Prove a real judgment before launching all searches. It is cached
            # under the same run and reused by the normal runner.
            if not (root / "preflight.json").exists():
                if not imported.exists():
                    raise ValueError("Run a saved first-role preflight and pass --import-preflight")
                first = result["candidates"][0]
                evaluated = Evaluator(judge, root, plan["as_of"]).evaluate(plan["roles"][0], plan["roles"][0]["rubric"], first)
                if evaluated["status"] != "complete":
                    raise ValueError("Preflight judgment incomplete")
                record = {"time": now(), "returned_count": result["returned_count"],
                          "field_coverage_counts": field_coverage_counts(result["candidates"]),
                          "first_judgment_status": evaluated["status"], "meta": result["meta"]}
                atomic_json(root / "preflight.json", record)
                print(json.dumps({"event": "preflight_passed", **record}), flush=True)
        if args.preflight_only:
            return 0
        report = run(envelope, DATASET, Path("data/runs"), args.run_id, search_client=searcher, judge=judge,
            parallel=10, judge_parallel=8, workers=16, interval_seconds=1,
            retry_failed=("judge",) if args.retry_judge else (),
            retry_uncertain=("judge",) if args.retry_judge else (),
            progress=lambda message: print(now(), message, flush=True))
    else:
        report = build_report(root, plan, envelope["plan_sha256"])
    units = [read_json(p) for p in (root / "units/search").glob("*.json")]
    completed = [u for u in units if u["status"] == "complete"]
    searches = [u["result"] for u in completed]
    recovered = {u["inputs"]["role_id"] for u in completed
                 if u["result"]["meta"]["headers"].get("x-treg-idempotent-replay") == "true"}
    # Derive before marking unavailable timings: the shared engine's summary
    # expects numeric latency. Only this provider's reports need the exclusion.
    lite = derive_lite(report, DATASET)
    finalize_latency(report, recovered)
    finalize_latency(lite, recovered)
    atomic_json(root / "summary.json", report)
    atomic_json(root / "summary-hard.json", report)
    atomic_json(root / "summary-lite.json", lite)
    call_ids = [r["search_id"] for r in searches]
    if len(call_ids) != len(set(call_ids)):
        raise ValueError("Duplicate search call IDs; inspect before aggregating charges")
    routing = {"completed_searches": len(searches),
               "served_by": dict(Counter(r["meta"]["served_by"] for r in searches)),
               "reported_search_cost_usd": sum(search_cost(r) or 0 for r in searches),
               "searches_without_cost": sum(search_cost(r) is None for r in searches),
               "recovered_cached_results": len(recovered),
               "cost_method": "Unique search call IDs; original charge header for cached recovery, response charge otherwise"}
    atomic_json(root / "routing-summary.json", routing)
    print(json.dumps({"hard": {"status": report["status"], "overall": report["overall"]},
                      "lite": {"status": lite["status"], "overall": lite["overall"]}, "routing": routing}), flush=True)
    return int(report["status"] != "complete")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({"event": "stopped", "error_type": type(error).__name__,
                          "code": error.code if isinstance(error, ProviderError) else "LOCAL_ERROR"}), flush=True)
        raise SystemExit(2)
