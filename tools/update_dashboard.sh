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
# Let the Python entry point load .env; forward only explicit legacy overrides.
MODEL_ARGS=()
if [[ -n "${REVIEW_BASE_URL:-}" ]]; then
  MODEL_ARGS+=(--review-base-url "${REVIEW_BASE_URL}")
fi
if [[ -n "${REVIEW_MODEL:-}" ]]; then
  MODEL_ARGS+=(--review-model "${REVIEW_MODEL}")
fi
if [[ -n "${EMBEDDING_BASE_URL:-}" ]]; then
  MODEL_ARGS+=(--embedding-base-url "${EMBEDDING_BASE_URL}")
fi
if [[ -n "${EMBEDDING_MODEL:-}" ]]; then
  MODEL_ARGS+=(--embedding-model "${EMBEDDING_MODEL}")
fi

cd "${REPO_ROOT}"

echo "Refreshing UFO/UAP source data..."
"${PYTHON}" tools/download_ufo_reports.py

echo "Rebuilding dashboard in hybrid mode with review timeout ${REVIEW_TIMEOUT}s..."
"${PYTHON}" tools/build_dashboard.py \
  --review-mode hybrid \
  --review-timeout "${REVIEW_TIMEOUT}" \
  ${MODEL_ARGS[@]+"${MODEL_ARGS[@]}"}

echo "Rebuilding the scientific evidence review..."
"${PYTHON}" tools/build_scientific_analysis.py

echo "Dashboard update complete."
