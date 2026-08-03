"""Phase-1 acceptance artifact: 5 behaviours x N channels, from REAL run artifacts only.

Writes logs/acceptance_nwide.json (machine-readable) and renders a readable table.

Every cell's number is read out of that channel's own run artifacts -- the agent result
JSON and its tool-call trace. Nothing is transcribed from a previous report, and no figure
is frozen into this file: re-running the channels and re-running this script is what
changes the numbers. A behaviour with no artifact is reported "not_run", never PASS.

The five behaviours are kept SEPARATE, including the two that a single run produces:
  * "happy path" asks: did the agent diagnose the right layer and did a human-authorized
    swap actually execute and get recorded?
  * "verify-by-measurement" asks: was the restored state then EARNED by a real post-swap
    reading crossing this channel's DERIVED ceiling?
A swap can execute and verification still fail (it did, during development, when the feed
never recovered). Collapsing them would hide that, so they are scored independently off
the same run's evidence.
"""
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS = os.path.join(ROOT, "logs")
sys.path.insert(0, os.path.join(ROOT, "config"))
import channels as registry  # noqa: E402

# Run artifacts are named by a short channel token.
SHORT = {"tears_of_steel": "tos", "sintel": "sintel"}
NUM_RE = re.compile(r"(\d+\.\d+)")


def _load(channel, beat):
    p = os.path.join(LOGS, f"assembled_nwide_{SHORT.get(channel, channel)}_{beat}.json")
    if not os.path.exists(p):
        return None
    with open(p) as f:
        return json.load(f)


def _trace(channel, beat):
    p = os.path.join(LOGS, "traces",
                     f"trace_nwide_{SHORT.get(channel, channel)}_{beat}.json")
    if not os.path.exists(p):
        return {}
    with open(p) as f:
        return json.load(f)


def _sentence_verdict(answer: str, layer_words, verdict_words) -> bool:
    """Match on MEANING within a sentence, not one exact phrasing -- the model varies
    wording and an over-strict matcher once scored a genuinely correct run as FAIL."""
    for sentence in re.split(r"[.\n]", (answer or "").lower()):
        if any(w in sentence for w in layer_words) and \
           any(v in sentence for v in verdict_words):
            return True
    return False


DEGRADED = ("degraded", "is frozen", "has stopped", "stalled")
HEALTHY = ("healthy", "nominal", "no degradation", "operating normally")


def _metric_value_from_trace(trace: dict, metric: str):
    """The real value the agent actually READ for a named metric, taken from the recorded
    query results. Returns None when that metric was never successfully queried in the run
    -- reporting None is correct there; inventing a number is not."""
    best = None
    for rec in (trace.get("records") or []):
        if rec.get("tool") != "query_prometheus" or rec.get("status") != "ok":
            continue
        result = rec.get("result") or {}
        for series in (result.get("data") or []):
            if (series.get("metric") or {}).get("__name__") != metric:
                continue
            v = series.get("value")
            if v and len(v) >= 2:
                try:
                    fv = float(v[1])
                except (TypeError, ValueError):
                    continue
                best = fv if best is None else max(best, fv)
    return best


def behaviour_happy_path(ch):
    """Diagnosed the faulted layer AND a human-authorized swap really executed."""
    r = _load(ch, "happy")
    if not r:
        return {"status": "not_run"}
    t = _trace(ch, "happy")
    fo = [x for x in (t.get("records") or []) if x["tool"] == "request_failover"]
    executed = any(
        (isinstance(x.get("result"), dict) and x["result"].get("status") == "executed")
        for x in fo
    )
    named_captions = _sentence_verdict(r.get("answer"), ("captions", "caption"), DEGRADED)

    # The value the agent actually read when it decided to fail over, taken from the trace.
    # Falls back to the first post-swap reading only if the magnitude was never queried
    # (diagnosis from the stall flag alone), and says which it is.
    diagnosed_value = _metric_value_from_trace(t, "caption_cue_sync_offset_seconds")
    reads = (r.get("verify_by_measurement") or {}).get("readings") or []
    first_post_swap = next((x["value"] for x in reads if x.get("value") is not None), None)
    stall_flag = _metric_value_from_trace(t, "caption_cue_publisher_stalled")

    return {
        "status": "PASS" if (named_captions and executed and r.get("human_approved")) else "FAIL",
        "diagnosed_layer": "captions" if named_captions else None,
        "human_approved": r.get("human_approved"),
        "swap_executed": executed,
        "fault_value_at_diagnosis_s": diagnosed_value,
        "fault_flag_publisher_stalled": stall_flag,
        "diagnosed_from": ("caption_cue_sync_offset_seconds magnitude"
                           if diagnosed_value is not None
                           else "caption_cue_publisher_stalled flag (magnitude not queried)"),
        "first_post_swap_reading_s": first_post_swap,
        "job_queried": (r.get("channel_jobs") or {}).get("captions"),
        "evidence": f"logs/assembled_nwide_{SHORT.get(ch, ch)}_happy.json",
    }


