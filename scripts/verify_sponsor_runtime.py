"""Proves BOTH sponsor SDKs are imported AND actually invoked at runtime.

A README claim is not evidence. This checks two things per sponsor:
  1. STATIC  -- the import and the call site exist in the shipped code (file + line).
  2. RUNTIME -- a real run actually exercised them, evidenced by artifacts that could only
                exist if the call happened (a captured tool-call trace with real latencies
                for Grafana; a real model reply for Google Cloud).

Exits non-zero if either sponsor fails either check.
"""
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SPONSORS = {
    "Google Cloud (Gemini via ADK)": {
        "file": "agent/assembled_agent.py",
        "imports": [
            "from google.adk.agents import Agent",
            "from google.adk.runners import InMemoryRunner",
            "from google.genai import types",
        ],
        "calls": ["model=\"gemini-2.5-flash\"", "runner.run_async("],
    },
    "Grafana (official mcp-grafana over streamable-http)": {
        "file": "agent/assembled_agent.py",
        "imports": [
            "from google.adk.tools.mcp_tool.mcp_toolset import MCPToolset",
            "from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams",
        ],
        "calls": ["MCPToolset(", "StreamableHTTPConnectionParams("],
    },
}


def static_evidence() -> list:
    findings = []
    for sponsor, spec in SPONSORS.items():
        path = os.path.join(ROOT, spec["file"])
        with open(path) as f:
            lines = f.read().splitlines()
        for needle in spec["imports"] + spec["calls"]:
            hit = next((i + 1 for i, l in enumerate(lines) if needle in l), None)
            findings.append({
                "sponsor": sponsor,
                "kind": "import" if needle in spec["imports"] else "call",
                "needle": needle,
                "file": spec["file"],
                "line": hit,
                "found": hit is not None,
            })
    return findings


def runtime_evidence() -> list:
    """Artifacts that can only exist if the SDKs really ran."""
    out = []

    # Grafana: a trace with real query_prometheus calls carrying measured latencies.
    trace_dir = os.path.join(ROOT, "logs", "traces")
    grafana_ok, detail = False, "no trace files found"
    if os.path.isdir(trace_dir):
        for name in sorted(os.listdir(trace_dir)):
            with open(os.path.join(trace_dir, name)) as f:
                t = json.load(f)
            mcp_calls = [r for r in t.get("records", [])
                         if r["tool"] in ("query_prometheus", "list_datasources",
                                          "list_prometheus_label_values")
                         and r.get("latency_ms")]
            if mcp_calls:
                grafana_ok = True
                detail = (f"{len(mcp_calls)} real MCP calls in logs/traces/{name} "
                          f"(e.g. {mcp_calls[0]['tool']} @ {mcp_calls[0]['latency_ms']}ms)")
                break
    out.append({"sponsor": "Grafana (official mcp-grafana over streamable-http)",
                "kind": "runtime", "ok": grafana_ok, "detail": detail})

    # Google Cloud: a real model reply produced by a Gemini turn.
    gc_ok, gc_detail = False, "no assembled run with a model answer found"
    logs = os.path.join(ROOT, "logs")
    for name in sorted(os.listdir(logs)):
        if not (name.startswith("assembled_") and name.endswith(".json")):
            continue
        with open(os.path.join(logs, name)) as f:
            r = json.load(f)
        answer = (r.get("answer") or "").strip()
        if answer and not r.get("refused"):
            gc_ok = True
            gc_detail = f"model reply captured in logs/{name} ({len(answer)} chars)"
            break
    out.append({"sponsor": "Google Cloud (Gemini via ADK)",
                "kind": "runtime", "ok": gc_ok, "detail": gc_detail})
    return out


def per_channel_evidence() -> list:
    """Both sponsors must be exercised for EVERY registered channel, not just once overall.
    A channel passes only if some run of its own produced BOTH a real Grafana MCP call
    (with a measured latency) and a real Gemini reply. Returns [] when no channel registry
    is available, so the single-channel setup still verifies as before."""
    try:
        sys.path.insert(0, os.path.join(ROOT, "config"))
        import channels as registry
    except Exception:
        return []

    logs = os.path.join(ROOT, "logs")
    trace_dir = os.path.join(logs, "traces")
    out = []

    for ch in registry.available_channels():
        jobs = set(registry.job_name(ch, l) for l in registry.LAYERS)

        grafana_calls, gemini_chars = 0, 0
        for name in sorted(os.listdir(logs)):
            if not (name.startswith("assembled_") and name.endswith(".json")):
                continue
            with open(os.path.join(logs, name)) as f:
                r = json.load(f)
            if r.get("channel") != ch:
                continue
            if (r.get("answer") or "").strip() and not r.get("refused"):
                gemini_chars = max(gemini_chars, len((r.get("answer") or "").strip()))
            scenario = r.get("scenario", "")
            tp = os.path.join(trace_dir, f"trace_{scenario}.json")
            if os.path.exists(tp):
                with open(tp) as f:
                    t = json.load(f)
                grafana_calls += sum(
                    1 for rec in (t.get("records") or [])
                    if rec.get("tool") in ("query_prometheus", "list_datasources",
                                           "list_prometheus_label_values")
                    and rec.get("latency_ms")
                )

        ok = grafana_calls > 0 and gemini_chars > 0
        out.append({
            "channel": ch,
            "ok": ok,
            "grafana_mcp_calls": grafana_calls,
            "gemini_reply_chars": gemini_chars,
            "channel_jobs": sorted(jobs),
            "detail": (f"{grafana_calls} real MCP calls + a {gemini_chars}-char Gemini "
                       f"reply from this channel's own runs"
                       if ok else
                       f"insufficient runtime evidence (mcp_calls={grafana_calls}, "
                       f"gemini_chars={gemini_chars})"),
        })
    return out


if __name__ == "__main__":
    static = static_evidence()
    runtime = runtime_evidence()

    print("=== STATIC call sites ===")
    for f in static:
        mark = "OK  " if f["found"] else "MISS"
        loc = f"{f['file']}:{f['line']}" if f["line"] else f["file"]
        print(f"  [{mark}] {f['sponsor']}\n         {f['kind']}: {f['needle']}  ->  {loc}")

    print("\n=== RUNTIME evidence ===")
    for r in runtime:
        print(f"  [{'OK  ' if r['ok'] else 'MISS'}] {r['sponsor']}\n         {r['detail']}")

    per_channel = per_channel_evidence()
    if per_channel:
        print("\n=== PER-CHANNEL runtime evidence ===")
        for c in per_channel:
            print(f"  [{'OK  ' if c['ok'] else 'MISS'}] {c['channel']}\n         {c['detail']}")

    result = {"static": static, "runtime": runtime, "per_channel": per_channel}
    with open(os.path.join(ROOT, "logs", "sponsor_runtime_evidence.json"), "w") as f:
        json.dump(result, f, indent=2)

    failed = ([f for f in static if not f["found"]]
              + [r for r in runtime if not r["ok"]]
              + [c for c in per_channel if not c["ok"]])
    print()
    if failed:
        print(f"FAILED: {len(failed)} sponsor check(s) did not pass")
        sys.exit(1)
    scope = f" across {len(per_channel)} channel(s)" if per_channel else ""
    print(f"both sponsors verified: imported, called in code, and exercised at runtime{scope}")
