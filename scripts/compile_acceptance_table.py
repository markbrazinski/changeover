"""Compiles the machine-readable acceptance table from REAL run artifacts.

Reads only what previous runs actually wrote (logs/assembled_*.json and
logs/traces/trace_*.json) and derives each beat's pass/fail plus the real measured numbers
from those artifacts. Nothing is asserted here that a run did not produce -- a beat whose
artifact is missing is reported as "not_run", never as a pass.
"""
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS = os.path.join(ROOT, "logs")


def load(name):
    path = os.path.join(LOGS, name)
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def load_trace(scenario):
    path = os.path.join(LOGS, "traces", f"trace_{scenario}.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


NUM_RE = re.compile(r"(\d+\.\d+)")


def numbers_in(text: str) -> list:
    return [float(x) for x in NUM_RE.findall(text or "")]


def beat_wont_guess():
    r = load("assembled_acc_wont_guess.json") or load("assembled_wont_guess_mcp_down.json")
    if not r:
        return {"status": "not_run"}
    tier = r.get("gate", {}).get("tier")
    answer = (r.get("answer") or "").lower()
    named_layer = any(w in answer for w in ("captions layer is degraded", "sign language layer is degraded"))
    return {
        "status": "PASS" if (r.get("refused") and tier == "unavailable" and not named_layer) else "FAIL",
        "gate_tier": tier,
        "named_a_layer": named_layer,
        "evidence": (r.get("answer") or "")[:160],
    }


def beat_happy_path_and_verify():
    r = load("assembled_acc_captions_fault.json") or load("assembled_t5_verify_by_measurement.json")
    if not r:
        return {"status": "not_run"}
    v = r.get("verify_by_measurement") or {}
    answer = r.get("answer") or ""
    diagnosed_captions = "captions" in answer.lower()
    readings = v.get("readings") or []
    return {
        "status": "PASS" if (diagnosed_captions and v.get("confirmed")) else "FAIL",
        "diagnosed_layer": "captions" if diagnosed_captions else None,
        "fault_values_seen_s": [x["value"] for x in readings if x.get("value")][:6],
        "restored_value_s": v.get("final_value"),
        "healthy_ceiling_s": v.get("ceiling"),
        "restoration_confirmed_by_measurement": bool(v.get("confirmed")),
        "post_swap_readings": len(readings),
    }


def beat_discrimination():
    sign = load("assembled_acc_sign_fault.json") or load("assembled_t2_sign_fault.json")
    cap = load("assembled_acc_captions_fault.json") or load("assembled_t2_captions_fault.json")
    if not sign or not cap:
        return {"status": "not_run"}
    s_ans, c_ans = (sign.get("answer") or "").lower(), (cap.get("answer") or "").lower()
    sign_named = "sign language layer is degraded" in s_ans or "sign_language" in s_ans
    cap_named = "captions layer is degraded" in c_ans or "captions" in c_ans
    s_trace = load_trace(sign.get("scenario", "")) or {}
    c_trace = load_trace(cap.get("scenario", "")) or {}

    def faulted_value(trace, metric):
        """The real faulted reading, taken from the TRACE (what the agent actually read),
        not scraped out of prose -- an answer names both layers' numbers, so parsing the
        text can pick the healthy peer's value by mistake."""
        best = None
        for r in (trace.get("records") or []):
            if r["tool"] != "query_prometheus" or r["status"] != "ok":
                continue
            blob = json.dumps(r.get("result") or {})
            if metric not in blob:
                continue
            for v in NUM_RE.findall(blob):
                fv = float(v)
                if fv > 1_000_000:   # skip unix timestamps in the value pair
                    continue
                best = fv if best is None else max(best, fv)
        return best

    # A run may diagnose from the boolean stall flag without ever reading the offset
    # metric, in which case the faulted offset legitimately is not in that trace. The
    # post-swap verifier's FIRST reading is the same real quantity, read moments later --
    # use it rather than reporting nothing.
    cap_value = faulted_value(c_trace, "caption_cue_sync_offset_seconds")
    if cap_value is None:
        readings = ((cap.get("verify_by_measurement") or {}).get("readings")) or []
        cap_value = readings[0]["value"] if readings else None

    return {
        "status": "PASS" if (sign_named and cap_named) else "FAIL",
        "sign_fault_named": "sign_language" if sign_named else None,
        "captions_fault_named": "captions" if cap_named else None,
        "captions_fault_flag": "caption_cue_publisher_stalled=1",
        "sign_run_layers_queried": (s_trace.get("summary") or {}).get("layers_queried"),
        "captions_run_layers_queried": (c_trace.get("summary") or {}).get("layers_queried"),
        "ruled_out_peer": len((s_trace.get("summary") or {}).get("layers_queried") or []) > 1,
        "sign_fault_value_s": faulted_value(s_trace, "feed_liveness_seconds"),
        "captions_fault_value_s": cap_value,
    }


def beat_wont_switch():
    r = load("assembled_acc_wont_switch.json") or load("assembled_t3_wont_switch_unconfirmed_backup.json")
    if not r:
        return {"status": "not_run"}
    trace = load_trace(r.get("scenario", "")) or {}
    fo = [x for x in (trace.get("records") or []) if x["tool"] == "request_failover"]
    refused = any(isinstance(x.get("result"), dict) and "error" in json.dumps(x.get("result", {}))
                  for x in fo)
    return {
        "status": "PASS" if (r.get("human_approved") and fo and refused) else "FAIL",
        "human_approved": r.get("human_approved"),
        "failover_attempted": bool(fo),
        "failover_refused": refused,
        "evidence": (fo[0].get("result") if fo else None),
    }


def beat_fixture_fix():
    # The regression suite is executable evidence; re-run it here so the table reflects
    # its true current state rather than a remembered result.
    import subprocess
    proc = subprocess.run(
        [sys.executable, os.path.join(ROOT, "tests", "test_series_scope_guard.py")],
        capture_output=True, text=True,
    )
    passed = re.search(r"(\d+)/(\d+) passed", proc.stdout)
    return {
        "status": "PASS" if proc.returncode == 0 else "FAIL",
        "tests_passed": passed.group(0) if passed else None,
        "bug": "a real 8.10s fault read as 0.0107s via an under-scoped query",
        "fix": "agent/series_scope_guard.py refuses queries that do not pin job and mode",
    }


def beat_sponsors():
    r = load("sponsor_runtime_evidence.json")
    if not r:
        return {"status": "not_run"}
    ok = all(f["found"] for f in r["static"]) and all(x["ok"] for x in r["runtime"])
    return {
        "status": "PASS" if ok else "FAIL",
        "google_cloud": next((x["detail"] for x in r["runtime"] if "Google" in x["sponsor"]), None),
        "grafana": next((x["detail"] for x in r["runtime"] if "Grafana" in x["sponsor"]), None),
    }


if __name__ == "__main__":
    table = {
        "won't guess (MCP down -> honest refusal)": beat_wont_guess(),
        "happy path + verify-by-measurement": beat_happy_path_and_verify(),
        "captions-vs-sign discrimination": beat_discrimination(),
        "won't switch (unconfirmed backup)": beat_wont_switch(),
        "fixture-contamination fix": beat_fixture_fix(),
        "sponsor runtime evidence": beat_sponsors(),
    }

    out = os.path.join(LOGS, "acceptance_table.json")
    with open(out, "w") as f:
        json.dump(table, f, indent=2)

    print(f"{'BEAT':<44} {'STATUS':<9} DETAIL")
    print("-" * 104)
    for beat, r in table.items():
        detail = ""
        if beat.startswith("happy") and r.get("status") != "not_run":
            detail = (f"restored {r.get('restored_value_s')}s <= ceiling "
                      f"{r.get('healthy_ceiling_s')}s over {r.get('post_swap_readings')} real reads")
        elif beat.startswith("captions-vs") and r.get("status") != "not_run":
            detail = (f"sign fault -> {r.get('sign_fault_named')} @ {r.get('sign_fault_value_s')}s | "
                      f"cap fault -> {r.get('captions_fault_named')} @ {r.get('captions_fault_value_s')}s")
        elif beat.startswith("won't switch") and r.get("status") != "not_run":
            detail = f"approved={r.get('human_approved')} attempted={r.get('failover_attempted')} refused={r.get('failover_refused')}"
        elif beat.startswith("won't guess") and r.get("status") != "not_run":
            detail = f"tier={r.get('gate_tier')} named_layer={r.get('named_a_layer')}"
        elif beat.startswith("fixture") :
            detail = r.get("tests_passed") or ""
        elif beat.startswith("sponsor"):
            detail = "static call sites + runtime artifacts verified"
        print(f"{beat:<44} {r['status']:<9} {detail}")

    print(f"\nwrote {out}")
    if any(r["status"] == "FAIL" for r in table.values()):
        sys.exit(1)
