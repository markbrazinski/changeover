#!/usr/bin/env bash
# PHASE 1 ACCEPTANCE -- the single entrypoint.
#
#   ./scripts/phase1_acceptance.sh                  # every available channel
#   ./scripts/phase1_acceptance.sh tears_of_steel   # one channel
#
# Stands up the rig, runs the full behaviour set on every channel against its own real
# film and its own distinct real backup, verifies both sponsors at runtime, and writes the
# machine-readable acceptance artifact:
#
#   logs/acceptance_nwide.json   5 behaviours x N channels, real measured numbers
#
# Reproducible from a clean clone once films are supplied (they are never committed --
# see "Adding a film (drop-in spec)" in README.md).
#
# Phase 1 proves N-INSTANCING ONLY. There is no supervisor and no contention handling;
# that is Phase 2 and is deliberately absent here.
set -uo pipefail
cd "$(dirname "$0")/.."

source .venv/bin/activate
set -a; source .env; set +a
export GRAFANA_MCP_URL="${GRAFANA_MCP_URL:-http://localhost:8001/mcp}"

# macOS ships bash 3.2 -- no mapfile/readarray.
if [ $# -gt 0 ]; then
  CHANNELS="$*"
else
  CHANNELS=$(python -c "
import sys; sys.path.insert(0,'config')
import channels; print(' '.join(channels.available_channels()))")
fi

if [ -z "${CHANNELS// }" ]; then
  echo "ERROR: no channels have their files present." >&2
  echo "Supply films per README.md 'Adding a film (drop-in spec)', then re-run." >&2
  exit 1
fi

echo "channels under test: ${CHANNELS}"

echo
echo "########## STAGE 1/4: rig ##########"
./scripts/up.sh
python scripts/test-only/wipe_test_fixtures.py || true

echo
echo "########## STAGE 2/4: derive ceilings from each channel's real baseline ##########"
# Ceilings are DERIVED, never hand-set. Re-derived here so the artifact's ceilings always
# match the films actually present rather than a stale config.
# shellcheck disable=SC2086
python scripts/derive_ceilings.py ${CHANNELS}

echo
echo "########## STAGE 3/4: behaviour set, per channel ##########"
# shellcheck disable=SC2086
./scripts/run_nwide_acceptance.sh ${CHANNELS}

echo
echo "########## STAGE 4/4: sponsor runtime + acceptance artifact ##########"
python scripts/verify_sponsor_runtime.py
SPONSOR_RC=$?

python scripts/compile_acceptance_nwide.py
TABLE_RC=$?

echo
if [ ${SPONSOR_RC} -eq 0 ] && [ ${TABLE_RC} -eq 0 ]; then
  echo "PHASE 1 ACCEPTANCE: PASS"
else
  echo "PHASE 1 ACCEPTANCE: FAIL (sponsor_rc=${SPONSOR_RC} table_rc=${TABLE_RC})"
fi
exit $(( SPONSOR_RC | TABLE_RC ))
