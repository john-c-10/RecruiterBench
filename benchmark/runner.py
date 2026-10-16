"""Plan, freeze, search, judge, and resume a standalone benchmark run."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from datetime import date
from pathlib import Path
import os
from threading import Event

from .evaluators import Evaluator
from .io import atomic_json, digest, identifier, lock, now, read_json
from .metrics import quality_metrics, summarize
from .models import JUDGE_MODEL, JUDGE_REASONING_EFFORT, ROOT, load_roles
from .profiles import PROFILE_FIELDS
from .providers.openai import OpenAIJudge
from .providers import search_client as provider_client, search_provider
from .providers.wrangle import origin
from .queries import build_query, load_rubric, select_roles
from .scoring import GATE_PARTIAL_CAP, VERDICT_CREDIT, TOP1_DEFINITION, SCORING_VERSION, UNKNOWN_POLICY, validate_scoring
from .interpretation import POLICY
from .state import Pacer, execute_unit
from .suites import load_suite


def engine_hash() -> str:
    files = sorted(path for path in ROOT.rglob("*") if path.is_file() and path.suffix in (".py", ".txt", ".json"))
    return digest({str(path.relative_to(ROOT)): path.read_text() for path in files})


def make_plan(dataset: Path, *, base_url: str | None = None, mode: str, provider: str = "wrangle", unknown_policy: str = UNKNOWN_POLICY, top1_definition: str = TOP1_DEFINITION,
              as_of: str, repeats: int = 1, limit: int | None = None, role_ids: list[str] | None = None,
              benchmark: str | None = None) -> dict:
    date.fromisoformat(as_of)
    source = search_provider(provider)
    base_url = base_url or source.DEFAULT_BASE_URL
    if not base_url:
        raise ValueError(f"Pass --api-base-url for the {provider} search provider")
    if mode not in source.MODES:
        raise ValueError(f"Mode for {provider} must be one of: " + ", ".join(source.MODES))
    validate_scoring(unknown_policy, top1_definition)
    if read_json(dataset / "manifest.json").get("interpretation_policy") != POLICY:
        raise ValueError("Dataset does not use the current interpretation policy")
    if type(repeats) is not int or not 1 <= repeats <= 20:
        raise ValueError("Repetitions must be 1 through 20")
    suite = None
    if benchmark:
        if limit is not None or role_ids:
            raise ValueError("A named benchmark fixes its roles; do not combine it with --limit or --role-id")
        suite = load_suite(benchmark, dataset)
        role_ids = suite["role_ids"]
    roles = select_roles(load_roles(dataset), role_ids=role_ids, limit=limit)
    if not roles:
        raise ValueError("No selected roles")
    selected = []
    for role in roles:
        record = load_rubric(dataset, role)
        snapshot = read_json(dataset / role["snapshot_path"])
        selected.append({"role_id": role["role_id"], "category": role["category"], "company": role["company"],
                         "title": role["title"], "job_url": role["job_url"], "snapshot_sha256": role["snapshot_sha256"],
                         "rubric_sha256": record["rubric_sha256"], "rubric": record["rubric"],
                         "experience_context": record.get("experience_context", []),
                         "job_description": snapshot["job"]["descriptionPlain"],
                         "search_body": source.search_body(build_query(record["rubric"]), mode)})
    searches = len(roles) * repeats
    plan = {"schema_version": "1.0", "benchmark": {"name": benchmark, "suite_sha256": digest(suite)} if suite else None,
            "dataset_id": dataset.name,
            "dataset_manifest_sha256": digest(read_json(dataset / "manifest.json")),
            "engine_sha256": engine_hash(), "provider": provider, "api_base_url": origin(base_url), "mode": mode,
            "limit": source.MAX_RESULTS,
            "model": JUDGE_MODEL, "reasoning_effort": JUDGE_REASONING_EFFORT, "as_of": as_of, "repeats": repeats,
            "scoring": {"unknown_policy": unknown_policy, "top1_definition": top1_definition,
                        "version": SCORING_VERSION, "factor": "adequate_experience_to_do_the_role",
                        "obvious_overqualification": "fail",
                        "verdict_credit": VERDICT_CREDIT, "gate_partial_cap": GATE_PARTIAL_CAP,
                        "short_results": "zero_pad_to_cutoff", "duplicates": "zero_at_original_rank",
                        "underqualification": "fail"},
            "interpretation_policy": POLICY,
            "roles": selected,
            "workload": {"search_requests": searches, "maximum_returned_profiles": searches * source.MAX_RESULTS,
                         "maximum_judge_requests": searches * source.MAX_RESULTS * 4,
                         "note": "Upper bounds include one validation repair per review and exclude explicit retries. Reused profile judgments can reduce calls; rubric extraction is already separate."}}
    return {"plan_sha256": digest(plan), "plan": plan}


def validate_plan(envelope: dict, dataset: Path) -> dict:
    plan = envelope["plan"]
    if digest(plan) != envelope["plan_sha256"]:
        raise ValueError("Plan hash mismatch")
    regenerated = make_plan(dataset, base_url=plan["api_base_url"], mode=plan["mode"], provider=plan["provider"],
                            unknown_policy=plan["scoring"]["unknown_policy"], top1_definition=plan["scoring"]["top1_definition"],
                            as_of=plan["as_of"], repeats=plan["repeats"],
                            **({"benchmark": plan["benchmark"]["name"]} if plan.get("benchmark")
                               else {"role_ids": [r["role_id"] for r in plan["roles"]]}))
    if regenerated != envelope:
        raise ValueError("Dataset, engine, prompts, or plan inputs changed; generate a new plan/run")
    return plan


RETRY_STAGES = ("search", "judge")


def retry_stages(value) -> frozenset:
    """Retries are opted into per stage so a judge retry never re-sends a paid search."""
    if isinstance(value, (str, bool)) or not set(value) <= set(RETRY_STAGES):
        raise ValueError("Retry stages must be a collection of: " + ", ".join(RETRY_STAGES))
    return frozenset(value)


def field_coverage_counts(candidates: list[dict]) -> dict:
    return {field: sum(candidate["field_coverage"][field] for candidate in candidates) for field in PROFILE_FIELDS}


def search_key(role_id: str, repeat: int) -> str:
    return f"{role_id}__{repeat}"


def build_report(root: Path, plan: dict, plan_hash: str) -> dict:
    rows = []
    for repeat in range(1, plan["repeats"] + 1):
        for role in plan["roles"]:
            key = search_key(role["role_id"], repeat)
            path = root / "results" / f"{key}.json"
            base = {name: role[name] for name in ("role_id", "category", "company", "title", "job_url")}
            base["repeat"] = repeat
            if not path.exists():
                rows.append({**base, "search_status": "missing", "scoreable": False, "latency_ms": None})
                continue
            saved = read_json(path)
            result = saved["result"]
            if saved["plan_sha256"] != plan_hash or saved["result_sha256"] != digest(result):
                raise ValueError("Search evaluation artifact was modified")
            if result["search_status"] != "complete":
                rows.append({**base, "search_status": result["search_status"], "scoreable": False, "latency_ms": None,
                             **{name: result[name] for name in ("returned_count", "field_coverage_counts") if name in result}})
                continue
            quality = quality_metrics(result["judgments"], result["returned_count"],
                                      unknown_policy=plan["scoring"]["unknown_policy"],
                                      top1_definition=plan["scoring"]["top1_definition"])
            # Whitelist public aggregates; no person IDs, evidence, or profiles.
            rows.append({**base, "search_status": "complete", "latency_ms": result["latency_ms"],
                         "api_latency_ms": result["api_latency_ms"], "backend_latency_ms": result["backend_latency_ms"],
                         "field_coverage_counts": result["field_coverage_counts"], **quality})
    summary = summarize(rows, plan["workload"]["search_requests"])
    replay = root / "replay.json"
    return {"schema_version": "1.0", "plan_sha256": plan_hash, "benchmark": plan.get("benchmark"), "dataset_id": plan["dataset_id"],
            "provider": plan["provider"], "mode": plan["mode"], "model": plan["model"], "reasoning_effort": plan["reasoning_effort"],
            "as_of": plan["as_of"], "generated_at": now(), "scoring": plan["scoring"],
            **({"search_replay": read_json(replay)} if replay.exists() else {}), **summary}


class ReplaySearch:
    """Serve a new plan's searches from a completed run's saved results, by role.

    With a fallback client, roles without a saved result are searched live.
    """
    def __init__(self, results_by_body: dict, fallback=None):
        self.results, self.fallback = results_by_body, fallback

    def search(self, body: dict, idempotency_key: str) -> dict:
        result = self.results.get(digest(body))
        if result is None:
            if self.fallback is None:
                raise ValueError("No saved search result to replay for this role")
            return self.fallback.search(body, idempotency_key)
        return deepcopy(result)


def rescore(envelope: dict, dataset: Path, runs_root: Path, run_id: str, source_run_id: str, *,
            search_missing: bool = False, **options) -> dict:
    """Judge a source run's saved searches under the current plan, rubrics, and judges.

    Makes no search requests. The new plan's search queries may differ from the
    queries that produced the saved results; replay.json records every role
    where they differ, and the report carries that record.
    """
    plan = validate_plan(envelope, dataset)
    source_root = runs_root / identifier(source_run_id)
    source = read_json(source_root / "plan.json")
    if (source["plan"].get("provider", "wrangle") != plan["provider"] or source["plan"]["api_base_url"] != plan["api_base_url"]
            or source["plan"]["mode"] != plan["mode"]):
        raise ValueError("Rescoring requires the same search provider, origin, and mode as the source run")
    if plan["repeats"] != 1 or source["plan"]["repeats"] != 1:
        raise ValueError("Rescoring supports single-repetition plans only")
    saved = {}
    for path in (source_root / "units" / "search").glob("*.json"):
        unit = read_json(path)
        if unit["status"] == "complete" and unit["inputs"]["plan_sha256"] == source["plan_sha256"]:
            if unit["result_sha256"] != digest(unit["result"]):
                raise ValueError("Saved search artifact was modified")
            saved[unit["inputs"]["role_id"]] = unit
    missing = [role["role_id"] for role in plan["roles"] if role["role_id"] not in saved]
    if missing and not search_missing:
        raise ValueError(f"Source run has no completed search for {len(missing)} planned roles; pass --search-missing to search them live")
    if missing and search_client_missing_key(plan["provider"]):
        raise ValueError(f"Set {search_provider(plan['provider']).API_KEY_ENV} before searching missing roles")
    results, changed = {}, []
    for role in plan["roles"]:
        if role["role_id"] in missing:
            continue
        unit = saved[role["role_id"]]
        results[digest(role["search_body"])] = unit["result"]
        if unit["inputs"]["body"] != role["search_body"]:
            changed.append(role["role_id"])
    root = runs_root / identifier(run_id)
    record = {"source_run_id": source_run_id, "source_plan_sha256": source["plan_sha256"],
              "source_dataset_id": source["plan"]["dataset_id"], "replayed_searches": len(plan["roles"]) - len(missing),
              "roles_with_different_query": changed, "live_searches": missing,
              "note": "Saved results from the source run were judged under this plan; roles listed in live_searches were searched live."}
    if (root / "replay.json").exists() and read_json(root / "replay.json") != record:
        raise ValueError("Run ID already replays a different source")
    atomic_json(root / "replay.json", record)
    fallback = provider_client(plan["provider"], plan["api_base_url"], state_root=root / "provider-state") if missing else None
    return run(envelope, dataset, runs_root, run_id, search_client=ReplaySearch(results, fallback),
               **({} if missing else {"interval_seconds": 0}), **options)


def search_client_missing_key(provider: str) -> bool:
    return not os.environ.get(search_provider(provider).API_KEY_ENV)


def run(envelope: dict, dataset: Path, runs_root: Path, run_id: str, *, workers=8, parallel=1, judge_parallel=None,
        interval_seconds=300 / 45, retry_failed=(), retry_uncertain=(), search_client=None, judge=None, progress=print) -> dict:
    plan = validate_plan(envelope, dataset)
    retry_failed, retry_uncertain = retry_stages(retry_failed), retry_stages(retry_uncertain)
    judge_parallel = parallel if judge_parallel is None else judge_parallel
    if type(workers) is not int or not 1 <= workers <= 32:
        raise ValueError("Judge workers must be 1 through 32")
    if type(parallel) is not int or not 1 <= parallel <= 16:
        raise ValueError("Parallel searches must be 1 through 16")
    if type(judge_parallel) is not int or not 1 <= judge_parallel <= 16:
        raise ValueError("Parallel judged roles must be 1 through 16")
    if search_client is None and search_client_missing_key(plan["provider"]):
        raise ValueError(f"Set {search_provider(plan['provider']).API_KEY_ENV} before executing searches")
    if judge is None and not os.environ.get("OPENAI_API_KEY"):
        raise ValueError("Set OPENAI_API_KEY before judging")
    root = runs_root / identifier(run_id)
    pacer = Pacer(interval_seconds)
    with lock(root):
        plan_path = root / "plan.json"
        if plan_path.exists() and read_json(plan_path) != envelope:
            raise ValueError("Run ID already belongs to a different plan")
        if not plan_path.exists():
            atomic_json(plan_path, envelope)
        searcher = search_client or provider_client(plan["provider"], plan["api_base_url"], state_root=root / "provider-state")
        reviewer = Evaluator(judge or OpenAIJudge(model=JUDGE_MODEL, reasoning_effort=JUDGE_REASONING_EFFORT), root, plan["as_of"],
                             retry_failed="judge" in retry_failed, retry_uncertain="judge" in retry_uncertain)
        retries = {"retry_failed": "search" in retry_failed, "retry_uncertain": "search" in retry_uncertain}
        stop = Event()

        def searched(role, repeat):
            # Returns the search to judge, or None when it failed; a failure stops new work.
            if stop.is_set():
                return None
            key = search_key(role["role_id"], repeat)
            result_path = root / "results" / f"{key}.json"
            inputs = {"plan_sha256": envelope["plan_sha256"], "role_id": role["role_id"], "repeat": repeat,
                      "base_url": plan["api_base_url"], "body": role["search_body"]}
            def action(attempt):
                pacer.wait()
                # No provider promises server-side deduplication for this key.
                # Local state, not this key, prevents replays.
                # Metaview checkpoints the asynchronous job under a stable local
                # unit key, so explicit retries poll it instead of creating it again.
                request_key = key if plan["provider"] == "metaview" else digest({"inputs": inputs, "attempt": attempt})
                return searcher.search(role["search_body"], request_key)
            try:
                result = execute_unit(root, "search", key, inputs, action, **retries)
            except Exception:
                failed = {"search_status": "failed_or_uncertain", "judgments": []}
                atomic_json(result_path, {"plan_sha256": envelope["plan_sha256"], "result_sha256": digest(failed), "result": failed})
                progress(f"{key}: search failed or uncertain; inspect private unit state")
                stop.set()
                return None
            coverage = field_coverage_counts(result["candidates"])
            if result["returned_count"] and not coverage["experiences"]:
                # Every profile lacking experience means the response contract
                # drifted; judging would silently score each candidate zero.
                rejected = {"search_status": "profile_contract_rejected", "returned_count": result["returned_count"],
                            "field_coverage_counts": coverage, "judgments": []}
                atomic_json(result_path, {"plan_sha256": envelope["plan_sha256"], "result_sha256": digest(rejected), "result": rejected})
                progress(f"{key}: no returned profile has experience evidence; check profile field normalization")
                stop.set()
                return None
            return role, key, result_path, result, coverage

        def judged(role, key, result_path, result, coverage):
            # A search completed before a failure is left unjudged; resume judges it.
            if stop.is_set():
                return
            judgments = []
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(reviewer.evaluate, role, role["rubric"], candidate): candidate for candidate in result["candidates"]}
                for future in as_completed(futures):
                    candidate = futures[future]
                    try:
                        judgment = future.result()
                    except Exception:
                        judgment = {"status": "error", "label": "judge_error", "relevance": None}
                        # Finish in-flight work, but do not start every queued
                        # paid judgment after a provider/validation failure.
                        for queued in futures:
                            queued.cancel()
                    judgments.append({"rank": candidate["rank"], "candidate_id": candidate["candidate_id"],
                                      "profile_sha256": candidate["profile_sha256"], **judgment})
            judgments.sort(key=lambda item: item["rank"])
            evaluated = {"search_status": "complete", "returned_count": result["returned_count"],
                         "latency_ms": result["elapsed_ms"], "api_latency_ms": result["api_latency_ms"],
                         "backend_latency_ms": result["backend_latency_ms"], "field_coverage_counts": coverage,
                         "judgments": judgments}
            atomic_json(result_path, {"plan_sha256": envelope["plan_sha256"], "result_sha256": digest(evaluated), "result": evaluated})
            progress(f"{key}: {len(judgments)} ranks reviewed; {sum(r['status'] == 'error' for r in judgments)} errors")
            if any(row["status"] == "error" for row in judgments):
                stop.set()

        # Searches never wait for judging: each finished search is handed to a
        # separate judging pool while the search pool moves on to the next role.
        for repeat in range(1, plan["repeats"] + 1):
            if stop.is_set():
                break
            with ThreadPoolExecutor(max_workers=parallel) as searches, ThreadPoolExecutor(max_workers=judge_parallel) as judging:
                pending = [judging.submit(judged, *done.result())
                           for done in as_completed([searches.submit(searched, role, repeat) for role in plan["roles"]])
                           if done.result() is not None]
                for future in pending:
                    future.result()
        report = build_report(root, plan, envelope["plan_sha256"])
        atomic_json(root / "summary.json", report)
        return report
