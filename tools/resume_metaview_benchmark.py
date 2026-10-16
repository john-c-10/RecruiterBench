"""Resume Metaview with the user's explicit zero-on-failure exception for one role.

The frozen engine and original provider checkpoint remain unchanged. The failed
job is read again, never recreated. A separate receipt records any zero override.
"""
import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
from statistics import mean
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from benchmark.io import atomic_json, digest, identifier, lock, now, read_json
from benchmark.models import JUDGE_MODEL, JUDGE_REASONING_EFFORT
from benchmark.providers.openai import OpenAIJudge, ProviderError
from benchmark.runner import build_report, run, validate_plan
from benchmark.suites import DATASET
from tools.run_metaview_benchmark import StartupPollingSearch, derive_lite, load_keys, validate_startup_recovery


ROLE_ID = "02db71fb-9524-435e-8f96-4053011581c1"
AUTHORIZATION = "alright dude continue and retry this query, but if it breaks just 0 it and proceed"


class GatewayRetryJudge(OpenAIJudge):
    """Bounded recovery for HTTP 520, without changing prompts or scoring."""
    def complete(self, **kwargs):
        for attempt in range(3):
            try:
                result = super().complete(**kwargs)
                return {**result, "gateway_520_retries": attempt}
            except ProviderError as error:
                if error.code != "OPENAI_HTTP_ERROR" or error.status != 520 or attempt == 2:
                    raise
                self._wait(attempt)


def validate_gateway_recovery(root, plan):
    """Retry only known 520 judgments, after every search has been checkpointed."""
    expected = {f"{role['role_id']}__1" for role in plan["roles"]}
    searches = [read_json(p) for p in (root / "units/search").glob("*.json")]
    if ({u["key"] for u in searches} != expected
            or any(u["status"] != "complete" or u["result_sha256"] != digest(u["result"]) for u in searches)):
        raise ValueError("Judge recovery requires all original searches to be complete")
    recovering = []
    for stage in ("gate", "relevance"):
        for path in (root / "units" / stage).glob("*.json"):
            unit = read_json(path)
            if unit["status"] == "complete":
                continue
            if (unit["status"] != "uncertain" or unit.get("error") != {"code": "OPENAI_HTTP_ERROR", "status": 520}
                    or unit["input_sha256"] != digest(unit["inputs"])
                    or unit["inputs"]["model"] != JUDGE_MODEL
                    or unit["inputs"]["reasoning_effort"] != JUDGE_REASONING_EFFORT):
                raise ValueError("Additional unresolved judgment failures need inspection")
            recovering.append({"stage": stage, "key": unit["key"], "attempt": unit["attempt"], "error": unit["error"]})
    return recovering


