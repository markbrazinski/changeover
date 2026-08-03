#!/usr/bin/env bash
# One-command acceptance pass-list for the assembled Changeover agent.
#
#   ./scripts/run_acceptance.sh
#
# Runs every demo beat against the real rig and writes a machine-readable pass/fail table
# with the REAL measured numbers to logs/acceptance_table.json.
#
# Each beat needs a different real telemetry situation (which exporters are alive, which
# are faulted, whether the backup is confirmable), so the script sequences those states
# for real rather than mocking them. It takes several minutes -- the waits are Prometheus
# scrape + Grafana Cloud remote_write propagation, which cannot be short-circuited.
set -uo pipefail
cd "$(dirname "$0")/.."

source .venv/bin/activate
set -a; source .env; set +a
export GRAFANA_MCP_URL="${GRAFANA_MCP_URL:-http://localhost:8001/mcp}"

PG="http://localhost:9091"
PROPAGATE=32

cleanup() {
  pkill -f "caption_cue_with_telemetry" 2>/dev/null || true
  pkill -f "feed_liveness_with_telemetry" 2>/dev/null || true
  pkill -f "backup_captions_health" 2>/dev/null || true
}
trap cleanup EXIT

reset_rig() {
  cleanup; sleep 1
  for g in media_pipeline_captions/mode/frozen_captions \
           media_pipeline_captions/mode/baseline \
           media_pipeline_feed_liveness/mode/frozen \
           media_pipeline_feed_liveness/mode/baseline \
           backup_captions; do
    curl -s -X DELETE "${PG}/metrics/job/${g}" > /dev/null || true
  done
}

echo "=== [0/7] rig up ==="
./scripts/up.sh
python scripts/test-only/wipe_test_fixtures.py || true

# --- Beat 1: won't guess (MCP down) -------------------------------------------------
# Needs no telemetry at all: the gate must refuse before the model is ever invoked.
echo
echo "=== [1/7] won't guess -- Grafana MCP unreachable ==="
python agent/assembled_agent.py --scenario acc_wont_guess \
  --mcp-url "http://localhost:9999/mcp" 2>&1 | grep -E "GATE\]|FINAL" || true

# --- Beat 2: captions fault -> names CAPTIONS, swaps, verifies by measurement --------
echo
echo "=== [2/7] happy path + verify-by-measurement (captions faulted, sign healthy) ==="
reset_rig
nohup python scripts/caption_cue_with_telemetry.py hold_open 260 > logs/hold_open.log 2>&1 &
nohup python scripts/feed_liveness_with_telemetry.py baseline > logs/liveness_baseline.log 2>&1 &
nohup python scripts/backup_captions_health.py watch 320 > logs/backup_health.log 2>&1 &
sleep ${PROPAGATE}
# The captions feed genuinely recovers onto the backup partway through verification, so
# the restored state is earned by a real reading rather than asserted on the swap.
( sleep 45; pkill -f "caption_cue_with_telemetry.py hold_open" 2>/dev/null; sleep 1; \
  nohup python scripts/caption_cue_with_telemetry.py recover 220 > logs/cap_recover.log 2>&1 & ) >/dev/null 2>&1 &
python agent/assembled_agent.py --scenario acc_captions_fault --approve --verify 2>&1 \
  | grep -E "GATE\]|SCOPE\]|VERIFY|verify\]|FINAL" || true

# --- Beat 3: sign fault -> names SIGN_LANGUAGE (discrimination) ----------------------
echo
echo "=== [3/7] discrimination (sign faulted, captions healthy) ==="
reset_rig
nohup python scripts/caption_cue_with_telemetry.py hold_healthy 260 > logs/cap_healthy.log 2>&1 &
nohup python scripts/feed_liveness_with_telemetry.py frozen 260 > logs/liveness_hold.log 2>&1 &
nohup python scripts/backup_captions_health.py watch 320 > logs/backup_health.log 2>&1 &
sleep ${PROPAGATE}
python agent/assembled_agent.py --scenario acc_sign_fault --approve 2>&1 \
  | grep -E "GATE\]|SCOPE\]|FINAL" || true

# --- Beat 4: won't switch (backup unconfirmable) ------------------------------------
echo
echo "=== [4/7] won't switch -- backup telemetry absent ==="
reset_rig
nohup python scripts/caption_cue_with_telemetry.py hold_open 200 > logs/hold_open.log 2>&1 &
nohup python scripts/feed_liveness_with_telemetry.py baseline > logs/liveness_baseline.log 2>&1 &
# Deliberately NO backup_captions_health exporter -- the backup cannot be confirmed.
sleep ${PROPAGATE}
python agent/assembled_agent.py --scenario acc_wont_switch --approve 2>&1 \
  | grep -E "GATE\]|SCOPE\]|request_failover|FINAL" || true

# --- Beat 5: fixture-contamination regression suite ----------------------------------
echo
echo "=== [5/7] fixture-contamination regression tests ==="
python tests/test_series_scope_guard.py

# --- Beat 6: sponsor runtime evidence ------------------------------------------------
echo
echo "=== [6/7] sponsor runtime evidence ==="
python scripts/verify_sponsor_runtime.py

# --- Beat 7: compile the table -------------------------------------------------------
echo
echo "=== [7/7] compiling acceptance table ==="
python scripts/compile_acceptance_table.py
