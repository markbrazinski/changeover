"""Structured, timestamped tool-call trace -- the surface the UI binds to.

Every record describes a call that ACTUALLY HAPPENED. Records are appended as the agent's
event stream is drained, from the real function_call / function_response parts ADK emits;
nothing is synthesized, back-filled, or embellished. If a query really missed and the model
really retried, that appears here as two records because two calls really occurred -- and
if it did not happen on a given run, no such record exists.

SCHEMA (one JSON object per record; the file is a JSON array)
------------------------------------------------------------
  seq          int     monotonic call index within the run, starting at 0
  timestamp    float   unix epoch seconds when the call was observed
  iso          str     the same instant, ISO-8601, for display
  tool         str     tool name as invoked (e.g. "query_prometheus", "request_failover")
  args         object  arguments as actually passed
  status       str     "ok" | "miss" | "error"
                         ok    -- returned real data
                         miss  -- succeeded but returned zero series (the query-miss case)
                         error -- the tool itself reported an error
  result       object  the tool's real response, truncated for display (see MAX_RESULT_CHARS)
  latency_ms   float|null  milliseconds between this call and its response, when both were
                           observed; null when the response could not be paired
  layer        str|null    the accessibility layer this call concerns, when determinable
                           from the args (routing hint for the UI; never a diagnosis)

A "miss" is detected from the real response body: the Grafana MCP returns a data array plus
a hints object when a query matches nothing. That is a genuine empty result, not an error,
and the distinction matters -- the UI renders miss-then-retry as the agent self-correcting.
"""
import json
import os
import re
import time

MAX_RESULT_CHARS = 1200


def _classify(response) -> tuple:
    """Returns (status, parsed_or_raw). Inspects the REAL response body -- no assumptions
    about shape beyond what the MCP actually returns."""
    text = None
    try:
        if isinstance(response, dict):
            if response.get("isError"):
                return "error", response
            content = response.get("content")
            if isinstance(content, list) and content:
                text = content[0].get("text")
            if response.get("error"):
                return "error", response
    except Exception:
        pass

    if text is None:
        return "ok", response

    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return "ok", response

    if isinstance(parsed, dict):
        data = parsed.get("data")
        # An empty data array is a real query miss: the call succeeded, matched nothing.
        if isinstance(data, list) and len(data) == 0:
            return "miss", parsed
        if data is not None:
            return "ok", parsed
    return "ok", parsed


_LAYER_RE = re.compile(r'layer="([^"]+)"')
_JOB_LAYER_HINTS = {
    "media_pipeline_captions": "captions",
    "media_pipeline_feed_liveness": "sign_language",
    "backup_captions": "captions",
    "backup_sign_language": "sign_language",
}


def _layer_of(args: dict) -> str | None:
    """Best-effort routing hint from the real args. Returns None rather than guessing."""
    if not isinstance(args, dict):
        return None
    if args.get("layer"):
        return args["layer"]
    blob = json.dumps(args)
    m = _LAYER_RE.search(blob)
    if m:
        return m.group(1)
    for job, layer in _JOB_LAYER_HINTS.items():
        if job in blob:
            return layer
    return None


class TraceRecorder:
    """Accumulates real call records. Call observe_call() / observe_response() as the ADK
    event stream is drained, in the order events actually arrive."""

    def __init__(self, run_label: str):
        self.run_label = run_label
        self.records = []
        self._pending = {}  # tool name -> seq of the most recent unanswered call

    def observe_call(self, tool: str, args: dict):
        now = time.time()
        rec = {
            "seq": len(self.records),
            "timestamp": now,
            "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)),
            "tool": tool,
            "args": args,
            "status": "pending",
            "result": None,
            "latency_ms": None,
            "layer": _layer_of(args),
        }
        self.records.append(rec)
        self._pending.setdefault(tool, []).append(rec["seq"])
        return rec["seq"]

    def observe_response(self, tool: str, response):
        seqs = self._pending.get(tool) or []
        if not seqs:
            return None
        seq = seqs.pop(0)
        rec = self.records[seq]
        status, parsed = _classify(response)
        rec["status"] = status
        rec["latency_ms"] = round((time.time() - rec["timestamp"]) * 1000, 1)
        blob = json.dumps(parsed, default=str)
        if len(blob) <= MAX_RESULT_CHARS:
            rec["result"] = json.loads(blob)
        else:
            # Truncate for display, but preserve the top-level scalar fields callers key on
            # (e.g. an actuating tool's "status"). Dropping them made a real executed
            # failover unreadable downstream, which silently skipped post-swap verification.
            preserved = {}
            if isinstance(parsed, dict):
                preserved = {
                    k: v for k, v in parsed.items()
                    if isinstance(v, (str, int, float, bool)) or v is None
                }
            rec["result"] = {"_truncated": True, "preview": blob[:MAX_RESULT_CHARS], **preserved}
        return seq

    # --- derived facts, computed from the real records only ---

    def had_query_miss_then_retry(self) -> bool:
        """True when a query really missed and a LATER query call really followed it."""
        for i, rec in enumerate(self.records):
            if rec["tool"] == "query_prometheus" and rec["status"] == "miss":
                if any(r["tool"] == "query_prometheus" for r in self.records[i + 1:]):
                    return True
        return False

    def layers_queried(self) -> list:
        return sorted({r["layer"] for r in self.records if r["layer"]})

    def summary(self) -> dict:
        return {
            "run_label": self.run_label,
            "total_calls": len(self.records),
            "by_status": {
                s: sum(1 for r in self.records if r["status"] == s)
                for s in ("ok", "miss", "error", "pending")
            },
            "tools_used": sorted({r["tool"] for r in self.records}),
            "layers_queried": self.layers_queried(),
            "query_miss_then_retry": self.had_query_miss_then_retry(),
        }

    def write(self, path: str):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(
                {"schema_version": 1, "summary": self.summary(), "records": self.records},
                f, indent=2, default=str,
            )
        return path
