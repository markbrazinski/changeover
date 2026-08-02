"""Shared evidence-availability / evidence-quality gate.

Extracted from counterfactual_rev.py's proven Arm 2 (20/20 on the frozen bar for total
failure and empty-results) so the SAME gate can be imported by both the trial harness and
the production diagnose-and-failover flow (gate5_diagnose_and_failover.py) -- previously
gate5 had no gate wired in at all, which is a real gap between what was proven and what
ships. This module is the fix for that gap, plus the new stale/partial tier (Task 2).

MECHANICAL NOTE ON "STALE" -- two design attempts and why both were rejected:

Attempt 1: use Prometheus's own per-sample timestamp (now - value[0]). Rejected: confirmed
empirically that a Pushgateway-backed metric's Prometheus-side timestamp keeps advancing
every scrape_interval even when the underlying value never changes, and the series vanishes
almost immediately (one scrape cycle) once its Pushgateway grouping is deleted rather than
aging visibly. So "now - sample_timestamp" is always either fresh or entirely gone (EMPTY).

Attempt 2: treat a high VALUE on a freshness-style metric (sign_feed_freshness_seconds) as
"stale evidence." Rejected after testing against the real frozen-sign-feed fixture from
Gate 4/5: a real, live, correctly-flowing fault (sign_feed_freshness_seconds=8.08s, feed
genuinely frozen) got misclassified as "evidence is stale" -- because a domain metric
reporting a real fault and a telemetry pipeline that's stopped updating produce the exact
same shape (a high number) under this heuristic. That's a genuine confusion between "the
thing being monitored is broken" (a real diagnosis) and "the monitoring itself can't be
trusted" (a refusal) -- conflating them would make the product wrongly refuse to diagnose
the very faults it exists to catch.

Actual mechanism: a SEPARATE heartbeat metric, decoupled from any domain metric's value.
Real exporters push `<job>_heartbeat_unix_time` = their own wall-clock time at push time.
The gate compares that VALUE (not Prometheus's scrape timestamp, not a domain value) to
its own wall-clock now. If the heartbeat value is old, the telemetry pipeline itself has
stopped pushing -- true evidence staleness -- regardless of what any domain metric says.
This can never conflate with a real fault, because a real fault never touches the
heartbeat; only the exporter process update cadence does.

Four tiers, checked in order, cheapest/most total first:
  UNAVAILABLE -- MCP session cannot even attach (total connection failure).
  EMPTY       -- session attaches, tools succeed, but the query space is genuinely empty.
  STALE       -- session attaches, domain data is present, but the job's heartbeat value
                 is older than the staleness threshold -- the exporter has stopped pushing
                 for real, so no domain value from this job can be trusted right now.
  PARTIAL     -- session attaches, heartbeat is fresh, but SOME of the queries needed for a
                 diagnosis return real data and others don't -- genuinely mixed evidence.

All four refuse the same way: the model is never invoked for a full diagnosis turn. The
gate decision is made from real tool calls against the real MCP server -- nothing here is
simulated or hard-coded.
"""
import asyncio
import json
import re
import time

from google.adk.tools.mcp_tool.mcp_toolset import MCPToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams

# A job's heartbeat is stale if its self-reported wall-clock push time is older than this.
# Set generously relative to this rig's real round-trip latency (a single gated diagnosis
# turn -- MCP session attach + several real LLM tool-call round-trips -- commonly takes
# 15-30s on its own), so the gate measures genuine exporter death, not test harness timing.
HEARTBEAT_STALE_THRESHOLD_SECONDS = 90.0


class EvidenceGateResult:
    def __init__(self, tier: str, ok: bool, detail: str, raw_samples: dict = None):
        self.tier = tier  # "available" | "unavailable" | "empty" | "stale" | "partial"
        self.ok = ok       # True = safe to proceed to a full diagnosis turn
        self.detail = detail
        self.raw_samples = raw_samples or {}

    def refusal_message(self) -> str:
        return (
            f"cannot_diagnose: evidence gate tier='{self.tier}'. {self.detail} "
            f"No diagnosis or failover recommendation can be made without complete, "
            f"current evidence. Escalating to a human."
        )

    def to_dict(self) -> dict:
        return {"tier": self.tier, "ok": self.ok, "detail": self.detail, "raw_samples": self.raw_samples}


async def _get_query_tool(mcp_url: str, timeout: float = 8.0):
    toolset = MCPToolset(
        connection_params=StreamableHTTPConnectionParams(url=mcp_url, timeout=5.0),
        tool_filter=["query_prometheus"],
    )
    tools = await asyncio.wait_for(toolset.get_tools(), timeout=timeout)
    query_tool = next((t for t in tools if t.name == "query_prometheus"), None)
    return toolset, query_tool


