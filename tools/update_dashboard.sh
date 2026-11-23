#!/usr/bin/env bash
# Refresh the source files and rebuild the whole dashboard.
# Every stage is resumable: re-running only redoes missing or failed work.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -x "${REPO_ROOT}/.venv/bin/python" ]]; then
  PYTHON="${PYTHON:-${REPO_ROOT}/.venv/bin/python}"
else
  PYTHON="${PYTHON:-python3}"
fi
cd "${REPO_ROOT}"

# A download problem with individual files must not block processing what is already here.
"${PYTHON}" -m ufo_pipeline download || echo "Download finished with errors; continuing with the files on disk."
"${PYTHON}" -m ufo_pipeline process "$@"
