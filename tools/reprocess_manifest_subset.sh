#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

if [[ -x "${REPO_ROOT}/.venv/bin/python" ]]; then
  PYTHON="${PYTHON:-${REPO_ROOT}/.venv/bin/python}"
else
  PYTHON="${PYTHON:-python3}"
fi

MANIFEST_MATCH="${MANIFEST_MATCH:-061226/release_03}"
REVIEW_TIMEOUT="${REVIEW_TIMEOUT:-900}"
REVIEW_BASE_URL="${REVIEW_BASE_URL:-http://localhost:4321/v1}"
REVIEW_MODEL="${REVIEW_MODEL:-Qwen3.6-35B-A3B-UD-MLX-4bit}"
EMBEDDING_BASE_URL="${EMBEDDING_BASE_URL:-http://localhost:4321/v1}"
EMBEDDING_MODEL="${EMBEDDING_MODEL:-Qwen3-Embedding-0.6B-4bit-DWQ}"
DRY_RUN="${DRY_RUN:-0}"
SKIP_BUILD="${SKIP_BUILD:-0}"

cd "${REPO_ROOT}"

echo "Invalidating caches for manifest entries matching: ${MANIFEST_MATCH}"
MANIFEST_MATCH="${MANIFEST_MATCH}" DRY_RUN="${DRY_RUN}" "${PYTHON}" - <<'PY'
import json
import os
from pathlib import Path

match = os.environ["MANIFEST_MATCH"]
dry_run = os.environ.get("DRY_RUN") == "1"

repo_root = Path.cwd()
manifest_path = repo_root / "data" / "processed" / "source_manifest.json"
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
documents = manifest.get("documents")
if not isinstance(documents, list):
    raise SystemExit(f"Manifest does not contain a documents list: {manifest_path}")

matched = []
for entry in documents:
    if not isinstance(entry, dict):
        continue
    haystack = "\n".join(str(value) for value in entry.values() if value is not None)
    filename = entry.get("filename")
    if isinstance(filename, str) and match in haystack:
        matched.append(filename)

matched = sorted(dict.fromkeys(matched))
if not matched:
    raise SystemExit(f"No manifest entries matched {match!r}")

cache_paths = []
embedding_keys = set()
for filename in matched:
    stem = Path(filename).stem
    embedding_keys.add(filename)
    embedding_keys.add(stem)
    cache_paths.extend(
        [
            repo_root / "data" / "processed" / "documents" / f"{stem}.json",
            repo_root / "data" / "processed" / "page_text" / f"{stem}.json",
            repo_root / "data" / "ocr_markdown" / f"{stem}.md",
            repo_root / "data" / "processed" / "transcripts" / f"{stem}.txt",
            repo_root / "data" / "processed" / "transcripts" / f"{stem}.json",
        ]
    )

removed_paths = []
for path in cache_paths:
    if path.exists():
        removed_paths.append(path)
        if not dry_run:
            path.unlink()

embedding_cache = repo_root / "data" / "processed" / "source_embeddings.json"
removed_embeddings = []
if embedding_cache.exists():
    payload = json.loads(embedding_cache.read_text(encoding="utf-8"))
    items = payload.get("items")
    if isinstance(items, dict):
        for key in list(items):
            if key in embedding_keys or any(key.startswith(f"{stem}#") for stem in embedding_keys):
                removed_embeddings.append(key)
                if not dry_run:
                    del items[key]
        if removed_embeddings and not dry_run:
            embedding_cache.write_text(json.dumps(payload, indent=2), encoding="utf-8")

print(f"Matched {len(matched)} manifest file(s).")
for filename in matched:
    print(f"  {filename}")
print(f"{'Would remove' if dry_run else 'Removed'} {len(removed_paths)} cache file(s).")
print(f"{'Would remove' if dry_run else 'Removed'} {len(removed_embeddings)} embedding cache entrie(s).")
PY

if [[ "${DRY_RUN}" == "1" ]]; then
  echo "Dry run complete; no caches were changed and the dashboard was not rebuilt."
  exit 0
fi

if [[ "${SKIP_BUILD}" == "1" ]]; then
  echo "Cache invalidation complete; skipping dashboard rebuild because SKIP_BUILD=1."
  exit 0
fi

echo "Rebuilding dashboard in hybrid mode with localhost review and embedding models..."
"${PYTHON}" tools/build_dashboard.py \
  --review-mode hybrid \
  --review-timeout "${REVIEW_TIMEOUT}" \
  --review-base-url "${REVIEW_BASE_URL}" \
  --review-model "${REVIEW_MODEL}" \
  --embedding-base-url "${EMBEDDING_BASE_URL}" \
  --embedding-model "${EMBEDDING_MODEL}"

echo "Subset reprocess complete."
