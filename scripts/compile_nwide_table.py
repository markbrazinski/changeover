"""Compiles the N-wide acceptance table from REAL per-channel run artifacts.

Reads only what real runs wrote (logs/assembled_nwide_<channel>_<beat>.json and their
traces) and derives each beat's pass/fail from those artifacts. A beat with no artifact is
"not_run", never a pass.
"""
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS = os.path.join(ROOT, "logs")
sys.path.insert(0, os.path.join(ROOT, "config"))
import channels as registry  # noqa: E402

SHORT = {"tears_of_steel": "tos", "sintel": "sintel"}
NUM_RE = re.compile(r"(\d+\.\d+)")


def load(channel, beat):
    p = os.path.join(LOGS, f"assembled_nwide_{SHORT[channel]}_{beat}.json")
    if not os.path.exists(p):
        return None
    with open(p) as f:
        return json.load(f)


def trace(channel, beat):
    p = os.path.join(LOGS, "traces", f"trace_nwide_{SHORT[channel]}_{beat}.json")
    if not os.path.exists(p):
        return {}
    with open(p) as f:
        return json.load(f)


def beat_happy(ch):
    r = load(ch, "happy")
    if not r:
        return {"status": "not_run"}
    v = r.get("verify_by_measurement") or {}
    reads = v.get("readings") or []
    named = "captions" in (r.get("answer") or "").lower()
    return {
        "status": "PASS" if (named and v.get("confirmed")) else "FAIL",
        "channel_job": (r.get("channel_jobs") or {}).get("captions"),
        "fault_peak_s": max((x["value"] for x in reads if x.get("value")), default=None),
        "restored_s": v.get("final_value"),
        "derived_ceiling_s": v.get("ceiling"),
        "post_swap_reads": len(reads),
    }


def beat_discrim(ch):
    r = load(ch, "discrim")
    if not r:
        return {"status": "not_run"}
    ans = (r.get("answer") or "").lower()

    # Match on MEANING, not one exact phrasing. The model varies wording ("the sign
    # language layer is degraded" vs "the sign language layer, however, is degraded"), and
    # an over-strict matcher scored a genuinely correct run as FAIL. Require the layer name
    # and a degraded/healthy verdict within the same sentence, so the check stays honest
    # without being brittle.
    def verdict(layer_words, verdict_words):
        for sentence in re.split(r"[.\n]", ans):
            if any(w in sentence for w in layer_words) and \
               any(v in sentence for v in verdict_words):
                return True
        return False

    DEGRADED = ("degraded", "is frozen", "has stopped", "stalled")
    HEALTHY = ("healthy", "nominal", "no degradation", "operating normally")
    named_sign = verdict(("sign language", "sign_language"), DEGRADED)
    ruled_out_cap = verdict(("captions", "caption"), HEALTHY)
    nums = [float(x) for x in NUM_RE.findall(r.get("answer") or "")]
    return {
        "status": "PASS" if (named_sign and ruled_out_cap) else "FAIL",
        "named": "sign_language" if named_sign else None,
        "ruled_out": "captions" if ruled_out_cap else None,
        "values_s": nums[:3],
        "channel_job": (r.get("channel_jobs") or {}).get("sign_language"),
    }


def beat_wont_guess(ch):
    r = load(ch, "wontguess")
    if not r:
        return {"status": "not_run"}
    ans = (r.get("answer") or "").lower()
    return {
        "status": "PASS" if (r.get("refused") and r["gate"]["tier"] == "unavailable"
                             and "layer is degraded" not in ans) else "FAIL",
        "tier": r["gate"]["tier"],
        "named_a_layer": "layer is degraded" in ans,
    }


def beat_wont_switch(ch):
    r = load(ch, "wontswitch")
    if not r:
        return {"status": "not_run"}
    t = trace(ch, "wontswitch")
    fo = [x for x in (t.get("records") or []) if x["tool"] == "request_failover"]
    refused = any("error" in json.dumps(x.get("result") or {}) for x in fo)
    return {
        "status": "PASS" if (r.get("human_approved") and fo and refused) else "FAIL",
        "human_approved": r.get("human_approved"),
        "attempted": bool(fo),
        "refused": refused,
    }


BEATS = [
    ("happy path + verify-by-measurement", beat_happy),
    ("captions-vs-feed-liveness discrimination", beat_discrim),
    ("won't guess (MCP down)", beat_wont_guess),
    ("won't switch (unconfirmed backup)", beat_wont_switch),
]

if __name__ == "__main__":
    chans = registry.available_channels()
    table = {"channels": {}, "films": {}}

    for ch in chans:
        meta = registry.CHANNELS[ch]
        table["films"][ch] = {
            "title": meta["title"],
            "license": meta["license"],
            "license_verified_in_file": meta["license_verified_in_file"],
            "program": os.path.relpath(registry.program_path(ch), ROOT),
            "backup": os.path.relpath(registry.backup_path(ch), ROOT),
            "ceilings": {l: registry.ceiling_for(ch, l) for l in registry.LAYERS},
        }
        table["channels"][ch] = {name: fn(ch) for name, fn in BEATS}

    out = os.path.join(LOGS, "nwide_acceptance_table.json")
    with open(out, "w") as f:
        json.dump(table, f, indent=2)

    print(f"{'BEAT':<44} " + " ".join(f"{c:<26}" for c in chans))
    print("-" * (44 + 27 * len(chans)))
    for name, _ in BEATS:
        row = f"{name:<44} "
        for ch in chans:
            r = table["channels"][ch][name]
            detail = ""
            if name.startswith("happy") and r["status"] != "not_run":
                detail = f"{r['restored_s']}<={r['derived_ceiling_s']}"
            elif name.startswith("captions-vs") and r["status"] != "not_run":
                detail = f"->{r['named']}"
            elif name.startswith("won't guess") and r["status"] != "not_run":
                detail = f"tier={r['tier']}"
            elif name.startswith("won't switch") and r["status"] != "not_run":
                detail = "approved+refused"
            row += f"{r['status']:<5}{detail:<21}"
        print(row)

    print()
    for ch in chans:
        f_ = table["films"][ch]
        v = "verified in file" if f_["license_verified_in_file"] else "asserted, not in file"
        print(f"  {ch}: {f_['title']} | {f_['license']} ({v})")
        print(f"      ceilings: " + ", ".join(f"{k}={v2}s" for k, v2 in f_["ceilings"].items()))

    print(f"\nwrote {os.path.relpath(out, ROOT)}")
    fails = [1 for ch in chans for n, _ in BEATS
             if table["channels"][ch][n]["status"] != "PASS"]
    if fails:
        sys.exit(1)
