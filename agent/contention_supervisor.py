"""Contention supervisor: arbitrates scarce backup capacity across N channel-agents.

WHAT THIS IS
------------
The channel-agents are unchanged and are not imported for their reasoning: each one already
diagnoses its own channel and can execute a human-authorized failover. What they cannot do
is see each other. When two channels have concurrent incidents and there is only one backup
in the pool, somebody has to decide who gets protected -- and to say plainly that the other
one is being left degraded. That judgement lives here and nowhere else.

WHAT IT DOES
  1. Reads each channel's REAL current telemetry (through the same Grafana MCP path the
     agents use) and classifies it as incident / healthy from that channel's OWN derived
     ceiling. Incidents are observed, never declared.
  2. Compares concurrent incidents against real pool capacity M (config/channels.py).
  3. If M >= incidents, there is no contention -- it says so and allocates to all.
     If M < incidents, it allocates by criticality tier and leaves the rest DEGRADED.
  4. Requires a human to authorize THE PRIORITIZATION before any allocation executes --
     the same discipline as the single-switch authorizer, applied one level up. The
     supervisor cannot originate this approval.
  5. Writes a contention artifact recording incidents, capacity, the signal used, the
     allocation, the degraded set, and the authorization.

WHAT IT IS NOT
  * It does not diagnose. Diagnosis stays in the channel-agents.
  * It does not actuate. Execution goes through failover_tool.failover(), unchanged, which
    independently re-verifies the backup and independently requires a human authorizer.
  * It does not rank by anything it measured. Criticality tier is OPERATOR-DECLARED (see
    config/channels.py) and is recorded as such in every artifact. Nothing here derives
    importance from bitrate, resolution or any other technical property -- doing so would
    dress an encoding artifact up as a business fact.
  * It has no rollback and no recovery-of-the-degraded logic. A degraded channel stays
    degraded and flagged for a human.
"""
import argparse
import asyncio
import json
import os
import sys
import time

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "config"))

import channels as registry  # noqa: E402
from evidence_gate import check_evidence, _get_query_tool, _run_query  # noqa: E402

MCP_URL = os.environ.get("GRAFANA_MCP_URL", "http://localhost:8001/mcp")
DATASOURCE_UID = "grafanacloud-prom"
ROOT = os.path.join(os.path.dirname(__file__), "..")


# --- observation -----------------------------------------------------------------------

async def observe_channel(query_tool, channel: str) -> dict:
    """Reads this channel's real telemetry for both layers and decides incident vs healthy
    against that channel's OWN derived ceiling. No value is assumed; a layer whose metric
    cannot be read is reported unknown rather than healthy."""
    layers = {}
    for layer in registry.LAYERS:
        names = registry.metric_names(layer)
        job = registry.job_name(channel, layer)
        ceiling = registry.ceiling_for(channel, layer)

        fault_expr = f'{names["metric"]}{{job="{job}",mode="{names["fault_mode"]}"}}'
        flag_expr = f'{names["flag"]}{{job="{job}",mode="{names["fault_mode"]}"}}'

        value = _max_value(await _run_query(query_tool, DATASOURCE_UID, fault_expr))
        flag = _max_value(await _run_query(query_tool, DATASOURCE_UID, flag_expr))

        if value is None and flag is None:
            status, detail = "healthy_or_absent", (
                "no fault-mode series present -- this layer is either healthy "
                "(publishing under mode=baseline) or not running"
            )
        elif value is not None and value > ceiling:
            status, detail = "incident", (
                f"{names['metric']}={value:.4f}s exceeds this channel's derived ceiling "
                f"{ceiling}s"
            )
        elif flag == 1 and value is None:
            status, detail = "incident", (
                f"{names['flag']}=1 (magnitude not readable this cycle)"
            )
        else:
            status, detail = "healthy", (
                f"{names['metric']}={value}s within derived ceiling {ceiling}s"
            )

        layers[layer] = {
            "status": status, "detail": detail,
            "value_s": value, "flag": flag,
            "derived_ceiling_s": ceiling, "job": job,
            "metric": names["metric"],
        }

    return {
        "channel": channel,
        "criticality": registry.tier_provenance(channel),
        "layers": layers,
        "has_incident": any(l["status"] == "incident" for l in layers.values()),
        "incident_layers": [k for k, v in layers.items() if v["status"] == "incident"],
    }