def _extract_data_array(result) -> list:
    """Pull the real `data` array out of an MCP query_prometheus result -- real parsing,
    no assumption it's present or well-formed."""
    try:
        content = result.get("content", []) if isinstance(result, dict) else []
        text = content[0].get("text", "{}") if content else "{}"
        parsed = json.loads(text)
        return parsed.get("data", [])
    except Exception:
        return []


async def _run_query(query_tool, datasource_uid: str, expr: str):
    try:
        result = await asyncio.wait_for(
            query_tool.run_async(
                args={
                    "datasourceUid": datasource_uid,
                    "expr": expr,
                    "queryType": "instant",
                    "endTime": "now",
                },
                tool_context=None,
            ),
            timeout=8,
        )
        return _extract_data_array(result if isinstance(result, dict) else {})
    except Exception:
        return []


def _job_from_expr(expr: str) -> str | None:
    m = re.search(r'job="([^"]+)"', expr)
    return m.group(1) if m else None


async def _check_heartbeat(query_tool, datasource_uid: str, job: str) -> tuple[bool, float | None]:
    """Returns (is_fresh, age_seconds). A missing heartbeat metric entirely is treated as
    stale (fail closed) rather than assumed fresh."""
    data = await _run_query(query_tool, datasource_uid, f'{job}_heartbeat_unix_time{{job="{job}"}}')
    if not data:
        return False, None
    values = []
    for series in data:
        v = series.get("value")
        if v and len(v) >= 2:
            try:
                values.append(float(v[1]))
            except (TypeError, ValueError):
                pass
    if not values:
        return False, None
    oldest_push = min(values)
    age = time.time() - oldest_push
    return age <= HEARTBEAT_STALE_THRESHOLD_SECONDS, age


async def check_evidence(mcp_url: str, datasource_uid: str, queries: list[str]) -> EvidenceGateResult:
    """queries: list of real PromQL expressions the diagnosis would need. Runs each for
    real against the live MCP server and classifies the result across all four tiers."""
    try:
        toolset, query_tool = await _get_query_tool(mcp_url)
    except Exception as e:
        return EvidenceGateResult("unavailable", False, f"MCP session could not attach: {type(e).__name__}: {e}")

    try:
        per_query = {}
        for expr in queries:
            data = await _run_query(query_tool, datasource_uid, expr)
            per_query[expr] = {"present": bool(data)}

        present_count = sum(1 for v in per_query.values() if v["present"])
        total = len(queries)

        if present_count == 0:
            return EvidenceGateResult(
                "empty", False,
                f"All {total} required queries returned zero series -- the query space is genuinely empty.",
                per_query,
            )

        # Heartbeat check: one per distinct job referenced across the queries.
        jobs = {j for j in (_job_from_expr(e) for e in queries) if j}
        heartbeat_results = {}
        for job in jobs:
            fresh, age = await _check_heartbeat(query_tool, datasource_uid, job)
            heartbeat_results[job] = {"fresh": fresh, "age_seconds": age}

        stale_jobs = {j: v for j, v in heartbeat_results.items() if not v["fresh"]}
        if stale_jobs and present_count > 0:
            def _describe(v):
                if v["age_seconds"] is None:
                    return "no heartbeat metric"
                return f"{v['age_seconds']:.1f}s old"
            detail_parts = [f"{j}={_describe(v)}" for j, v in stale_jobs.items()]
            return EvidenceGateResult(
                "stale", False,
                f"Domain data is present, but the exporter heartbeat for {len(stale_jobs)} "
                f"job(s) is stale or missing: {', '.join(detail_parts)} (threshold "
                f"{HEARTBEAT_STALE_THRESHOLD_SECONDS}s). The telemetry pipeline itself "
                f"appears to have stopped updating -- no domain value from this job can be "
                f"trusted right now, regardless of what it currently reads.",
                {"queries": per_query, "heartbeats": heartbeat_results},
            )

        if present_count < total:
            missing = [e for e, v in per_query.items() if not v["present"]]
            return EvidenceGateResult(
                "partial", False,
                f"Only {present_count}/{total} required queries returned data; missing: {missing}. "
                f"Diagnosing from incomplete evidence is refused.",
                {"queries": per_query, "heartbeats": heartbeat_results},
            )

        return EvidenceGateResult(
            "available", True,
            f"All {total} required queries returned data with a fresh exporter heartbeat.",
            {"queries": per_query, "heartbeats": heartbeat_results},
        )
    finally:
        await toolset.close()
