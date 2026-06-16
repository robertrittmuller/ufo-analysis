#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

if [[ -x "${REPO_ROOT}/.venv/bin/python" ]]; then
  PYTHON="${PYTHON:-${REPO_ROOT}/.venv/bin/python}"
else
  PYTHON="${PYTHON:-python3}"
fi

REVIEW_TIMEOUT="${REVIEW_TIMEOUT:-900}"
REVIEW_BASE_URL="${REVIEW_BASE_URL:-http://localhost:4321/v1}"
REVIEW_MODEL="${REVIEW_MODEL:-Qwen3.6-35B-A3B-UD-MLX-4bit}"
EMBEDDING_BASE_URL="${EMBEDDING_BASE_URL:-http://localhost:4321/v1}"
EMBEDDING_MODEL="${EMBEDDING_MODEL:-Qwen3-Embedding-0.6B-4bit-DWQ}"

cd "${REPO_ROOT}"

echo "Refreshing UFO/UAP source data..."
"${PYTHON}" tools/download_ufo_reports.py

echo "Rebuilding dashboard in hybrid mode with review timeout ${REVIEW_TIMEOUT}s..."
"${PYTHON}" tools/build_dashboard.py \
  --review-mode hybrid \
  --review-timeout "${REVIEW_TIMEOUT}" \
  --review-base-url "${REVIEW_BASE_URL}" \
  --review-model "${REVIEW_MODEL}" \
  --embedding-base-url "${EMBEDDING_BASE_URL}" \
  --embedding-model "${EMBEDDING_MODEL}"

echo "Dashboard update complete."