def _max_value(data):
    vals = []
    for series in data or []:
        v = series.get("value")
        if v and len(v) >= 2:
            try:
                vals.append(float(v[1]))
            except (TypeError, ValueError):
                pass
    return max(vals) if vals else None


# --- allocation ------------------------------------------------------------------------

def allocate(observations: list, capacity: int) -> dict:
    """Priority allocation under real scarcity.

    Sorts contending channels by their OPERATOR-DECLARED criticality tier and gives the
    scarce backups to the highest-priority incidents. Ties break on channel id purely for
    determinism -- that is arbitrary and is recorded as arbitrary, not dressed up as
    judgement.
    """
    contending = [o for o in observations if o["has_incident"]]
    ordered = sorted(contending,
                     key=lambda o: (registry.tier_rank(o["channel"]), o["channel"]))

    protected = ordered[:capacity]
    degraded = ordered[capacity:]

    return {
        "contending_channels": [o["channel"] for o in ordered],
        "capacity": capacity,
        "contended": len(ordered) > capacity,
        "protected": [
            {
                "channel": o["channel"],
                "tier": o["criticality"]["criticality_tier"],
                "incident_layers": o["incident_layers"],
                "reason": (f"highest-priority contender: operator-declared tier "
                           f"'{o['criticality']['criticality_tier']}'"),
            } for o in protected
        ],
        "degraded": [
            {
                "channel": o["channel"],
                "tier": o["criticality"]["criticality_tier"],
                "incident_layers": o["incident_layers"],
                "status": "LEFT DEGRADED -- not protected, flagged for human",
                "reason": (f"lost contention for {capacity} backup(s) to "
                           f"{', '.join(p['channel'] for p in protected)}; this channel's "
                           f"operator-declared tier is "
                           f"'{o['criticality']['criticality_tier']}'"),
            } for o in degraded
        ],
        "signal_used": {
            "field": "criticality_tier",
            "source": "operator-declared",
            "measured_or_derived": False,
            "note": ("Tier is a facility policy input supplied by the operator and recorded "
                     "with its rationale, not a property this system measured. Ranking is "
                     "by tier only."),
            "declared": {o["channel"]: o["criticality"] for o in ordered},
        },
    }


def tradeoff_lines(alloc: dict) -> list:
    """The honest human-readable tradeoff record."""
    out = []
    for p in alloc["protected"]:
        out.append(f"PROTECTED  {p['channel']} · tier={p['tier']} · "
                   f"layers={','.join(p['incident_layers'])}")
    for d in alloc["degraded"]:
        out.append(f"DEGRADED   {d['channel']} · tier={d['tier']} · "
                   f"layers={','.join(d['incident_layers'])} · FLAGGED FOR HUMAN")
    if not alloc["degraded"] and alloc["protected"]:
        out.append("no contention: capacity covered every incident")
    return out


# --- execution -------------------------------------------------------------------------

def execute_protected(alloc: dict, authorized_by: str) -> list:
    """Executes failover for the protected channels ONLY.

    Each execution runs through failover_tool.failover() unchanged: it independently
    re-verifies that channel's own distinct backup and independently requires the human
    authorizer string. The supervisor decides WHO gets a backup; it does not weaken, bypass
    or re-implement any part of how a switch is authorized or verified.

    failover_tool resolves its channel from CHANGEOVER_CHANNEL at import time, so each
    channel is executed in its own subprocess with that variable set -- which also keeps
    each channel writing to its own per-channel state file.
    """
    import subprocess
    results = []
    for p in alloc["protected"]:
        ch = p["channel"]
        for layer in p["incident_layers"]:
            env = dict(os.environ, CHANGEOVER_CHANNEL=ch)
            reason = (f"contention allocation: protected as tier="
                      f"{p['tier']} over {len(alloc['degraded'])} degraded channel(s)")
            proc = subprocess.run(
                [sys.executable, os.path.join(ROOT, "agent", "failover_tool.py"),
                 layer, reason, authorized_by],
                capture_output=True, text=True, env=env,
            )
            ok = proc.returncode == 0
            results.append({
                "channel": ch, "layer": layer, "executed": ok,
                "authorized_by": authorized_by,
                "state_file": f"logs/feed_state_{ch}.json",
                "stdout": proc.stdout[-600:] if ok else None,
                "error": (proc.stderr or proc.stdout)[-400:] if not ok else None,
            })
    return results