class AuthorizedFailureSearch(StartupPollingSearch):
    def __init__(self, *args, root, plan_hash, role_id=ROLE_ID, **kwargs):
        super().__init__(*args, state_root=root / "provider-state", **kwargs)
        self.root, self.plan_hash, self.role_id = root, plan_hash, role_id
        self.target_key = f"{role_id}__1"
        self.receipt_path = root / "failure-zero" / f"{digest(self.target_key)}.json"

    def search(self, body, idempotency_key):
        if idempotency_key != self.target_key:
            return super().search(body, idempotency_key)
        checkpoint = self.state_root / f"{digest(idempotency_key)}.json"
        record = read_json(checkpoint) if checkpoint.exists() else {}
        if not record.get("search_id") or record.get("body_sha256") != digest(body):
            raise ValueError("Zero exception requires the matching, known original search")
        if self.receipt_path.exists():
            receipt = read_json(self.receipt_path)
            if (receipt["plan_sha256"] != self.plan_hash or receipt["key"] != self.target_key
                    or receipt["checkpoint_sha256"] != digest(record)
                    or receipt["result_sha256"] != digest(receipt["result"])):
                raise ValueError("Failure-zero receipt differs from the original search")
            return receipt["result"]
        try:
            return super().search(body, idempotency_key)
        except ProviderError as error:
            # This narrowly authorized exception cannot recreate a job, suppress
            # local integrity errors, or zero any other role's provider failure.
            result = {"search_id": record["search_id"],
                      "elapsed_ms": max(0, self.wall_clock() - record["started_epoch"]) * 1000,
                      "api_latency_ms": None, "backend_latency_ms": None,
                      "returned_count": 0, "candidates": [],
                      "meta": {"outcome": "provider_failure_scored_zero", "error_code": error.code},
                      "response": {"search_id": record["search_id"], "outcome": "provider_failure_scored_zero"}}
            atomic_json(self.receipt_path, {"created_at": now(), "plan_sha256": self.plan_hash,
                "key": self.target_key, "role_id": self.role_id, "authorization": AUTHORIZATION,
                "checkpoint": record, "checkpoint_sha256": digest(record),
                "retry_kind": "retrieve_existing_search_without_resubmitting",
                "error": {"code": error.code, "status": error.status},
                "result": result, "result_sha256": digest(result)})
            print(json.dumps({"event": "provider_failure_scored_zero", "role_id": self.role_id,
                              "search_id": record["search_id"], "code": error.code, "time": now()}), flush=True)
            return result


