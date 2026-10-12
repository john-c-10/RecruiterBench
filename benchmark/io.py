"""Canonical hashing, private atomic artifacts, and exclusive run ownership."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import tempfile


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def read_json(path: Path) -> object:
    return json.loads(path.read_text())


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def atomic_json(path: Path, value: object) -> None:
    atomic_text(path, json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n")


def atomic_jsonl(path: Path, rows: list[dict]) -> None:
    atomic_text(path, "".join(canonical(row) + "\n" for row in rows))


def atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as output:
            output.write(value)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,119}", value):
        raise ValueError("Invalid artifact identifier")
    return value


@contextmanager
def lock(root: Path):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / ".lock"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise RuntimeError(f"Run is locked: {path}. Inspect the recorded process before removing a stale lock.") from None
    try:
        with os.fdopen(fd, "w") as output:
            json.dump({"pid": os.getpid(), "host": socket.gethostname(), "created_at": now()}, output)
        yield
    finally:
        path.unlink(missing_ok=True)


def text_leaves(value: object):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from text_leaves(child)
    elif isinstance(value, list):
        for child in value:
            yield from text_leaves(child)


def normalized(value: str) -> str:
    return " ".join(value.split())


def grounded(quote: str, source: object) -> bool:
    quote = normalized(quote)
    return bool(quote) and any(quote in normalized(field) for field in text_leaves(source))
