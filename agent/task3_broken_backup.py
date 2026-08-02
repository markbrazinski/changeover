"""Task 3: reachable-but-broken backup refusal, frozen-bar style (5 runs per condition).

healthy -- backup file passes ffprobe AND its own telemetry shows no fault -> must PASS
           the health check (failover proceeds).
broken  -- backup file passes ffprobe BUT its own telemetry shows a real fault (elevated
           freshness) -> must FAIL the health check (failover refused), proving the
           general "won't route onto another broken feed" claim, not just the narrower
           unconfirmed-backup case already covered.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from failover_tool import verify_backup_feed_healthy, failover

ROOT = os.path.join(os.path.dirname(__file__), "..")


def run(condition: str, run_idx: int):
    healthy = verify_backup_feed_healthy("sign_language")
    refused = False
    error = None
    try:
        failover("sign_language", f"task3 {condition} test run {run_idx}", "mark@brazinski.us")
    except RuntimeError as e:
        refused = True
        error = str(e)

    if condition == "healthy":
        clean_pass = healthy and not refused
    else:  # broken
        clean_pass = (not healthy) and refused

    result = {
        "condition": condition, "run": run_idx,
        "health_check_result": healthy, "failover_refused": refused,
        "error": error, "clean_pass": clean_pass,
    }
    out_path = os.path.join(ROOT, "logs", f"task3_{condition}_run{run_idx}.json")
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2)
    print(f"=== TASK3 {condition} run {run_idx} === clean_pass={clean_pass} health_check={healthy} refused={refused}")
    return result


if __name__ == "__main__":
    condition = sys.argv[1]
    run_idx = int(sys.argv[2])
    run(condition, run_idx)
