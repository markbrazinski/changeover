#!/usr/bin/env bash
# N-wide acceptance: the SAME assembled agent, instanced per channel, clearing the full
# behaviour set on each channel's own real film and its own distinct backup.
#
#   ./scripts/run_nwide_acceptance.sh                  # all available channels
#   ./scripts/run_nwide_acceptance.sh tears_of_steel   # one channel
#
# No new agent logic is exercised here -- only CHANGEOVER_CHANNEL changes, which resolves
# the channel's film, sidecar, distinct backup, Prometheus jobs and DERIVED ceiling through
# config/channels.py. Results land in logs/nwide_acceptance_table.json.
#
# Films are supplied out-of-band and are not committed; see "Adding a film" in README.md.
set -uo pipefail
cd "$(dirname "$0")/.."

source .venv/bin/activate
set -a; source .env; set +a
export GRAFANA_MCP_URL="${GRAFANA_MCP_URL:-http://localhost:8001/mcp}"

PG="http://localhost:9091"
PROPAGATE=34

# macOS ships bash 3.2, which has no `mapfile`/`readarray` -- use word splitting instead.
if [ $# -gt 0 ]; then
  CHANNELS="$*"
else
  CHANNELS=$(python -c "
import sys; sys.path.insert(0,'config')
import channels; print(' '.join(channels.available_channels()))")
fi

if [ -z "${CHANNELS// }" ]; then
  echo "no channels with files present -- supply films per README.md 'Adding a film'." >&2
  exit 1
fi

cleanup() {
  pkill -f "caption_cue_with_telemetry" 2>/dev/null || true
  pkill -f "feed_liveness_with_telemetry" 2>/dev/null || true
  pkill -f "backup_captions_health" 2>/dev/null || true
}
trap cleanup EXIT

reset_channel() {
  local ch="$1"
  cleanup; sleep 1
  for g in "media_pipeline_${ch}_captions/mode/frozen_captions" \
           "media_pipeline_${ch}_captions/mode/baseline" \
           "media_pipeline_${ch}_feed_liveness/mode/frozen" \
           "media_pipeline_${ch}_feed_liveness/mode/baseline" \
           "backup_${ch}_captions"; do
    curl -s -X DELETE "${PG}/metrics/job/${g}" > /dev/null || true
  done
}

echo "=== rig up ==="
./scripts/up.sh
python scripts/test-only/wipe_test_fixtures.py || true

for CH in ${CHANNELS}; do
  export CHANGEOVER_CHANNEL="$CH"
  SHORT=$([ "$CH" = "tears_of_steel" ] && echo tos || echo "$CH")
  echo
  echo "############ CHANNEL: ${CH} ############"

  # --- Beat 1: won't guess (needs no telemetry) ---
  echo "--- [1/4] won't guess (MCP unreachable) ---"
  python agent/assembled_agent.py --scenario "nwide_${SHORT}_wontguess" --approve \
    --mcp-url "http://localhost:9999/mcp" 2>&1 | grep -E "GATE\]|CHANNEL\]" || true

  # --- Beat 2: happy path + verify-by-measurement ---
  echo "--- [2/4] happy path + verify-by-measurement (captions faulted) ---"
  reset_channel "$CH"
  nohup python scripts/caption_cue_with_telemetry.py hold_open 300 > logs/${SHORT}_cap.log 2>&1 &
  nohup python scripts/feed_liveness_with_telemetry.py hold_healthy 300 > logs/${SHORT}_liv.log 2>&1 &
  nohup python scripts/backup_captions_health.py watch 360 > logs/${SHORT}_bkp.log 2>&1 &
  sleep ${PROPAGATE}
  # The feed genuinely recovers onto the backup mid-verification, so "restored" is earned
  # by a real post-swap reading rather than asserted on the swap.
  ( sleep 45; pkill -f "caption_cue_with_telemetry.py hold_open" 2>/dev/null; sleep 1; \
    CHANGEOVER_CHANNEL="$CH" nohup python scripts/caption_cue_with_telemetry.py recover 220 \
      > logs/${SHORT}_recover.log 2>&1 & ) >/dev/null 2>&1 &
  python agent/assembled_agent.py --scenario "nwide_${SHORT}_happy" --approve --verify 2>&1 \
    | grep -E "CHANNEL\]|GATE\]|SCOPE\]|VERIFY|verify\]" || true

  # --- Beat 3: discrimination (feed-liveness faulted, captions healthy) ---
  echo "--- [3/4] discrimination (feed-liveness faulted) ---"
  reset_channel "$CH"
  nohup python scripts/caption_cue_with_telemetry.py hold_healthy 300 > logs/${SHORT}_cap.log 2>&1 &
  nohup python scripts/feed_liveness_with_telemetry.py frozen 300 > logs/${SHORT}_liv.log 2>&1 &
  sleep ${PROPAGATE}
  python agent/assembled_agent.py --scenario "nwide_${SHORT}_discrim" --approve 2>&1 \
    | grep -E "CHANNEL\]|GATE\]|SCOPE\]" || true

  # --- Beat 4: won't switch (backup deliberately unconfirmable) ---
  echo "--- [4/4] won't switch (no backup telemetry) ---"
  reset_channel "$CH"
  nohup python scripts/caption_cue_with_telemetry.py hold_open 260 > logs/${SHORT}_cap.log 2>&1 &
  nohup python scripts/feed_liveness_with_telemetry.py hold_healthy 260 > logs/${SHORT}_liv.log 2>&1 &
  # NO backup exporter started -- the backup cannot be confirmed.
  sleep ${PROPAGATE}
  python agent/assembled_agent.py --scenario "nwide_${SHORT}_wontswitch" --approve 2>&1 \
    | grep -E "CHANNEL\]|request_failover ->" || true
done

cleanup
echo
echo "=== N-wide acceptance table ==="
python scripts/compile_nwide_table.py