def behaviour_verify_by_measurement(ch):
    """Restoration EARNED by a real post-swap reading crossing the DERIVED ceiling."""
    r = _load(ch, "happy")
    if not r:
        return {"status": "not_run"}
    v = r.get("verify_by_measurement") or {}
    reads = [x["value"] for x in (v.get("readings") or []) if x.get("value") is not None]
    return {
        "status": "PASS" if v.get("confirmed") else "FAIL",
        "post_swap_readings_s": reads,
        "restored_value_s": v.get("final_value"),
        "derived_ceiling_s": v.get("ceiling"),
        "n_real_reads": len(reads),
        "detail": v.get("detail"),
        "evidence": f"logs/assembled_nwide_{SHORT.get(ch, ch)}_happy.json",
    }


def behaviour_discrimination(ch):
    """Feed-liveness faulted while captions healthy -> names sign_language, rules out captions.

    NOTE: this discriminates two DIFFERENT PHYSICAL QUANTITIES across two layers
    (caption-cue-sync vs feed-liveness). It is NOT a claim about two independent
    accessibility layers -- feed-liveness runs on a stand-in feed, never a signer feed."""
    r = _load(ch, "discrim")
    if not r:
        return {"status": "not_run"}
    ans = r.get("answer") or ""
    named_sign = _sentence_verdict(ans, ("sign language", "sign_language"), DEGRADED)
    ruled_out = _sentence_verdict(ans, ("captions", "caption"), HEALTHY)

    # Read the per-metric values out of the TRACE, not out of prose. An answer names BOTH
    # layers' numbers, so scraping text and taking max/min can report the healthy peer's
    # value as the faulted one -- it did, for Sintel.
    faulted = _metric_value_from_trace(_trace(ch, "discrim"), "feed_liveness_seconds")
    healthy = _metric_value_from_trace(_trace(ch, "discrim"),
                                       "caption_cue_sync_offset_seconds")
    flag = _metric_value_from_trace(_trace(ch, "discrim"), "feed_frozen")

    return {
        "status": "PASS" if (named_sign and ruled_out) else "FAIL",
        "named_faulted_layer": "sign_language" if named_sign else None,
        "ruled_out_layer": "captions" if ruled_out else None,
        # May be None when the agent diagnosed from the boolean flag alone without ever
        # reading the magnitude -- a legitimate diagnosis path, so the flag is reported
        # instead of inventing a number.
        "faulted_value_s": faulted,
        "faulted_flag_feed_frozen": flag,
        "diagnosed_from": ("feed_liveness_seconds magnitude" if faulted is not None
                           else "feed_frozen boolean flag (magnitude not queried this run)"),
        "healthy_peer_value_s": healthy,
        "job_queried": (r.get("channel_jobs") or {}).get("sign_language"),
        "evidence": f"logs/assembled_nwide_{SHORT.get(ch, ch)}_discrim.json",
    }


