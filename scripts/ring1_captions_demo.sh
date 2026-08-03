#!/usr/bin/env bash
# Ring 1, captions happy path -- ONE command, clean rig to recorded feed-state swap.
#
#   ./scripts/ring1_captions_demo.sh --approve
#
# Without --approve the agent still investigates and recommends, but failover_tool.py
# refuses to execute the switch (there is no path for the model to authorize itself).
set -euo pipefail
cd "$(dirname "$0")/.."

APPROVE_ARG="${1:-}"

source .venv/bin/activate
set -a; source .env; set +a
export GRAFANA_MCP_URL="${GRAFANA_MCP_URL:-http://localhost:8001/mcp}"

echo "=== [0/5] rig up (Prometheus + Pushgateway + Grafana + Grafana MCP) ==="
./scripts/up.sh

echo
echo "=== [1/5] pre-demo fixture hygiene ==="
# Non-fatal: this wipes leftover test-only fixtures and exits 1 if it had to clean up.
python scripts/test-only/wipe_test_fixtures.py || true

echo
echo "=== [2/5] baseline captions run (real cue-vs-program-clock measurement) ==="
python scripts/caption_cue_with_telemetry.py baseline

# The baseline run is a COMPLETED historical run, not a live exporter. Its heartbeat stops
# advancing the moment the script exits. agent/evidence_gate.py takes the OLDEST heartbeat
# across a job's series, so leaving this grouping in Pushgateway makes the whole job read
# as stale (correctly -- that series really does have a dead exporter) and the gate refuses
# before the model is ever invoked. Retiring the grouping leaves only live exporters
# reporting a heartbeat. The baseline SAMPLES already scraped into Prometheus are retained
# for the curve; this only stops Pushgateway from re-serving a dead exporter's heartbeat.
echo "  retiring completed baseline exporter grouping from Pushgateway..."
curl -sf -X DELETE "http://localhost:9091/metrics/job/media_pipeline_captions/mode/baseline" || true

echo
echo "=== [3/5] frozen-captions fault (cue publisher genuinely stopped) ==="
python scripts/caption_cue_with_telemetry.py frozen_captions

echo
echo "=== [4/5] backup captions health (measured, not asserted) ==="
# Held open for the same reason as the primary exporter: the backup-health check runs
# through the same evidence gate, which refuses telemetry whose heartbeat has gone stale.
python scripts/backup_captions_health.py watch 300 > logs/backup_health.log 2>&1 &
BACKUP_PID=$!
sleep 10
head -3 logs/backup_health.log

# The captions pipeline being demonstrated is LIVE with a stalled cue publisher -- the
# exporter is healthy and reporting, the thing it measures is broken. A one-shot producer
# that exited would be a genuinely dead exporter and the gate would refuse it (correctly).
# Hold the exporter open, still pushing the real, still-climbing offset, for the duration
# of the agent's investigation.
echo
echo "  holding captions exporter open during investigation..."
python scripts/caption_cue_with_telemetry.py hold_open 300 > logs/hold_open.log 2>&1 &
HOLD_PID=$!
trap 'kill ${HOLD_PID} ${BACKUP_PID} 2>/dev/null || true' EXIT

# Prometheus scrapes Pushgateway on an interval and remote_writes to Grafana Cloud; the
# agent reads through Grafana, so give the samples a real propagation margin.
echo "  waiting 25s for scrape + remote_write propagation..."
sleep 25

echo
echo "=== [5/5] assembled agent run: MCP investigation -> diagnosis -> failover ==="
python agent/ring1_captions_loop.py ${APPROVE_ARG}

kill ${HOLD_PID} ${BACKUP_PID} 2>/dev/null || true

echo
echo "=== terminal state: logs/feed_state.json ==="
python -c "import json;s=json.load(open('logs/feed_state.json'));print(json.dumps(s['history'][-1],indent=2)) if s.get('history') else print('(no history)')"
