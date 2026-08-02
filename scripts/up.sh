#!/usr/bin/env bash
# One-command bring-up for the whole rig: Prometheus + Pushgateway + Grafana + Grafana MCP server.
set -euo pipefail
cd "$(dirname "$0")/.."

set -a
source .env
set +a
envsubst < prometheus/prometheus.yml.template > prometheus/prometheus.yml

docker compose up -d

echo "waiting for grafana..."
until curl -sf http://localhost:3000/api/health > /dev/null; do sleep 1; done

echo "waiting for prometheus..."
until curl -sf http://localhost:9090/-/healthy > /dev/null; do sleep 1; done

echo "waiting for grafana-mcp..."
until curl -sf -o /dev/null -X POST http://localhost:8000/mcp \
  -H "Accept: application/json, text/event-stream" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":0,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"healthcheck","version":"0"}}}'; do sleep 1; done

echo "rig is up:"
echo "  Grafana:     http://localhost:3000  (admin/admin)"
echo "  Prometheus:  http://localhost:9090"
echo "  Grafana MCP: http://localhost:8000/mcp"