def behaviour_wont_switch(ch):
    """Human approved and layer really faulted, but the backup could not be confirmed ->
    the switch is refused and NO state is written."""
    r = _load(ch, "wontswitch")
    if not r:
        return {"status": "not_run"}
    t = _trace(ch, "wontswitch")
    fo = [x for x in (t.get("records") or []) if x["tool"] == "request_failover"]
    refused = any("error" in json.dumps(x.get("result") or {}) for x in fo)
    err = next((x["result"].get("error") for x in fo
                if isinstance(x.get("result"), dict) and x["result"].get("error")), None)
    return {
        "status": "PASS" if (r.get("human_approved") and fo and refused) else "FAIL",
        "human_approved": r.get("human_approved"),
        "failover_attempted": bool(fo),
        "failover_refused": refused,
        "refusal_reason": err,
        "evidence": f"logs/traces/trace_nwide_{SHORT.get(ch, ch)}_wontswitch.json",
    }


def behaviour_wont_guess(ch):
    """Grafana MCP unreachable -> honest refusal, no layer named, no fabricated value."""
    r = _load(ch, "wontguess")
    if not r:
        return {"status": "not_run"}
    ans = (r.get("answer") or "").lower()
    named = ("layer is degraded" in ans) or ("layer is healthy" in ans)
    t = _trace(ch, "wontguess")
    return {
        "status": "PASS" if (r.get("refused") and r["gate"]["tier"] == "unavailable"
                             and not named) else "FAIL",
        "gate_tier": r["gate"]["tier"],
        "named_a_layer": named,
        "tool_calls_made": len(t.get("records") or []),
        "refusal": (r.get("answer") or "")[:160],
        "evidence": f"logs/assembled_nwide_{SHORT.get(ch, ch)}_wontguess.json",
    }


BEHAVIOURS = [
    ("happy path", behaviour_happy_path),
    ("captions-vs-feed-liveness discrimination", behaviour_discrimination),
    ("won't switch (unconfirmed backup)", behaviour_wont_switch),
    ("won't guess (MCP down)", behaviour_wont_guess),
    ("verify-by-measurement", behaviour_verify_by_measurement),
]


def channel_provenance(ch: str) -> dict:
    meta = registry.CHANNELS[ch]
    ceilings = registry.load_ceilings()["channels"][ch]
    vtt = registry.vtt_path(ch)

    cue_count, first_cue, last_cue = None, None, None
    if os.path.exists(vtt):
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        from caption_cue_with_telemetry import parse_vtt_cues
        cues = parse_vtt_cues(vtt)
        cue_count, first_cue, last_cue = len(cues), cues[0][0], cues[-1][0]

    return {
        "film": {
            "title": meta["title"],
            "license": meta["license"],
            "license_verified_in_file": meta["license_verified_in_file"],
            "license_evidence": meta["license_evidence"],
            "source": meta["source"],
            "note": meta["note"],
            "path": os.path.relpath(registry.program_path(ch), ROOT),
        },
        "caption_sidecar": {
            "path": os.path.relpath(vtt, ROOT),
            "cue_count": cue_count,
            "first_cue_s": first_cue,
            "last_cue_s": last_cue,
            "cue_text": "authored placeholder describing the monitoring rig -- NOT a "
                        "transcript; neither film ships subtitles",
            "cue_timing": "real monotonic ~2.002s cadence spanning the film's real "
                          "ffprobe duration; this is the only thing the metric reads",
        },
        "distinct_backup": {
            "path": os.path.relpath(registry.backup_path(ch), ROOT),
            "distinct_from_primary": (registry.backup_path(ch)
                                      != registry.program_path(ch)),
            "cut_from": "the OTHER channel's film -- no channel's backup shares a source "
                        "file with its own primary",
        },
        "derived_ceilings": {
            layer: {
                "ceiling_s": ceilings[layer]["ceiling"],
                "observed_max_s": ceilings[layer]["observed_max"],
                "observed_min_s": ceilings[layer]["observed_min"],
                "n_baseline_samples": ceilings[layer]["n"],
                "safety_factor": ceilings[layer]["safety_factor"],
                "rule": ceilings[layer]["rule"],
                "derivation": (
                    "measured by scripts/derive_ceilings.py running this channel's real "
                    "producers against its real film; max-times-factor rather than a "
                    "percentile because the offset is a sawtooth whose max IS the physical "
                    "bound (one cue interval), so a percentile would clip legitimate peaks"
                ),
                "hand_set": False,
            }
            for layer in registry.LAYERS
        },
        "prometheus_jobs": {l: registry.job_name(ch, l) for l in registry.LAYERS},
    }


