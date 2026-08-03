"""Guard against fixture/sibling-series contamination of a diagnosis query.

THE REAL BUG THIS FIXES
-----------------------
`sign_feed_freshness_seconds` is pushed by more than one producer into the same Prometheus
metric name:

    job="media_pipeline_sign"    mode="frozen"     8.10   <- the REAL fault
    job="media_pipeline_sign"    mode="baseline"   0.0107 <- a healthy sibling run
    job="accessibility_layers"   (no mode)         0.20   <- scripts/reseed.py seed data

Any query that selects the metric WITHOUT pinning job (and mode, where the producer uses
one) matches all three. An aggregation over that set then reports a healthy number for a
genuinely faulted layer:

    min(sign_feed_freshness_seconds{layer="sign_language"})  ->  0.0107

which is how a real 8.1s fault once read as "fresh". Reproduced directly against the live
rig; see tests/test_series_scope_guard.py.

WHAT THIS MODULE DOES
---------------------
Given the expressions a diagnosis is about to rely on, it asks Prometheus (through the SAME
real MCP path the rest of the agent uses) how many distinct series each metric name has,
and refuses when a query is under-scoped relative to what actually exists. It never
inspects or second-guesses metric VALUES -- a high number is a diagnosis, not a defect,
which is the same principle agent/evidence_gate.py's design note establishes for staleness.

This is deliberately a SEPARATE module from evidence_gate.py: the evidence gate answers
"can this evidence be trusted to be current and complete?", while this answers "does this
query actually address the one series I think it does?" Both must pass.
"""
import re

# Labels that, when present on a series, must be pinned by any query claiming to diagnose
# a specific feed. `job` separates producers; `mode` separates a producer's baseline run
# from its fault run -- leaving either free is what lets a healthy sibling answer for a
# faulted one.
DISCRIMINATING_LABELS = ("job", "mode")

METRIC_NAME_RE = re.compile(r"^\s*([a-zA-Z_:][a-zA-Z0-9_:]*)\s*\{")


class ScopeGuardResult:
    def __init__(self, ok: bool, detail: str, findings: list = None):
        self.ok = ok
        self.detail = detail
        self.findings = findings or []

    def refusal_message(self) -> str:
        return (
            f"cannot_diagnose: query scope is ambiguous. {self.detail} "
            f"Refusing to diagnose from a query that does not uniquely identify the feed "
            f"under investigation -- a healthy sibling series could answer for a faulted "
            f"one. Escalating to a human."
        )

    def to_dict(self) -> dict:
        return {"ok": self.ok, "detail": self.detail, "findings": self.findings}


def metric_name_of(expr: str) -> str | None:
    m = METRIC_NAME_RE.match(expr)
    return m.group(1) if m else None


def pinned_labels(expr: str) -> set:
    """Labels this expression pins with an exact match (=). A regex or negative matcher is
    deliberately NOT counted as pinning: `mode=~".*"` selects everything."""
    return set(re.findall(r'(\w+)\s*=\s*"', expr))


def check_scope(series_by_metric: dict, exprs: list) -> ScopeGuardResult:
    """series_by_metric: {metric_name: [ {label: value}, ... ]} -- REAL series label sets
    as returned by Prometheus, not assumptions.

    A query is under-scoped when the metric it selects has more than one distinct value for
    a discriminating label that the query does not pin.
    """
    findings = []

    for expr in exprs:
        name = metric_name_of(expr)
        if not name:
            continue
        series = series_by_metric.get(name, [])
        if len(series) <= 1:
            continue  # only one series exists; nothing to confuse it with

        pinned = pinned_labels(expr)
        for label in DISCRIMINATING_LABELS:
            distinct = {s.get(label) for s in series if s.get(label) is not None}
            if len(distinct) > 1 and label not in pinned:
                findings.append({
                    "expr": expr,
                    "metric": name,
                    "unpinned_label": label,
                    "distinct_values": sorted(str(v) for v in distinct),
                    "series_count": len(series),
                })

    if findings:
        parts = [
            f"{f['metric']} has {len(f['distinct_values'])} distinct '{f['unpinned_label']}' "
            f"values {f['distinct_values']} but the query does not pin it"
            for f in findings
        ]
        return ScopeGuardResult(False, "; ".join(parts), findings)

    return ScopeGuardResult(True, f"All {len(exprs)} queries uniquely scope their series.")
