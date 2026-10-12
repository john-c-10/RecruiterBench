"""Content-addressed execution records with explicit uncertain-call recovery.

Follows the existing Wrangle benchmark's frozen-input, started/uncertain/complete
unit lifecycle, without importing private app code into this standalone package.
"""
from pathlib import Path
import math
from threading import Lock
import time

from .io import atomic_json, digest, now, read_json
from .providers.openai import ProviderError


class ValidationRejected(ValueError):
    """A paid response arrived intact but failed local validation; not uncertain."""


def execute_unit(root: Path, stage: str, key: str, inputs: dict, action, *, retry_failed=False, retry_uncertain=False) -> dict:
    path = root / "units" / stage / f"{digest(key)}.json"
    input_hash = digest(inputs)
    previous = read_json(path) if path.exists() else None
    if previous and previous["input_sha256"] != input_hash:
        raise ValueError("Run inputs changed; create a new run")
    if previous and previous["status"] == "complete":
        if previous["result_sha256"] != digest(previous["result"]):
            raise ValueError("Completed artifact changed")
        return previous["result"]
    if previous and previous["status"] in ("started", "uncertain") and not retry_uncertain:
        raise RuntimeError("Previous call is uncertain; inspect the artifact before explicit retry")
    if previous and previous["status"] == "failed" and not retry_failed:
        raise RuntimeError("Previous call failed; inspect the artifact before explicit retry")
    attempt = previous["attempt"] + 1 if previous else 1
    record = {"stage": stage, "key": key, "input_sha256": input_hash, "inputs": inputs,
              "status": "started", "started_at": now(), "attempt": attempt,
              "previous_attempts": ([*previous.get("previous_attempts", []), {name: value for name, value in previous.items() if name != "previous_attempts"}] if previous else [])}
    atomic_json(path, record)
    start = time.perf_counter()
    try:
        result = action(attempt)
        atomic_json(path, {**record, "status": "complete", "ended_at": now(),
                           "unit_elapsed_ms": (time.perf_counter() - start) * 1000,
                           "result": result, "result_sha256": digest(result)})
        return result
    except Exception as error:
        # A provider rejection is definitively failed only for a non-timeout 4xx.
        # A received response that local validation rejected is also definite.
        # Network failures, 5xx, and malformed paid responses remain uncertain.
        definite = (isinstance(error, ValidationRejected)
                    or isinstance(error, ProviderError) and error.status is not None and 400 <= error.status < 500 and error.status != 408)
        atomic_json(path, {**record, "status": "failed" if definite else "uncertain", "ended_at": now(),
                           "unit_elapsed_ms": (time.perf_counter() - start) * 1000,
                           "error": {"code": error.code if isinstance(error, ProviderError) else "LOCAL_VALIDATION_REJECTED" if isinstance(error, ValidationRejected) else "LOCAL_ERROR",
                                     "status": getattr(error, "status", None)}})
        raise


class Pacer:
    """Pace network admissions; this wait is outside measured request latency."""
    def __init__(self, interval_seconds: float = 300 / 45):
        if not math.isfinite(interval_seconds) or interval_seconds < 0:
            raise ValueError("Pacing interval must be finite and nonnegative")
        self.interval = interval_seconds
        self.next_start = 0.0
        self._guard = Lock()

    def wait(self):
        # Reserve the next admission slot under the lock; sleep outside it.
        with self._guard:
            start = max(self.next_start, time.monotonic())
            self.next_start = start + self.interval
        wait = start - time.monotonic()
        if wait > 0:
            time.sleep(wait)