# --- main ------------------------------------------------------------------------------

async def run(capacity: int, authorized_by: str | None, execute: bool) -> dict:
    chans = registry.available_channels()
    print(f"[SUPERVISOR] channels={chans} pool_capacity={capacity}")

    toolset, query_tool = await _get_query_tool(MCP_URL)
    try:
        observations = [await observe_channel(query_tool, ch) for ch in chans]
    finally:
        await toolset.close()

    for o in observations:
        mark = "INCIDENT" if o["has_incident"] else "ok      "
        print(f"[OBSERVE] {mark} {o['channel']:16s} "
              f"tier={o['criticality']['criticality_tier']:9s} "
              f"incidents={o['incident_layers'] or '-'}")
        for layer, l in o["layers"].items():
            print(f"           {layer:14s} {l['status']:18s} {l['detail']}")

    alloc = allocate(observations, capacity)

    print(f"\n[CONTENTION] contending={len(alloc['contending_channels'])} "
          f"capacity={capacity} contended={alloc['contended']}")
    for line in tradeoff_lines(alloc):
        print(f"  {line}")

    artifact = {
        "timestamp": time.time(),
        "phase": "Phase 2 -- contention supervisor",
        "pool_capacity_M": capacity,
        "channels_N": len(chans),
        "scarcity_is_real": capacity < len(alloc["contending_channels"]),
        "observations": observations,
        "allocation": alloc,
        "tradeoff_record": tradeoff_lines(alloc),
        "human_authorization": {
            "required": True,
            "what_is_authorized": ("the PRIORITIZATION -- which channel gets the scarce "
                                   "backup and which is left degraded"),
            "authorized_by": authorized_by,
            "granted": bool(authorized_by),
        },
        "scope_limits": [
            "Criticality tier is OPERATOR-DECLARED, not measured or derived.",
            "'sign_language' is feed-liveness on a stand-in feed, never a signer feed.",
            "Audio description is NOT instrumented.",
            "No rollback: a degraded channel stays degraded and flagged for a human.",
            "The supervisor allocates; failover_tool executes and independently "
            "re-verifies the backup and the human authorizer.",
        ],
    }

    if not alloc["contending_channels"]:
        print("\n[SUPERVISOR] no incidents -- nothing to arbitrate")
    elif not authorized_by:
        print("\n[SUPERVISOR] REFUSED: no human authorization for this prioritization. "
              "Allocation computed and recorded, but NOT executed.")
        artifact["execution"] = {"executed": False,
                                 "reason": "no human authorization for the prioritization"}
    elif not execute:
        print("\n[SUPERVISOR] authorization present but --execute not passed; "
              "allocation recorded, not executed.")
        artifact["execution"] = {"executed": False, "reason": "--execute not passed"}
    else:
        print(f"\n[SUPERVISOR] prioritization authorized by {authorized_by} -- executing "
              f"protected channel(s) only")
        artifact["execution"] = {"executed": True,
                                 "results": execute_protected(alloc, authorized_by)}
        for r in artifact["execution"]["results"]:
            print(f"  {'EXECUTED' if r['executed'] else 'FAILED  '} {r['channel']}/"
                  f"{r['layer']} -> {r['state_file']}"
                  + (f"  ({r['error']})" if r.get("error") else ""))

    return artifact


def main():
    ap = argparse.ArgumentParser(description="Changeover contention supervisor")
    ap.add_argument("--capacity", type=int, default=registry.BACKUP_POOL_CAPACITY,
                    help="shared backup pool size M (default from config/channels.py)")
    ap.add_argument("--authorize", metavar="HUMAN",
                    help="a real human authorizing THE PRIORITIZATION (not a switch)")
    ap.add_argument("--execute", action="store_true",
                    help="execute failover for the protected channel(s)")
    ap.add_argument("--label", default="contention",
                    help="artifact label -> logs/contention_<label>.json")
    args = ap.parse_args()

    artifact = asyncio.run(run(args.capacity, args.authorize, args.execute))

    out = os.path.join(ROOT, "logs", f"contention_{args.label}.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        json.dump(artifact, f, indent=2, default=str)
    print(f"\nartifact: {os.path.relpath(out, ROOT)}")


if __name__ == "__main__":
    main()
