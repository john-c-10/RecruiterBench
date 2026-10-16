"""Run Treg's explicit Parallel provider: Lite searches first, then all Hard."""
import argparse
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from benchmark.evaluators import Evaluator
from benchmark.io import atomic_json, digest, identifier, lock, now, read_json
from benchmark.models import JUDGE_MODEL, JUDGE_REASONING_EFFORT
from benchmark.providers import SEARCH_PROVIDERS
from benchmark.providers.openai import OpenAIJudge, ProviderError
from benchmark.runner import build_report, field_coverage_counts, make_plan, run, search_key, validate_plan
from benchmark.state import execute_unit
from benchmark.suites import DATASET, load_suite
from tools import treg_parallel_provider, treg_provider
from tools.run_metaview_benchmark import derive_lite
from tools.run_treg_benchmark import finalize_latency, load_keys, search_cost


def search_role(root, envelope, role, searcher):
    plan = envelope["plan"]
    inputs = {"plan_sha256": envelope["plan_sha256"], "role_id": role["role_id"], "repeat": 1,
              "base_url": plan["api_base_url"], "body": role["search_body"]}
    return execute_unit(root, "search", search_key(role["role_id"], 1), inputs,
        lambda attempt: searcher.search(role["search_body"], digest({"inputs": inputs, "attempt": attempt})))


def prefetch_lite(root, envelope, roles, searcher):
    """A completion barrier prevents any non-Lite search from starting early."""
    if (root / "lite-searches-complete.json").exists():
        # Revalidate every cached unit instead of trusting only the marker.
        for role in roles:
            search_role(root, envelope, role, searcher)
        return
    iterator = iter(roles)
    with ThreadPoolExecutor(max_workers=10) as pool:
        pending = {pool.submit(search_role, root, envelope, role, searcher): role
                   for role in [next(iterator) for _ in range(min(10, len(roles))) ]}
        completed = 0
        while pending:
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            # Check the whole completed batch before admitting more work.
            for future in done:
                future.result()
            for future in done:
                role = pending.pop(future)
                completed += 1
                print(now(), f"Lite search {completed}/{len(roles)}: {role['role_id']}", flush=True)
                role = next(iterator, None)
                if role is not None:
                    pending[pool.submit(search_role, root, envelope, role, searcher)] = role
    atomic_json(root / "lite-searches-complete.json", {"completed_at": now(),
        "plan_sha256": envelope["plan_sha256"], "role_ids": [r["role_id"] for r in roles]})


