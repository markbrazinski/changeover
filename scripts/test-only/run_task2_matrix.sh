#!/usr/bin/env bash
# Task 2 frozen-bar matrix: 5x stale, 5x partial, 5x control. Refreshes real fixtures
# immediately before each run that needs freshness (partial's heartbeat is cheap to
# re-push; control needs the real ffmpeg fault pipeline re-run since it's a one-shot
# script, not a continuously-running exporter).
set -euo pipefail
cd "$(dirname "$0")/../.."
source .venv/bin/activate
set -a; source .env; set +a
export GRAFANA_MCP_URL="http://localhost:8001/mcp"

echo "=== STALE (no heartbeat pushed at all, ever) ==="
for i in 0 1 2 3 4; do
  echo "--- stale run $i ---"
  python agent/task2_evidence_quality.py stale $i 2>&1 | grep -E "clean_pass|gate:"
done

echo
echo "=== PARTIAL (fresh heartbeat re-pushed before each run, with propagation wait) ==="
for i in 0 1 2 3 4; do
  python scripts/test-only/seed_evidence_quality_fixtures.py partial > /dev/null
  sleep 10  # local Prometheus scrape (5s) + remote_write propagation to Grafana Cloud
  echo "--- partial run $i ---"
  python agent/task2_evidence_quality.py partial $i 2>&1 | grep -E "clean_pass|gate:"
done

echo
echo "=== CONTROL (real ffmpeg fault pipeline re-run before each run) ==="
for i in 0 1 2 3 4; do
  echo "-- re-running real sign-feed fault pipeline for run $i --"
  python scripts/sign_feed_with_telemetry.py frozen > /dev/null 2>&1
  sleep 8  # remote_write propagation margin, same as partial
  echo "--- control run $i ---"
  python agent/task2_evidence_quality.py control $i 2>&1 | grep -E "clean_pass|gate:"
done