def build() -> dict:
    chans = registry.available_channels()
    return {
        "phase": "Phase 1 -- generalization (N-instancing). Supervisor/contention is "
                 "Phase 2 and is NOT covered here.",
        "claim": "The SAME assembled agent, instanced per channel via CHANGEOVER_CHANNEL, "
                 "clears the full behaviour set on each channel's own real film and its "
                 "own distinct real backup. No agent logic branches on the channel.",
        "scope_limits": [
            "'sign_language' is FEED LIVENESS on a stand-in feed, never a signer feed.",
            "Discrimination is between two different PHYSICAL QUANTITIES across two "
            "layers -- not two independent accessibility layers.",
            "Audio description is NOT instrumented; no AD metric or diagnosis exists.",
            "failover() toggles primary<->backup and was not refactored.",
            "No rollback / post-switch-failure state.",
        ],
        "channels": {ch: channel_provenance(ch) for ch in chans},
        "behaviours": {
            ch: {name: fn(ch) for name, fn in BEHAVIOURS} for ch in chans
        },
    }


def render(table: dict):
    chans = list(table["channels"])
    w = 30
    print()
    print("N-WIDE ACCEPTANCE  --  5 behaviours x " + str(len(chans)) + " channels")
    print("=" * (46 + w * len(chans)))
    print(f"{'BEHAVIOUR':<44}  " + "".join(f"{c:<{w}}" for c in chans))
    print("-" * (46 + w * len(chans)))

    for name, _ in BEHAVIOURS:
        row = f"{name:<44}  "
        for ch in chans:
            r = table["behaviours"][ch][name]
            s = r["status"]
            if name == "happy path":
                fv = r.get("fault_value_at_diagnosis_s")
                d = (f"cap @{fv}s->swap" if fv is not None
                     else "cap @flag=1->swap")
            elif name.startswith("captions-vs"):
                fv = r.get("faulted_value_s")
                d = f"sign @{fv}s" if fv is not None else "sign @flag=1"
            elif name.startswith("won't switch"):
                d = "approved+refused"
            elif name.startswith("won't guess"):
                d = f"tier={r.get('gate_tier')}"
            else:
                d = f"{r.get('restored_value_s')}<={r.get('derived_ceiling_s')}"
            row += f"{s + '  ' + d:<{w}}"
        print(row)

    print()
    for ch in chans:
        p = table["channels"][ch]
        f_, s_, b_ = p["film"], p["caption_sidecar"], p["distinct_backup"]
        v = "verified in file" if f_["license_verified_in_file"] else "ASSERTED, not in file"
        print(f"  {ch}")
        print(f"    film     : {f_['title']}  [{f_['license']} -- {v}]")
        print(f"    sidecar  : {s_['cue_count']} real cues, {s_['first_cue_s']}s -> {s_['last_cue_s']}s")
        print(f"    backup   : {b_['path']}  (distinct from primary: {b_['distinct_from_primary']})")
        for layer, c in p["derived_ceilings"].items():
            print(f"    ceiling  : {layer:14s} {c['ceiling_s']}s "
                  f"= max {c['observed_max_s']}s x {c['safety_factor']} "
                  f"(n={c['n_baseline_samples']}, hand_set={c['hand_set']})")
        print()


if __name__ == "__main__":
    table = build()
    out = os.path.join(LOGS, "acceptance_nwide.json")
    with open(out, "w") as f:
        json.dump(table, f, indent=2)
    render(table)
    print(f"wrote {os.path.relpath(out, ROOT)}")

    failed = [(ch, n) for ch in table["behaviours"]
              for n, r in table["behaviours"][ch].items() if r["status"] != "PASS"]
    if failed:
        print("\nFAILED cells:")
        for ch, n in failed:
            print(f"  {ch}: {n} -> {table['behaviours'][ch][n]['status']}")
        sys.exit(1)
    total = len(table["behaviours"]) * len(BEHAVIOURS)
    print(f"\n{total}/{total} cells PASS across {len(table['behaviours'])} channels")
