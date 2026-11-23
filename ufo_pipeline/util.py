"""Small shared helpers: logging, atomic writes, hashing, text normalization."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any, Optional

_LOG_LOCK = threading.Lock()


def log(message: str) -> None:
    with _LOG_LOCK:
        print(f"[{time.strftime('%H:%M:%S')}] {message}", file=sys.stderr, flush=True)


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write through a temp file and rename so readers never see a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def atomic_write_json(path: Path, payload: Any, indent: Optional[int] = None) -> None:
    separators = None if indent else (",", ":")
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=indent, separators=separators))


def read_json(path: Path, default: Any = None) -> Any:
    """Read JSON; a missing file returns default, a corrupt one is reported and returns default."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as exc:
        log(f"WARNING: could not read {path}: {exc}")
        return default


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def stable_hash(payload: Any, length: int = 16) -> str:
    return sha256_text(json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str))[:length]


class FileHasher:
    """Content hashes for source files, memoized by (size, mtime) so 26 GB is hashed once."""

    def __init__(self, index_path: Path) -> None:
        self.index_path = index_path
        self.index = read_json(index_path, {}) or {}
        self._lock = threading.Lock()
        self._dirty = False

    def sha256(self, path: Path) -> str:
        stat = path.stat()
        key = path.name
        with self._lock:
            entry = self.index.get(key)
            if entry and entry.get("size") == stat.st_size and entry.get("mtime") == int(stat.st_mtime):
                return entry["sha256"]
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
                digest.update(block)
        value = digest.hexdigest()
        with self._lock:
            self.index[key] = {"size": stat.st_size, "mtime": int(stat.st_mtime), "sha256": value}
            self._dirty = True
        return value

    def save(self) -> None:
        with self._lock:
            if self._dirty:
                atomic_write_json(self.index_path, self.index, indent=1)
                self._dirty = False


def slugify(value: str, max_length: int = 96) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-").lower()
    return value[:max_length].strip("-") or "item"


def normalize_space(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_for_match(text: str) -> str:
    """Lower-case, strip punctuation and collapse whitespace for fuzzy quote matching."""
    text = unicodedata.normalize("NFKD", text or "").lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def as_list(value: Any) -> list:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    return [value]


def clean_str(value: Any, limit: int = 0) -> str:
    """Model output to a display string; treats null-ish placeholders as empty."""
    if value is None or isinstance(value, (dict, list)):
        return ""
    text = normalize_space(value)
    if text.lower() in {"null", "none", "n/a", "na", "unknown", "not stated", "not specified", "unspecified", "-"}:
        return ""
    return text[:limit] if limit else text


def str_list(value: Any, limit: int = 40) -> list:
    out = []
    seen = set()
    for item in as_list(value):
        text = clean_str(item)
        if text and text.lower() not in seen:
            seen.add(text.lower())
            out.append(text)
    return out[:limit]
