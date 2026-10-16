"""Evaluate append-only browser captures without making search-provider API calls.

Usage: python -m benchmark.browser_evaluation init|evaluate|watch|report
The collector writes complete JSON page files atomically; a last page closes a
search only at 200 candidates or explicitly observed result exhaustion.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import os
from pathlib import Path
import shlex
from statistics import mean
import time
from urllib.parse import urlsplit

from .evaluators import Evaluator
from .io import atomic_json, digest, identifier, lock, now, read_json
from .metrics import METRICS, quality_metrics
from .models import JUDGE_MODEL, JUDGE_REASONING_EFFORT
from .profiles import PROFILE_FIELDS, prepare_candidates
from .providers.openai import OpenAIJudge
from .queries import build_query
from .runner import engine_hash, field_coverage_counts, make_plan
from .suites import DATASET, load_suite

DEFAULT_RUN = Path("data/runs/juicebox-computer-use-hard-v1")
PROVIDER = "juicebox"
MODE = "sourcing-computer-use"


def make_browser_plan(dataset=DATASET):
    # Reuse the existing corpus, rubric, scoring and workload freezing logic.
    plan = make_plan(dataset, base_url="https://app.juicebox.ai", mode="fast",
                     as_of="2026-09-30", benchmark="recruiterbench-hard-v1")["plan"]
    plan.update(provider=PROVIDER, mode=MODE, collection_method="computer_use_linkedin_urls",
                latency_basis="unavailable_setup_interrupted",
                enrichment={"provider": "wrangle", "endpoint": "https://app.usewrangle.com/api/developers/enrich/linkedin",
                            "evidence_source": "independently_enriched_linkedin_urls",
                            "cache_source": "verified_completed_wrangle_benchmark_profiles_with_original_timestamps",
                            "missing_profiles": "skip_and_continue_to_200_successfully_enriched",
                            "selection": "first_200_successfully_enriched_in_original_source_order",
                            "duplicates": "retain_original_occurrences_zero_credit",
                            "authorization": "User explicitly requested skipping invalid URLs and enrichment misses and continuing to 200 LinkedIn profiles"})
    for role in plan["roles"]:
        role["search_body"] = {"query": build_query(role["rubric"]), "limit": 200}
    plan["lite_benchmark"] = load_suite("recruiterbench-lite-v1", dataset)
    return {"plan_sha256": digest(plan), "plan": plan}


def validate_browser_plan(envelope, dataset=DATASET, *, allow_engine_revision=False):
    if digest(envelope["plan"]) != envelope["plan_sha256"]:
        raise ValueError("Browser plan hash mismatch")
    if envelope["plan"]["engine_sha256"] != engine_hash() and not allow_engine_revision:
        raise ValueError("Benchmark engine changed; inspect before creating a new run")
    regenerated = make_browser_plan(dataset)
    if allow_engine_revision:
        regenerated["plan"]["engine_sha256"] = envelope["plan"]["engine_sha256"]
        regenerated["plan_sha256"] = digest(regenerated["plan"])
    if regenerated != envelope:
        raise ValueError("Frozen corpus, rubrics or browser collection policy changed")
    return envelope["plan"]


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Capture timestamps must include a timezone")
    return parsed


def read_collection(root, role, *, source_directory="browser", require_experience=True):
    is_links = source_directory == "browser-links"
    directory = root / source_directory / identifier(role["role_id"])
    paths = sorted(directory.glob("page-*.json"))
    if not paths:
        return None
    candidates, page_hashes, pages = [], {}, []
    for number, path in enumerate(paths, 1):
        if path.name != f"page-{number:03d}.json":
            raise ValueError("Capture pages must be contiguous and numbered from page-001.json")
        page = read_json(path)
        if page["role_id"] != role["role_id"] or page["page"] != number:
            raise ValueError("Capture page identity differs from its path")
        if type(page["complete"]) is not bool or not isinstance(page["candidates"], list):
            raise ValueError("Invalid capture completion flag or candidate array")
        if page["complete"] and number != len(paths) and not is_links:
            raise ValueError("Cannot append after a completed collection")
        if urlsplit(page["source_url"]).hostname != "app.juicebox.ai":
            raise ValueError("Capture source must be the Juicebox browser UI")
        timestamp(page["collected_at"])
        if number == 1:
            if page["query"] != role["search_body"]["query"]:
                raise ValueError("Browser query differs from frozen benchmark brief")
            if page.get("timing_valid") is not False:
                timestamp(page["query_started_at"])
        candidates.extend(page["candidates"])
        page_hashes[path.name] = digest(page)
        pages.append(page)
    normalized = [] if is_links else prepare_candidates(candidates)
    for row, original in zip(normalized, candidates):
        if "enrichment_status" in original:
            row["enrichment_status"] = original["enrichment_status"]
    found = [row for row in normalized if row.get("enrichment_status") not in ("not_found", "source_unavailable")]
    if require_experience and found and not any(row["field_coverage"]["experiences"] for row in found):
        raise ValueError("No capture has experience evidence; inspect UI evidence parsing")
    complete = pages[-1]["complete"] and (not is_links or pages[-1].get("termination_reason") == "results_exhausted")
    if complete:
        reason = pages[-1].get("termination_reason")
        if reason not in ("target_reached", "results_exhausted"):
            raise ValueError("Completed collections require an observed termination reason")
        if reason == "target_reached" and len(normalized) != 200 and not is_links:
            raise ValueError("Target completion requires exactly 200 candidates")
    elapsed = None if pages[0].get("timing_valid") is False else (timestamp(pages[-1]["collected_at"]) - timestamp(pages[0]["query_started_at"])).total_seconds() * 1000
    if elapsed is not None and elapsed < 0:
        raise ValueError("Collection ended before it started")
    return {"candidates": normalized, "raw_candidates": candidates, "pages": pages, "page_hashes": page_hashes,
            "collection_complete": complete, "browser_collection_ms": elapsed,
            "termination_reason": pages[-1].get("termination_reason") if complete else None}


def read_result(root, role_id, plan_hash):
    path = root / "results" / f"{identifier(role_id)}__1.json"
    if not path.exists():
        return None
    saved = read_json(path)
    if saved["plan_sha256"] != plan_hash or saved["result_sha256"] != digest(saved["result"]):
        raise ValueError("Browser evaluation artifact changed")
    return saved["result"]


def evaluate_collection(root, role, collection, reviewer, plan_hash, workers=12):
    previous = read_result(root, role["role_id"], plan_hash)
    if previous:
        for path, fingerprint in previous["page_hashes"].items():
            if collection["page_hashes"].get(path) != fingerprint:
                raise ValueError("Previously imported browser page changed or disappeared")
        if previous["page_hashes"] == collection["page_hashes"] and all(
                row["status"] == "complete" or row["label"] in ("enrichment_not_found", "enrichment_source_unavailable") for row in previous["judgments"]):
            return False
    old = {row["rank"]: row for row in previous["judgments"]} if previous else {}
    judgments, pending = [], []
    for candidate in collection["candidates"]:
        prior = old.get(candidate["rank"])
        if prior and (prior["candidate_id"] != candidate["candidate_id"] or prior["profile_sha256"] != candidate["profile_sha256"]):
            raise ValueError("Previously imported candidate rank or evidence changed")
        if candidate.get("enrichment_status") in ("not_found", "source_unavailable") and candidate["duplicate_of_rank"] is None:
            judgments.append({"rank": candidate["rank"], "candidate_id": candidate["candidate_id"],
                              "profile_sha256": candidate["profile_sha256"], "status": "error",
                              "label": "enrichment_" + candidate["enrichment_status"], "relevance": None})
        elif prior and prior["status"] == "complete":
            judgments.append(prior)
        else:
            pending.append(candidate)
    errors = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(reviewer.evaluate, role, role["rubric"], row): row for row in pending}
        for future in as_completed(futures):
            candidate = futures[future]
            try:
                judgment = future.result()
            except Exception:
                errors += 1
                judgment = {"status": "error", "label": "judge_error", "relevance": None}
                for queued in futures:
                    queued.cancel()
            judgments.append({"rank": candidate["rank"], "candidate_id": candidate["candidate_id"],
                              "profile_sha256": candidate["profile_sha256"], **judgment})
    judgments.sort(key=lambda row: row["rank"])
    result = {"search_status": "complete" if collection["collection_complete"] else "collecting",
              "returned_count": len(collection["candidates"]),
              "field_coverage_counts": field_coverage_counts(collection["candidates"]),
              **{key: collection[key] for key in ("page_hashes", "collection_complete", "browser_collection_ms", "termination_reason")},
              "judgments": judgments}
    atomic_json(root / "results" / f"{role['role_id']}__1.json",
                {"plan_sha256": plan_hash, "result_sha256": digest(result), "result": result})
    print(f"{role['role_id']}: {len(judgments)} evaluated ranks; {errors} errors; {result['search_status']}", flush=True)
    if errors:
        raise RuntimeError("Judge errors saved; inspect units before explicitly retrying")
    return True


def aggregate(rows):
    finished = [row for row in rows if row["search_status"] == "complete"]
    scoreable = bool(rows) and len(finished) == len(rows) and all(row["scoreable"] for row in rows)
    return {"search_count": len(rows), "successful_searches": len(finished),
            "scoreable": scoreable,
            "avg_candidates_returned": mean(row["returned_count"] for row in finished) if finished else None,
            "returned_profiles": sum(row.get("returned_count", 0) for row in rows),
            "evaluated_profiles": sum(row.get("evaluated_count", 0) for row in rows),
            "field_coverage_counts": {field: sum(row.get("field_coverage_counts", {}).get(field, 0) for row in rows)
                                      for field in PROFILE_FIELDS},
            "avg_latency_ms": None,
            "avg_browser_collection_ms": None,
            "enrichment_not_found": sum(row.get("enrichment_not_found", 0) for row in rows),
            "enrichment_source_unavailable": sum(row.get("enrichment_source_unavailable", 0) for row in rows),
            "metrics": {key: mean(row["metrics"][key] for row in rows) if scoreable else None for key in METRICS}}


def report(root, envelope):
    plan, rows = envelope["plan"], []
    for role in plan["roles"]:
        base = {key: role[key] for key in ("role_id", "category", "company", "title", "job_url")}
        saved = read_result(root, role["role_id"], envelope["plan_sha256"])
        if not saved:
            rows.append({**base, "search_status": "missing", "scoreable": False})
            continue
        base.update({key: saved[key] for key in ("search_status", "returned_count", "field_coverage_counts", "browser_collection_ms")})
        base["evaluated_count"] = sum(row["status"] == "complete" for row in saved["judgments"])
        base["enrichment_not_found"] = sum(row["label"] == "enrichment_not_found" for row in saved["judgments"])
        base["enrichment_source_unavailable"] = sum(row["label"] == "enrichment_source_unavailable" for row in saved["judgments"])
        if saved["collection_complete"]:
            base.update(quality_metrics(saved["judgments"], saved["returned_count"]))
        else:
            base.update(scoreable=False, metrics=dict.fromkeys(METRICS))
        rows.append(base)
    for suite, selected in (("hard", rows), ("lite", [row for row in rows if row["role_id"] in set(plan["lite_benchmark"]["role_ids"])])):
        overall = aggregate(selected)
        summary = {"schema_version": "1.0", "plan_sha256": envelope["plan_sha256"],
                   "provider": PROVIDER, "platform": "Juicebox (computer use)", "mode": MODE,
                   "benchmark": f"recruiterbench-{suite}-v1", "as_of": plan["as_of"],
                   "model": plan["model"], "reasoning_effort": plan["reasoning_effort"],
                   "generated_at": now(), "status": "complete" if overall["scoreable"] else "incomplete",
                   "expected_searches": len(selected), "overall": overall, "searches": selected,
                   "by_category": {category: aggregate([row for row in selected if row["category"] == category])
                                   for category in sorted({row["category"] for row in selected})},
                   "enrichment": plan.get("enrichment"),
                   "latency_note": "Latency is unavailable: collection timing includes interrupted setup and pagination. Do not compare these durations with provider API latency."}
        atomic_json(root / f"summary-{suite}.json", summary)
    return {"expected_searches": len(rows), **aggregate(rows)}


def load_env(path):
    if not path.exists():
        return
    # Parse local assignment values as data, never execute the env file.
    for line in path.read_text().splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[7:].strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() not in ("OPENAI_API_KEY", "WRANGLE_API_KEY"):
            continue
        parts = shlex.split(value, comments=True)
        if len(parts) != 1:
            raise ValueError("Credential assignment is malformed")
        os.environ.setdefault(key.strip(), parts[0])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init", "evaluate", "watch", "report"))
    parser.add_argument("--root", type=Path, default=DEFAULT_RUN)
    parser.add_argument("--dataset", type=Path, default=DATASET)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--record-compatible-engine-update", action="store_true")
    parser.add_argument("--retry-uncertain", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=5)
    args = parser.parse_args(argv)
    if not 1 <= args.workers <= 32 or not 1 <= args.poll_seconds <= 60:
        parser.error("workers must be 1..32 and poll-seconds 1..60")
    with lock(args.root):
        plan_path = args.root / "plan.json"
        if args.command == "init":
            envelope = make_browser_plan(args.dataset)
            if plan_path.exists() and read_json(plan_path) != envelope:
                raise ValueError("Existing run has a different plan")
            atomic_json(plan_path, envelope)
            lite = set(envelope["plan"]["lite_benchmark"]["role_ids"])
            roles = envelope["plan"]["roles"]
            queue = sorted(enumerate(roles), key=lambda item: (item[1]["role_id"] not in lite, item[0]))
            atomic_json(args.root / "collection-queue.json", [{"role_id": role["role_id"], "title": role["title"],
                "company": role["company"], "category": role["category"], "query": role["search_body"]["query"],
                "in_lite": role["role_id"] in lite} for _, role in queue])
            print(f"Frozen {len(roles)} Hard roles; {len(lite)} form the Lite subset", flush=True)
            return 0
        envelope = read_json(plan_path)
        plan = validate_browser_plan(envelope, args.dataset, allow_engine_revision=args.record_compatible_engine_update)
        if args.record_compatible_engine_update:
            current = engine_hash()
            atomic_json(args.root / "engine-revisions" / f"{current}.json", {"plan_sha256": envelope["plan_sha256"],
                "original_engine_sha256": plan["engine_sha256"], "current_engine_sha256": current, "recorded_at": now(),
                "reason": "Routine orchestration reliability fix; regenerated frozen corpus, query, rubric, scoring and evidence-selection policy are exactly unchanged."})
        if args.command == "report":
            print(report(args.root, envelope))
            return 0
        load_env(args.env_file)
        if not os.environ.get("OPENAI_API_KEY"):
            raise ValueError("OPENAI_API_KEY is required for evaluator calls")
        reviewer = Evaluator(OpenAIJudge(model=JUDGE_MODEL, reasoning_effort=JUDGE_REASONING_EFFORT), args.root, plan["as_of"],
                             retry_failed=args.retry_failed, retry_uncertain=args.retry_uncertain)
        print(f"Ready: {args.workers} judge workers, watching {args.root / 'browser'}", flush=True)
        while True:
            try:
                for role in plan["roles"]:
                    collection = read_collection(args.root, role)
                    if collection:
                        changed = evaluate_collection(args.root, role, collection, reviewer, envelope["plan_sha256"], args.workers)
                        if changed:
                            report(args.root, envelope)
            finally:
                summary = report(args.root, envelope)
            if args.command == "evaluate" or summary["scoreable"] or (args.root / "STOP").exists():
                return 0
            time.sleep(args.poll_seconds)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except (ValueError, RuntimeError, KeyError, OSError) as error:
        # Evidence and upstream errors remain in private artifacts, not stdout.
        print(f"Browser evaluator stopped: {type(error).__name__}: {str(error) if isinstance(error, (ValueError, RuntimeError)) else 'inspect private capture format'}", flush=True)
        raise SystemExit(2)
