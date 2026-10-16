"""Enrich browser-ranked LinkedIn URLs through the existing Wrangle API.

No Juicebox APIs or browser internals are used. Runs concurrently with
browser_evaluation; stable idempotency keys and local unit records protect paid
requests, and an append-only URL cache is shared across roles.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
import argparse
from functools import lru_cache
import os
from pathlib import Path
import time
from urllib.parse import urlsplit

import certifi
import httpx

from .browser_evaluation import DEFAULT_RUN, load_env, read_collection, validate_browser_plan
from .io import atomic_json, digest, identifier, lock, now, read_json
from .profiles import normalize_profile
from .providers.openai import ProviderError
from .state import Pacer, execute_unit
from .providers.wrangle import scrub_surrogates

ENRICHMENT_PACER = Pacer(16)

ENDPOINT = "https://app.usewrangle.com/api/developers/enrich/linkedin"


def canonical_linkedin(value):
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError("Every browser result requires a LinkedIn profile URL")
    url = urlsplit(value)
    host = (url.hostname or "").lower()
    if url.scheme in ("http", "https") and (host == "linkedin.com" or host.endswith(".linkedin.com")) and url.path.startswith("/search/results/people"):
        return None
    if url.scheme not in ("http", "https") or not (host == "linkedin.com" or host.endswith(".linkedin.com")) or not url.path.lower().startswith("/in/") or not url.path[4:].strip("/"):
        raise ValueError("Invalid LinkedIn profile URL in browser capture")
    return "https://www.linkedin.com" + url.path.rstrip("/").lower()


def cache_path(root, url):
    return root / "enrichment-cache" / f"{digest(url)}.json"


_SOURCE_INDEXES = {}

def source_index(root):
    path = root / "source-cache-index.json"
    if not path.exists():
        return {}
    modified = path.stat().st_mtime_ns
    cached = _SOURCE_INDEXES.get(str(root))
    if cached is None or cached[0] != modified:
        cached = (modified, read_json(path)["urls"])
        _SOURCE_INDEXES[str(root)] = cached
    return cached[1]


@lru_cache(maxsize=4)
def source_unit(path, expected_hash):
    record = read_json(Path(path))
    if record["status"] != "complete" or record["result_sha256"] != expected_hash or digest(record["result"]) != expected_hash:
        raise ValueError("Previously indexed profile source changed")
    return record


def cached_profile(root, url):
    path = cache_path(root, url)
    if not path.exists():
        entry = source_index(root).get(url)
        if entry is None:
            return None
        unit = source_unit(entry["source_unit"], entry["result_sha256"])
        candidate = unit["result"]["candidates"][entry["rank"] - 1]
        if "linkedin:" + urlsplit(url).path not in candidate["identity_keys"] or candidate["profile_sha256"] != entry["profile_sha256"] or digest(candidate["profile"]) != entry["profile_sha256"]:
            raise ValueError("Indexed professional profile changed")
        person = next((key.removeprefix("person:") for key in candidate["identity_keys"] if key.startswith("person:")), None)
        result = {"enrichment_status": "found", "profile": candidate["profile"],
                  "personId": person, "linkedinUrlStable": url,
                  "cache_provenance": {"source": "verified_wrangle_benchmark_cache", **entry}}
        atomic_json(path, {"url": url, "result": result, "result_sha256": digest(result)})
        return result
    record = read_json(path)
    if record["url"] != url or record["result_sha256"] != digest(record["result"]):
        raise ValueError("Enrichment cache changed")
    return record["result"]


def enrich_batch(root, urls, client, *, retry_failed=False, retry_uncertain=False):
    inputs = {"endpoint": ENDPOINT, "linkedinUrls": urls}
    key = digest(inputs)
    def action(attempt):
        for rate_retry in range(6):
            ENRICHMENT_PACER.wait()
            response = client.post(ENDPOINT, json={"linkedinUrls": urls},
                headers={"X-API-Key": os.environ["WRANGLE_API_KEY"], "Idempotency-Key": "recruiterbench-linkedin-" + key})
            if response.status_code != 429 or rate_retry == 5:
                break
            try:
                delay = float(response.headers.get("retry-after", "16"))
            except ValueError:
                delay = 16
            time.sleep(min(60, max(1, delay)))
        if response.status_code not in (200, 404):
            raise ProviderError("LINKEDIN_ENRICHMENT_HTTP_ERROR", response.status_code)
        data = scrub_surrogates(response.json())
        rows = data.get("results")
        if not isinstance(rows, list):
            raise ValueError("Enrichment response lacks per-URL results")
        results = {}
        for row in rows:
            url = canonical_linkedin(row.get("identifier") or row.get("linkedinUrl"))
            if url not in urls or url in results or row.get("status") not in ("found", "not_found"):
                raise ValueError("Enrichment response URL or status contract mismatch")
            profile = row.get("profile")
            if row["status"] == "found" and not isinstance(profile, dict):
                raise ValueError("Enrichment found result has no profile")
            results[url] = {"enrichment_status": row["status"],
                            "profile": normalize_profile(profile) if profile else None,
                            "personId": (profile.get("personId") or profile.get("id")) if profile else None,
                            "linkedinUrlStable": profile.get("linkedinUrlStable") if profile else None}
        if set(results) != set(urls):
            raise ValueError("Enrichment response did not cover every requested URL")
        return {"results": results, "request_id": data.get("requestId"), "credits_used": data.get("creditsUsed"),
                "operation_credits": data.get("operationCredits"), "completed_at": now()}
    result = execute_unit(root, "enrichment", key, inputs, action,
                          retry_failed=retry_failed, retry_uncertain=retry_uncertain)
    for url, profile in result["results"].items():
        existing = cached_profile(root, url) if cache_path(root, url).exists() else None
        if existing is not None and existing != profile:
            if existing.get("cache_provenance"):
                # A collector may already have frozen verified local evidence
                # while this paid batch was in flight. Keep that evidence; the
                # new response remains in the completed paid unit for audit.
                continue
            raise ValueError("Refusing to overwrite different enriched evidence")
        atomic_json(cache_path(root, url), {"url": url, "result": profile, "result_sha256": digest(profile)})
    print(f"Enriched {len(urls)} URLs: {sum(row['enrichment_status']=='found' for row in result['results'].values())} found", flush=True)



def pending_batches(root, urls):
    """Keep URL-to-paid-unit assignments stable even when collectors append pages."""
    directory = root / "enrichment-batches"
    # Migrate any paid work created before manifests were introduced. Never
    # regroup an uncertain or failed request under a new idempotency key.
    for path in (root / "units" / "enrichment").glob("*.json"):
        unit = read_json(path)
        inputs = unit["inputs"]
        key = digest(inputs)
        manifest = directory / f"{key}.json"
        if not manifest.exists():
            atomic_json(manifest, {"inputs": inputs, "batch_sha256": key})
    batches, assignments = [], {}
    for path in sorted(directory.glob("*.json")):
        record = read_json(path)
        inputs = record["inputs"]
        if digest(inputs) != record["batch_sha256"] or inputs["endpoint"] != ENDPOINT:
            raise ValueError("Enrichment batch manifest changed")
        batch = inputs["linkedinUrls"]
        if not 1 <= len(batch) <= 50 or len(batch) != len(set(batch)):
            raise ValueError("Invalid enrichment batch manifest")
        for url in batch:
            if canonical_linkedin(url) != url or url in assignments:
                raise ValueError("Enrichment URL assigned to multiple paid batches")
            assignments[url] = record["batch_sha256"]
        batches.append(batch)
    fresh = list(dict.fromkeys(url for url in urls if url not in assignments))
    for index in range(0, len(fresh), 50):
        batch = fresh[index:index+50]
        inputs = {"endpoint": ENDPOINT, "linkedinUrls": batch}
        key = digest(inputs)
        atomic_json(directory / f"{key}.json", {"inputs": inputs, "batch_sha256": key})
        batches.append(batch)
    return [batch for batch in batches if any(cached_profile(root, url) is None for url in batch)]


def publish_pages(root, role, collection):
    """Publish the first 200 found profiles while preserving raw selection lineage."""
    selected, found, missing, invalid, pending = [], 0, 0, 0, 0
    prefix_ready = True
    for source_rank, row in enumerate(collection["raw_candidates"], 1):
        url = canonical_linkedin(row.get("linkedin_url"))
        if url is None:
            invalid += 1
            continue
        enriched = cached_profile(root, url)
        if enriched is None:
            pending += 1
            prefix_ready = False
            continue
        if enriched["enrichment_status"] != "found":
            missing += 1
            continue
        found += 1
        if prefix_ready and len(selected) < 200:
            selected.append({**row, **enriched["profile"], "source_rank": source_rank,
                "enrichment_status": "found",
                "enrichment_provenance": enriched.get("cache_provenance", {"source": ENDPOINT}),
                **({"personId": "wrangle:" + str(enriched["personId"])} if enriched["personId"] else {}),
                **({"linkedinUrlStable": enriched["linkedinUrlStable"]} if enriched["linkedinUrlStable"] else {})})
    exhausted = collection["collection_complete"]
    complete = len(selected) == 200 or (exhausted and pending == 0)
    progress = {"role_id": role["role_id"], "raw_count": len(collection["raw_candidates"]),
                "scanned_count": found + missing + invalid, "found_count": found,
                "pending_count": pending, "selected_count": len(selected),
                "skipped_invalid_count": invalid, "skipped_not_found_count": missing,
                "needs_more": not complete and not exhausted and found + pending < 200,
                "complete": complete, "exhausted": exhausted, "updated_at": now()}
    atomic_json(root / "enrichment-progress" / f"{identifier(role['role_id'])}.json", progress)
    directory = root / "browser" / identifier(role["role_id"])
    paths = sorted(directory.glob("page-*.json"))
    existing_pages = [read_json(path) for path in paths]
    existing = [row for page in existing_pages for row in page["candidates"]]
    if selected[:len(existing)] != existing:
        raise ValueError("Previously selected enriched profile evidence or source order changed")
    if existing_pages and existing_pages[-1]["complete"]:
        return progress
    remaining = selected[len(existing):]
    chunks = [remaining[n:n+25] for n in range(0, len(remaining), 25)]
    if complete and not chunks:
        chunks = [[]]
    for index, chunk in enumerate(chunks):
        final = complete and index == len(chunks) - 1
        number = len(paths) + index + 1
        page = {"role_id": role["role_id"], "page": number,
                "query": collection["pages"][0]["query"],
                "query_started_at": collection["pages"][0]["query_started_at"],
                "source_url": collection["pages"][-1]["source_url"],
                "collected_at": collection["pages"][-1]["collected_at"],
                "candidates": chunk, "complete": final,
                "enrichment_provider": "wrangle", "enriched_at": now(), "timing_valid": False,
                "selection_policy": "first_200_successfully_enriched_in_source_order"}
        if final:
            page["termination_reason"] = "target_reached" if len(selected) == 200 else "results_exhausted"
        atomic_json(directory / f"page-{number:03d}.json", page)
    return progress


def file_versions(paths):
    """Cheap invalidation for atomically written inputs; hash checks stay in readers."""
    versions = []
    for path in sorted(set(paths)):
        try:
            info = path.stat()
            version = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        except FileNotFoundError:
            version = None
        versions.append((path, version))
    return tuple(versions)


class WatcherPasses:
    """Reuse only settled, unchanged inputs within this exclusive worker process."""
    def __init__(self, root):
        self.root, self.settled, self.batch_versions = root, {}, None

    def capture_versions(self, role_id):
        return file_versions((self.root / "browser-links" / identifier(role_id)).glob("page-*.json"))

    def output_versions(self, role_id):
        return file_versions([*(self.root / "browser" / identifier(role_id)).glob("page-*.json"),
                              self.root / "enrichment-progress" / f"{identifier(role_id)}.json"])

    def unchanged(self, role_id):
        previous = self.settled.get(role_id)
        return (previous is not None and previous["captures"] == self.capture_versions(role_id)
                and previous["profiles"] == file_versions(path for path, _ in previous["profiles"])
                and previous["outputs"] == self.output_versions(role_id))

    def publish(self, role, collection, captures):
        role_id = role["role_id"]
        if self.unchanged(role_id):
            return
        self.settled.pop(role_id, None)
        profile_paths = [cache_path(self.root, url) for row in collection["raw_candidates"]
                         if (url := canonical_linkedin(row.get("linkedin_url")))]
        before = file_versions(profile_paths)
        progress = publish_pages(self.root, role, collection)
        # Pending profiles must be reconsidered, even if their capture is unchanged.
        # Do not memoize inputs that changed while they were being validated.
        if (progress["pending_count"] == 0 and captures == self.capture_versions(role_id)
                and before == file_versions(profile_paths)):
            self.settled[role_id] = {"captures": captures, "profiles": before,
                                    "outputs": self.output_versions(role_id)}

    def scan(self, roles):
        collections, missing, seen = [], [], set()
        for role in roles:
            role_id = role["role_id"]
            if self.unchanged(role_id):
                continue
            self.settled.pop(role_id, None)
            captures = self.capture_versions(role_id)
            collection = read_collection(self.root, role, source_directory="browser-links", require_experience=False)
            if collection is None:
                continue
            collections.append((role, collection, captures))
            for row in collection["raw_candidates"]:
                url = canonical_linkedin(row.get("linkedin_url"))
                if url and url not in seen and cached_profile(self.root, url) is None:
                    missing.append(url)
                    seen.add(url)
            self.publish(role, collection, captures)
        return collections, missing

    def catalog_versions(self):
        return file_versions([*(self.root / "units" / "enrichment").glob("*.json"),
                              *(self.root / "enrichment-batches").glob("*.json")])

    def batches(self, missing):
        versions = self.catalog_versions()
        if not missing and self.batch_versions is not None and versions == self.batch_versions:
            return []
        batches = pending_batches(self.root, missing)
        self.batch_versions = None if batches else self.catalog_versions()
        return batches

    def batches_finished(self):
        # Called only after all scheduled units finished successfully. A failed or
        # uncertain unit still raises through execute_unit, with the same paid inputs.
        self.batch_versions = self.catalog_versions()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_RUN)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--record-compatible-engine-update", action="store_true")
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--retry-uncertain", action="store_true")
    args = parser.parse_args(argv)
    if not 1 <= args.workers <= 8:
        parser.error("workers must be 1..8")
    load_env(Path(".env"))
    if not os.environ.get("WRANGLE_API_KEY"):
        raise ValueError("Existing WRANGLE_API_KEY is required for LinkedIn enrichment")
    envelope = read_json(args.root / "plan.json")
    plan = validate_browser_plan(envelope, allow_engine_revision=args.record_compatible_engine_update)
    if args.record_compatible_engine_update:
        from .runner import engine_hash
        current = engine_hash()
        atomic_json(args.root / "engine-revisions" / f"{current}.json", {"plan_sha256": envelope["plan_sha256"],
            "original_engine_sha256": plan["engine_sha256"], "current_engine_sha256": current, "recorded_at": now(),
            "reason": "Routine orchestration reliability fix; regenerated frozen corpus, query, rubric, scoring and evidence-selection policy are exactly unchanged."})
    with lock(args.root / "enrichment-worker"), httpx.Client(verify=certifi.where(), timeout=180, follow_redirects=False) as client:
        print(f"Ready: {args.workers} enrichment workers, batches up to 50", flush=True)
        passes = WatcherPasses(args.root)
        while True:
            collections, missing = passes.scan(plan["roles"])
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                futures = [pool.submit(enrich_batch, args.root, batch, client,
                    retry_failed=args.retry_failed, retry_uncertain=args.retry_uncertain) for batch in passes.batches(missing)]
                try:
                    for future in as_completed(futures):
                        future.result()
                        for role, collection, captures in collections:
                            passes.publish(role, collection, captures)
                except Exception:
                    for future in futures:
                        future.cancel()
                    raise
            passes.batches_finished()
            if not args.watch or (args.root / "STOP").exists():
                return
            time.sleep(5)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as error:
        print(f"Enrichment stopped: {type(error).__name__}: {str(error) if isinstance(error, (ValueError, RuntimeError)) else 'inspect private unit artifacts'}", flush=True)
        raise SystemExit(2)
