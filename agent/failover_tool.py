"""Gate 5: a SEPARATE, narrowly-scoped tool that performs failover -- switching a layer's
active feed from primary to a verified backup. This is NOT wired through Grafana (Grafana
investigates; it does not actuate, per the concept's design constraint). It writes a real
state file that a human must approve before the switch executes, and the state change is
independently readable/inspectable afterward.

Task 3 extension: verify_backup_feed_healthy() previously only checked that the backup
video FILE is structurally playable (ffprobe). That's necessary but not sufficient -- a
backup can be a perfectly valid, reachable file while its OWN accessibility-layer pipeline
is itself degraded (e.g. its captioning has stalled). Failing over onto a reachable-but-
broken backup would just trade one fault for another while reporting success. The check
now also queries the backup's own real telemetry (backup_sign_language_freshness_seconds,
via scripts/backup_health_exporter.py) through the same evidence gate used elsewhere, and
refuses if that telemetry shows a real fault OR is itself unavailable/stale/partial.
"""
import asyncio
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(__file__))

STATE_PATH = os.path.join(os.path.dirname(__file__), "..", "logs", "feed_state.json")
MCP_URL = os.environ.get("GRAFANA_MCP_URL", "http://localhost:8001/mcp")
DATASOURCE_UID = "grafanacloud-prom"

# Freshness above this means the backup's OWN pipeline has stalled -- same real signal
# shape as the primary-feed fault detection, applied to the backup instead.
BACKUP_FRESHNESS_FAULT_THRESHOLD_SECONDS = 3.0


def read_state() -> dict:
    if not os.path.exists(STATE_PATH):
        return {"layer": None, "active_feed": "primary", "history": []}
    with open(STATE_PATH) as f:
        return json.load(f)


def write_state(state: dict):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


BACKUP_FEEDS = {
    "sign_language": os.path.join(os.path.dirname(__file__), "..", "fixtures", "source.mp4"),
}


# Which real telemetry job represents each layer's backup's OWN pipeline health, and the
# PromQL expressions the backup-health gate needs. Layers with no entry here have no
# backup-telemetry check available and are refused (fail closed), same as an unregistered
# backup file.
BACKUP_TELEMETRY_JOBS = {
    "sign_language": "backup_sign_language",
}


def _check_backup_telemetry(job: str) -> tuple:
    """Runs the real evidence gate PLUS a real value fetch against the backup's own
    telemetry, in an isolated thread with its own event loop -- verify_backup_feed_healthy()
    is called synchronously from inside an already-running ADK event loop (via
    request_failover), so a plain asyncio.run() here would raise "cannot be called from a
    running event loop." A dedicated thread sidesteps that safely without needing
    failover_tool's whole call chain to become async.

    Returns (ok, freshness_value_or_None, detail)."""
    from evidence_gate import check_evidence, _get_query_tool, _run_query

    expr = f'backup_sign_language_freshness_seconds{{job="{job}"}}'
    result_holder = {}

    def runner():
        async def go():
            gate = await check_evidence(MCP_URL, DATASOURCE_UID, [expr])
            if not gate.ok:
                return gate, None
            toolset, query_tool = await _get_query_tool(MCP_URL)
            try:
                data = await _run_query(query_tool, DATASOURCE_UID, expr)
            finally:
                await toolset.close()
            values = []
            for series in data:
                v = series.get("value")
                if v and len(v) >= 2:
                    try:
                        values.append(float(v[1]))
                    except (TypeError, ValueError):
                        pass
            return gate, (max(values) if values else None)

        result_holder["result"] = asyncio.run(go())

    t = threading.Thread(target=runner)
    t.start()
    t.join(timeout=20)
    result = result_holder.get("result")
    if result is None:
        return False, None, "backup telemetry check timed out or did not complete"

    gate, freshness = result
    if not gate.ok:
        return False, None, f"backup telemetry gate tier={gate.tier}: {gate.detail}"
    if freshness is None:
        return False, None, "backup telemetry gate said available but no value could be parsed"
    return True, freshness, gate.detail


def verify_backup_feed_healthy(layer: str) -> bool:
    """Real, two-part check, not a stub-always-true:
      1. The backup video FILE exists and is structurally playable (ffprobe) -- unchanged
         from before this task.
      2. The backup's OWN accessibility-layer telemetry is available, fresh, complete, AND
         shows no real fault of its own -- new in Task 3. A backup that is a perfectly
         valid, reachable file but whose own pipeline has stalled must still be refused;
         otherwise failover just trades one broken feed for another while reporting
         success.
    Layers with no registered backup file, or no registered backup-telemetry job, are
    refused (fail closed) rather than assumed healthy."""
    import subprocess
    backup_path = BACKUP_FEEDS.get(layer)
    if not backup_path or not os.path.exists(backup_path):
        return False
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", backup_path],
        capture_output=True, text=True,
    )
    if result.returncode != 0 or "duration" not in result.stdout:
        return False

    telemetry_job = BACKUP_TELEMETRY_JOBS.get(layer)
    if not telemetry_job:
        return False

    ok, freshness, _detail = _check_backup_telemetry(telemetry_job)
    if not ok or freshness is None:
        return False
    if freshness > BACKUP_FRESHNESS_FAULT_THRESHOLD_SECONDS:
        return False

    return True


def failover(layer: str, reason: str, authorized_by: str) -> dict:
    """Scoped failover action. Requires an explicit human authorizer string -- this
    function will not execute without one, enforcing human-in-the-loop by construction."""
    if not authorized_by or not authorized_by.strip():
        raise ValueError("failover refused: no human authorizer provided")

    if not verify_backup_feed_healthy(layer):
        raise RuntimeError(f"failover refused: backup feed for '{layer}' failed health check")

    state = read_state()
    prior_feed = state.get("active_feed", "primary")
    new_feed = "backup" if prior_feed == "primary" else "primary"

    state["layer"] = layer
    state["active_feed"] = new_feed
    state.setdefault("history", []).append({
        "timestamp": time.time(),
        "layer": layer,
        "from": prior_feed,
        "to": new_feed,
        "reason": reason,
        "authorized_by": authorized_by,
    })
    write_state(state)
    return state


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("usage: python failover_tool.py <layer> <reason> <authorized_by>")
        sys.exit(1)
    layer, reason, authorized_by = sys.argv[1], sys.argv[2], sys.argv[3]
    result = failover(layer, reason, authorized_by)
    print(json.dumps(result, indent=2))