def annotate_failures(report, root):
    """Keep zero in quality denominators, but never call it a successful search."""
    report = deepcopy(report)
    adjustments = []
    for path in (root / "failure-zero").glob("*.json"):
        receipt = read_json(path)
        if (receipt["plan_sha256"] != report["plan_sha256"]
                or receipt["result_sha256"] != digest(receipt["result"])):
            raise ValueError("Failure-zero receipt failed verification")
        for row in report["searches"]:
            if f"{row['role_id']}__{row['repeat']}" != receipt["key"]:
                continue
            if row["search_status"] != "complete" or not row.get("scoreable"):
                continue
            if row["returned_count"] or any(row["metrics"].values()):
                raise ValueError("Failure-zero receipt conflicts with scored results")
            row.update(search_status="provider_failure_scored_zero", latency_ms=None,
                       provider_outcome="failure_scored_zero", provider_error=receipt["error"])
            adjustments.append({"role_id": row["role_id"], "repeat": row["repeat"],
                "outcome": "provider_failure_scored_zero", "error": receipt["error"],
                "retry_kind": receipt["retry_kind"], "recorded_at": receipt["created_at"]})
    if not adjustments:
        return report
    report["scoring_adjustments"] = {"authorization": AUTHORIZATION, "roles": adjustments,
        "method": "User-authorized provider failure receives zero at every cutoff; all frozen roles remain in the quality denominator. Failed searches are excluded from successful-search counts and latency averages."}

    def annotate_aggregate(aggregate, rows):
        failed = sum(r["search_status"] == "provider_failure_scored_zero" for r in rows)
        if not failed:
            return
        successful = [r for r in rows if r["search_status"] == "complete"]
        fastest = {}
        for row in successful:
            fastest[row["role_id"]] = min(fastest.get(row["role_id"], row["latency_ms"]), row["latency_ms"])
        aggregate.update(successful_searches=len(successful), failed_searches_scored_zero=failed,
            avg_latency_ms=mean(fastest.values()) if fastest else None,
            avg_candidates_returned=mean(r["returned_count"] for r in successful) if successful else None)

    annotate_aggregate(report["overall"], report["searches"])
    for field, groups in (("role_id", report["by_role"]), ("category", report["by_category"])):
        for key, aggregate in groups.items():
            annotate_aggregate(aggregate, [r for r in report["searches"] if r[field] == key])
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", default="hard-v1-metaview-source-20261015")
    parser.add_argument("--report-only", action="store_true")
    parser.add_argument("--retry-judge", action="store_true",
                        help="Resume inspected HTTP 520 judgment failures using only completed searches")
    args = parser.parse_args()
    root = Path("data/runs") / identifier(args.run_id)
    envelope = read_json(root / "plan.json")
    plan = validate_plan(envelope, DATASET)
    if plan["provider"] != "metaview" or plan["repeats"] != 1:
        raise ValueError("This recovery is scoped to the original single-repeat Metaview run")
    role = next(r for r in plan["roles"] if r["role_id"] == ROLE_ID)
    if args.report_only:
        report = build_report(root, plan, envelope["plan_sha256"])
    else:
        load_keys()
        searcher = AuthorizedFailureSearch(root=root, plan_hash=envelope["plan_sha256"], start_interval_seconds=20)
        with lock(root):
            recovering = validate_startup_recovery(root, envelope["plan_sha256"])
            if any(r["unit_key"] != searcher.target_key for r in recovering):
                raise ValueError("Additional unresolved search failures need inspection")
            launcher_hash = digest(Path(__file__).read_text())
            policy_path = root / "failure-zero-policy.json"
            if policy_path.exists():
                policy = read_json(policy_path)
                if policy["plan_sha256"] != envelope["plan_sha256"] or policy["role_id"] != ROLE_ID:
                    raise ValueError("Saved zero policy belongs to a different run or role")
            else:
                atomic_json(policy_path, {"created_at": now(), "plan_sha256": envelope["plan_sha256"],
                    "role_id": ROLE_ID, "authorization": AUTHORIZATION, "launcher_sha256": launcher_hash})
            if args.retry_judge:
                judgments = validate_gateway_recovery(root, plan)
                atomic_json(root / "judge-recoveries" / f"{os.getpid()}.json", {"created_at": now(),
                    "plan_sha256": envelope["plan_sha256"], "launcher_sha256": launcher_hash,
                    "judgments": judgments, "note": "Resume unfinished scoring after HTTP 520; reuse all completed searches and judgments. Model, effort, prompts, and scoring remain frozen."})
            previous = read_json(root / "process.json") if (root / "process.json").exists() else None
            if not previous or previous.get("pid") != os.getpid():
                atomic_json(root / "process.json", {"pid": os.getpid(), "started_at": now(),
                    "log": str(root / "run.log"), "searches_per_minute": 3, "expected_searches": 290,
                    "lite_roles": 58, "resume_mode": "judge_gateway_recovery" if args.retry_judge else "authorized_single_role_zero_on_failure",
                    "launcher_sha256": launcher_hash,
                    "previous_process": previous})
            # Resolve the known failure before admitting any remaining searches.
            saved_zero = searcher.receipt_path.exists()
            result = searcher.search(role["search_body"], searcher.target_key)
            print(json.dumps({"event": "saved_search_reused" if saved_zero else "saved_search_retried", "role_id": ROLE_ID,
                              "returned_count": result["returned_count"], "time": now()}), flush=True)
        report = run(envelope, DATASET, Path("data/runs"), args.run_id, search_client=searcher,
            judge=GatewayRetryJudge(model=JUDGE_MODEL, reasoning_effort=JUDGE_REASONING_EFFORT),
            parallel=16, judge_parallel=4, workers=8, interval_seconds=20,
            retry_uncertain=("judge",) if args.retry_judge else ("search",),
            progress=lambda message: print(now(), message, flush=True))
    lite = annotate_failures(derive_lite(report, DATASET), root)
    hard = annotate_failures(report, root)
    atomic_json(root / "summary.json", hard)
    atomic_json(root / "summary-hard.json", hard)
    atomic_json(root / "summary-lite.json", lite)
    print(json.dumps({"hard": {"status": hard["status"], "overall": hard["overall"]},
                      "lite": {"status": lite["status"], "overall": lite["overall"]}}), flush=True)
    return int(hard["status"] != "complete")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({"event": "stopped", "error_type": type(error).__name__,
                          "code": error.code if isinstance(error, ProviderError) else "LOCAL_ERROR"}), flush=True)
        raise SystemExit(2)
