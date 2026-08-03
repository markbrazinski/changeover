#!/usr/bin/env bash
# PHASE 2 CONTENTION -- the single entrypoint.
#
#   ./scripts/phase2_contention.sh
#
# Creates REAL concurrent incidents on every channel, then runs the supervisor twice:
#   1. WITHOUT human authorization -- it must compute the allocation and REFUSE to execute.
#   2. WITH human authorization    -- it protects the premium channel and leaves the rest
#                                     degraded and flagged.
#
# Scarcity is real, not staged: the shared backup pool is M=1 while N=2 channels are
# concurrently faulted, so the shortage is structural. Both faults are produced by really
# stopping each channel's cue publisher -- the same fault the single-channel demo uses.
#
# Artifacts: logs/contention_unauthorized.json, logs/contention_authorized.json
#
# Scope: the supervisor allocates. failover_tool executes, unchanged, and independently
# re-verifies each backup and the human authorizer. There is no rollback -- a degraded
# channel stays degraded and flagged.
set -uo pipefail
cd "$(dirname "$0")/.."

source .venv/bin/activate
set -a; source .env; set +a
export GRAFANA_MCP_URL="${GRAFANA_MCP_URL:-http://localhost:8001/mcp}"

AUTHORIZER="${CHANGEOVER_AUTHORIZER:-mark@brazinski.us}"
CAPACITY="${CHANGEOVER_BACKUP_POOL:-1}"
PG="http://localhost:9091"
PROPAGATE=36

CHANNELS=$(python -c "
import sys; sys.path.insert(0,'config')
import channels; print(' '.join(channels.available_channels()))")

if [ -z "${CHANNELS// }" ]; then
  echo "ERROR: no channels present. Supply films per README.md 'Adding a film'." >&2
  exit 1
fi

cleanup() {
  pkill -f "caption_cue_with_telemetry" 2>/dev/null || true
  pkill -f "feed_liveness_with_telemetry" 2>/dev/null || true
  pkill -f "backup_captions_health" 2>/dev/null || true
}
trap cleanup EXIT

echo "########## STAGE 1/4: rig ##########"
./scripts/up.sh
python scripts/test-only/wipe_test_fixtures.py || true

echo
echo "########## STAGE 2/4: real concurrent incidents on every channel ##########"
cleanup; sleep 1
for CH in ${CHANNELS}; do
  for SUFFIX in captions/mode/frozen_captions captions/mode/baseline \
                feed_liveness/mode/frozen feed_liveness/mode/baseline; do
    curl -s -X DELETE "${PG}/metrics/job/media_pipeline_${CH}_${SUFFIX}" > /dev/null || true
  done
done

for CH in ${CHANNELS}; do
  echo "  ${CH}: stopping the caption cue publisher for real"
  CHANGEOVER_CHANNEL="$CH" nohup python scripts/caption_cue_with_telemetry.py hold_open 340 \
    > "logs/ct_${CH}_cap.log" 2>&1 &
  # Backup health must be live, or failover is refused for a stale-telemetry reason rather
  # than on the contention decision under test.
  CHANGEOVER_CHANNEL="$CH" nohup python scripts/backup_captions_health.py watch 400 \
    > "logs/ct_${CH}_bkp.log" 2>&1 &
done

echo "  waiting ${PROPAGATE}s for scrape + remote_write propagation..."
sleep ${PROPAGATE}

echo
echo "########## STAGE 3/4: supervisor WITHOUT authorization (must refuse) ##########"
python agent/contention_supervisor.py --capacity "${CAPACITY}" --label unauthorized

echo
echo "########## STAGE 4/4: supervisor WITH human-authorized prioritization ##########"
python agent/contention_supervisor.py --capacity "${CAPACITY}" \
  --authorize "${AUTHORIZER}" --execute --label authorized

echo
echo "=== per-channel terminal state ==="
python - <<'PY'
import json, os, sys
sys.path.insert(0, "config")
import channels as registry
for ch in registry.available_channels():
    p = f"logs/feed_state_{ch}.json"
    tier = registry.criticality_tier(ch)
    if not os.path.exists(p):
        print(f"  {ch:16s} tier={tier:9s} NO state file -- never switched (left degraded)")
        continue
    s = json.load(open(p))
    print(f"  {ch:16s} tier={tier:9s} active_feed={s['active_feed']:8s} "
          f"switches={len(s['history'])}")
PY

echo
echo "=== sponsor runtime ==="
python scripts/verify_sponsor_runtime.py | tail -6

cleanup
echo
echo "PHASE 2 artifacts: logs/contention_unauthorized.json logs/contention_authorized.json"