def reports(root, envelope, report):
    completed = [u for p in (root / "units/search").glob("*.json")
                 if (u := read_json(p))["status"] == "complete"]
    results = [u["result"] for u in completed]
    recovered = {u["inputs"]["role_id"] for u in completed
                 if u["result"]["meta"]["headers"].get("x-treg-idempotent-replay") == "true"}
    lite = derive_lite(report, DATASET)
    finalize_latency(report, recovered)
    finalize_latency(lite, recovered)
    for name, value in (("summary", report), ("summary-hard", report), ("summary-lite", lite)):
        atomic_json(root / f"{name}.json", value)
    for field in ("search_id", "entity_set_id"):
        ids = [r[field] if field == "search_id" else r["meta"][field] for r in results]
        if any(not value for value in ids) or len(ids) != len(set(ids)):
            raise ValueError(f"Missing or repeated {field}; inspect before publishing")
    routing = {"completed_searches": len(results), "requested_results": 100, "parallel_searches": 10,
        "endpoint": treg_parallel_provider.ENDPOINT,
        "served_by": dict(Counter(r["meta"]["served_by"] for r in results)),
        "reported_search_cost_usd": round(sum(search_cost(r) or 0 for r in results), 6),
        "searches_without_cost": sum(search_cost(r) is None for r in results),
        "recovered_cached_results": len(recovered)}
    atomic_json(root / "routing-summary.json", routing)
    atomic_json(root / "status.json", {"status": report["status"], "updated_at": now(),
        "hard": report["overall"], "lite": lite["overall"], **routing})
    print(json.dumps({"hard": {"status": report["status"], "overall": report["overall"]},
        "lite": {"status": lite["status"], "overall": lite["overall"]}, "routing": routing}), flush=True)
    return report["status"] == "complete"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", default="hard-v1-treg-parallel-20261015")
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--report-only", action="store_true")
    parser.add_argument("--retry-judge", action="store_true")
    args = parser.parse_args()
    SEARCH_PROVIDERS[treg_parallel_provider.NAME] = treg_parallel_provider
    root = Path("data/runs") / identifier(args.run_id)
    path = root / "plan.json"
    envelope = read_json(path) if path.exists() else make_plan(DATASET,
        provider=treg_parallel_provider.NAME, mode="parallel", benchmark="recruiterbench-hard-v1", as_of="2026-09-30")
    plan = validate_plan(envelope, DATASET)
    lite_ids = load_suite("recruiterbench-lite-v1", DATASET)["role_ids"]
    lite_roles = plan["roles"][:len(lite_ids)]
    if [r["role_id"] for r in lite_roles] != lite_ids:
        raise ValueError("Hard plan must start with the exact frozen Lite role order")
    project = Path(__file__).resolve().parents[1]
    files = [Path(__file__).resolve(), Path(treg_parallel_provider.__file__).resolve(),
             Path(treg_provider.__file__).resolve(), project / "tools/run_treg_benchmark.py",
             project / "tools/run_metaview_benchmark.py"]
    sources = {str(p.relative_to(project)): digest(p.read_text()) for p in files}
    provenance = root / "launcher-sources.json"
    if provenance.exists() and read_json(provenance) != sources:
        raise ValueError("Launcher sources changed; inspect before resuming")
    if args.report_only:
        return int(not reports(root, envelope, build_report(root, plan, envelope["plan_sha256"])))
    load_keys()
    searcher = treg_parallel_provider.ParallelSearch(state_root=root / "provider-state")
    judge = OpenAIJudge(model=JUDGE_MODEL, reasoning_effort=JUDGE_REASONING_EFFORT)
    with lock(root):
        atomic_json(path, envelope)
        atomic_json(provenance, sources)
        atomic_json(root / "status.json", {"status": "running", "updated_at": now(),
            "requested_results": 100, "parallel_searches": 10, "hard_roles": 290, "lite_first": len(lite_ids)})
        if not (root / "preflight.json").exists():
            records = []
            for role in (lite_roles[0], lite_roles[35]):
                result = search_role(root, envelope, role, searcher)
                coverage = field_coverage_counts(result["candidates"])
                if not result["returned_count"] or not coverage["experiences"]:
                    raise ValueError("Preflight lacks scoreable professional evidence")
                evaluation = Evaluator(judge, root, plan["as_of"]).evaluate(role, role["rubric"], result["candidates"][0])
                if evaluation["status"] != "complete":
                    raise ValueError("Preflight judgment incomplete")
                records.append({"role_id": role["role_id"], "returned_count": result["returned_count"],
                    "first_profile_sha256": result["candidates"][0]["profile_sha256"],
                    "field_coverage_counts": coverage, "first_judgment": evaluation})
            if records[0]["first_profile_sha256"] == records[1]["first_profile_sha256"]:
                raise ValueError("Distinct preflight roles repeated the first profile")
            atomic_json(root / "preflight.json", {"completed_at": now(), "roles": records})
            print(now(), "Two live Lite searches and judgments verified", flush=True)
        if args.preflight_only:
            return 0
        prefetch_lite(root, envelope, lite_roles, searcher)
    report = run(envelope, DATASET, Path("data/runs"), args.run_id, search_client=searcher, judge=judge,
        parallel=10, judge_parallel=8, workers=16, interval_seconds=0,
        retry_failed=("judge",) if args.retry_judge else (),
        retry_uncertain=("judge",) if args.retry_judge else (),
        progress=lambda message: print(now(), message, flush=True))
    return int(not reports(root, envelope, report))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({"event": "stopped", "error_type": type(error).__name__,
            "code": error.code if isinstance(error, ProviderError) else "LOCAL_ERROR"}), flush=True)
        raise SystemExit(2)
