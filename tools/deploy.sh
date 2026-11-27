#!/usr/bin/env bash
# Publish everything to Cloudflare in one step: the dashboard, the MCP server's database and the MCP server.
# Run it after tools/update_dashboard.sh (or any full build).
#
#   tools/deploy.sh               deploy
#   tools/deploy.sh --dry-run     run every check and build both Workers, but publish nothing and load no data
#   tools/deploy.sh --force-data  reload the database even when the data has not changed since the last load
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -x "${REPO_ROOT}/.venv/bin/python" ]]; then
  PYTHON="${PYTHON:-${REPO_ROOT}/.venv/bin/python}"
else
  PYTHON="${PYTHON:-python3}"
fi
WRANGLER=(npx --yes wrangler@4)
DATABASE="ufo-analysis"
SQL="${REPO_ROOT}/data/mcp/d1.sql"
LOADED="${REPO_ROOT}/data/mcp/d1.loaded.sha256"   # hash of the data file last loaded into the remote database

DRY_RUN=0
FORCE_DATA=0
for arg in "$@"; do
  case "${arg}" in
    --dry-run) DRY_RUN=1 ;;
    --force-data) FORCE_DATA=1 ;;
    *) echo "Unknown option: ${arg} (expected --dry-run or --force-data)" >&2; exit 2 ;;
  esac
done

step() { printf '\n===== %s =====\n' "$1"; }
fail() { echo "ERROR: $1" >&2; exit 1; }

cd "${REPO_ROOT}"

step "Checks"
[[ -f dashboard/data/index.json ]] || fail "dashboard/data/index.json is missing. Run tools/update_dashboard.sh first."
"${PYTHON}" - <<'PY' || fail "dashboard/ holds a partial build. Run a full build before deploying."
import json, sys
sys.exit(1 if json.load(open("dashboard/data/index.json")).get("partial") else 0)
PY
echo "Site data and configuration look complete."

# Always rewritten from the built site, so the dashboard and the MCP server are published from the same data.
step "Database file"
"${PYTHON}" -m ufo_pipeline export-d1
HASH="$("${PYTHON}" -c 'import hashlib, sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' "${SQL}")"
NEEDS_LOAD=1
if [[ "${FORCE_DATA}" -eq 0 && -f "${LOADED}" && "$(cat "${LOADED}")" == "${HASH}" ]]; then
  NEEDS_LOAD=0
fi

if [[ "${DRY_RUN}" -eq 1 ]]; then
  step "Dashboard (dry run)"
  "${WRANGLER[@]}" deploy --dry-run
  step "MCP server (dry run)"
  (cd mcp && "${WRANGLER[@]}" deploy --dry-run)
  step "Dry run finished"
  if [[ "${NEEDS_LOAD}" -eq 1 ]]; then
    echo "A real run would reload the MCP database (the data has changed since the last load, or was never loaded)."
  else
    echo "A real run would leave the MCP database alone (the data has not changed since the last load)."
  fi
  echo "Nothing was published."
  exit 0
fi

step "1/3 Dashboard"
"${WRANGLER[@]}" deploy

step "2/3 MCP database"
if [[ "${NEEDS_LOAD}" -eq 1 ]]; then
  echo "Loading ${SQL} into the remote database. Tool calls fail while this runs."
  if ! (cd mcp && "${WRANGLER[@]}" d1 execute "${DATABASE}" --remote --yes --file "${SQL}"); then
    rm -f "${LOADED}"
    fail "The database load did not finish. The MCP server may answer with errors until this script is run again."
  fi
  echo "${HASH}" > "${LOADED}"
else
  echo "Data unchanged since the last load; skipping (use --force-data to reload anyway)."
fi

step "3/3 MCP server"
(cd mcp && "${WRANGLER[@]}" deploy)

step "Deployed"
echo "Dashboard, MCP database and MCP server are published."
