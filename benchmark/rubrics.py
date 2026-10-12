"""Distill frozen jobs once; retain evidence, lineage, and resumable call state."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import os

from .io import atomic_json, digest, lock, now, read_json
from .models import MODEL, REASONING_EFFORT, load_roles, prompt, role_source, schema, validate_rubric
from .providers.openai import OpenAIJudge, ProviderError


def extract(dataset: Path, private_root: Path, *, limit: int | None = None, workers: int = 6,
            retry_failed: bool = False, retry_uncertain: bool = False, role_ids: set[str] | None = None) -> dict:
    roles = load_roles(dataset)
    if role_ids:
        unknown = role_ids - {role["role_id"] for role in roles}
        if unknown:
            raise ValueError("Unknown role selection")
        roles = [role for role in roles if role["role_id"] in role_ids]
    if limit:
        roles = roles[:limit]
    if not 1 <= workers <= 12:
        raise ValueError("Extraction workers must be between 1 and 12")
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ValueError("Extraction limit must be positive")
    if any(not (dataset / role["rubric_path"]).exists() for role in roles) and not os.environ.get("OPENAI_API_KEY"):
        raise ValueError("Set OPENAI_API_KEY before extracting new rubrics")
    policy, output_schema = prompt("rubric"), schema("rubric")
    client = OpenAIJudge()
    state_root = private_root / "extractions" / dataset.name

    def one(role):
        snapshot = read_json(dataset / role["snapshot_path"])
        inputs = {"snapshot_sha256": role["snapshot_sha256"], "prompt_sha256": digest(policy),
                  "schema_sha256": digest(output_schema), "model": MODEL, "reasoning_effort": REASONING_EFFORT}
        input_hash = digest(inputs)
        target = dataset / role["rubric_path"]
        if target.exists():
            saved = read_json(target)
            # Frozen rubrics keep the model that produced them; only the job,
            # prompt, schema, and effort must still match.
            recorded = {key: saved["extraction"].get(key) for key in inputs}
            if (saved["input_sha256"] != digest(recorded) or saved["rubric_sha256"] != digest(saved["rubric"])
                    or any(recorded[key] != inputs[key] for key in inputs if key != "model")):
                raise ValueError("Rubric or extraction inputs changed; use a new dataset version")
            validate_rubric(saved["rubric"], snapshot)
            return {"role_id": role["role_id"], "status": "cached", "model": recorded["model"]}
        state_path = state_root / f"{role['role_id']}.json"
        previous = read_json(state_path) if state_path.exists() else None
        if previous and previous["input_sha256"] != input_hash:
            raise ValueError("Extraction lineage changed")
        if previous and previous["status"] in ("started", "uncertain") and not retry_uncertain:
            raise ValueError("Uncertain extraction: inspect private state before --retry-uncertain")
        if previous and previous["status"] == "failed" and not retry_failed:
            raise ValueError("Failed extraction: inspect private state before --retry-failed")
        record = {"role_id": role["role_id"], "input_sha256": input_hash, "status": "started", "started_at": now(),
                  "attempt": (previous.get("attempt", 0) if previous else 0) + 1,
                  "previous_attempts": ([*previous.get("previous_attempts", []), {key: value for key, value in previous.items() if key != "previous_attempts"}] if previous else [])}
        atomic_json(state_path, record)
        responses = []
        payload = {"as_of": snapshot["source"]["verified_at"][:10], "job": role_source(snapshot)}
        try:
            for attempt in range(2):
                result = client.complete(system=policy, payload=payload, schema=output_schema, name="job_rubric")
                responses.append(result)
                try:
                    rubric = validate_rubric(result["value"], snapshot)
                    if len(rubric["search_query"]) > 12000 or not rubric["search_query"].strip() or not rubric["role_summary"].strip():
                        raise ValueError("Invalid search query or role summary")
                    break
                except ValueError:
                    if attempt == 1:
                        raise
                    payload = {**payload, "previous_draft": result["value"],
                               "repair_request": "The previous draft failed exact source-quotation/ID validation. Quote contiguous original text from one original field, without ellipses, added labels, changed words, or invented punctuation. Use the original job as evidence, never the draft. Return the complete corrected rubric with sequential IDs."}
            saved = {"schema_version": "1.0", "role_id": role["role_id"], "snapshot_sha256": role["snapshot_sha256"],
                     "input_sha256": input_hash, "rubric_sha256": digest(rubric), "rubric": rubric,
                     "extraction": {**inputs, "returned_model": result["model"], "generated_at": now(),
                                    "response_id": result["response_id"], "usage": result["usage"],
                                    "attempt_count": len(responses), "review_status": "machine_extracted_source_quotes_validated"}}
            atomic_json(target, saved)
            atomic_json(state_path, {**record, "status": "complete", "completed_at": now(), "responses": responses})
            return {"role_id": role["role_id"], "status": "complete", "ambiguities": len(rubric["ambiguities"])}
        except Exception as error:
            uncertain = isinstance(error, ProviderError) and error.code == "OPENAI_REQUEST_UNCERTAIN"
            code = error.code if isinstance(error, ProviderError) else "RUBRIC_VALIDATION_REJECTED"
            atomic_json(state_path, {**record, "status": "uncertain" if uncertain else "failed", "completed_at": now(),
                                     "responses": responses, "error": {"code": code, "status": getattr(error, "status", None)}})
            return {"role_id": role["role_id"], "status": "failed", "error": code}

    results = []
    with lock(state_root):
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(one, role) for role in roles]
            for future in as_completed(futures):
                result = future.result()
                results.append(result)
                print(f"rubrics {len(results)}/{len(roles)} {result['role_id']} {result['status']}", flush=True)
    report = {"dataset": dataset.name, "selected": len(roles), "completed": sum(row["status"] in ("complete", "cached") for row in results),
              "failed": [row for row in results if row["status"] == "failed"], "results": results}
    atomic_json(state_root / "summary.json", report)
    return report
