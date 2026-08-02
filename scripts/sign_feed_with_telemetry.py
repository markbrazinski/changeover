"""Gate 4 (fault 2/3): real ffmpeg sign-language feed at live pacing (-re), with a real
freeze fault -- the ffmpeg process is genuinely killed mid-stream (SIGKILL, not a synthetic
number) and NOT restarted. A sampler thread independently polls "how long since the last
real frame arrived" on a fixed wall-clock interval, decoupled from frame arrival itself --
that decoupling is what makes the freeze gap a real measured value instead of trivially ~0.
"""
import re
import subprocess
import sys
import threading
import time
import urllib.request

ROOT = "/Users/markbrazinski/Desktop/coding fun/Changeover"
SRC = f"{ROOT}/fixtures/source.mp4"
PUSHGATEWAY = "http://localhost:9091"
JOB = "media_pipeline_sign"

PROGRESS_RE = re.compile(r"out_time_ms=(\d+)")


def push_sample(freshness_seconds: float, mode: str, sample_idx: int, frozen: int = 0):
    # Heartbeat: this exporter's own wall-clock time at push -- decoupled from the domain
    # value (freshness/frozen), so the evidence-quality gate (agent/evidence_gate.py) can
    # tell "exporter is alive and reporting a real fault" apart from "exporter itself has
    # stopped pushing" without ever having to interpret what the domain value means.
    body = (
        f"# HELP sign_feed_freshness_seconds Seconds since the last real frame was produced\n"
        f"# TYPE sign_feed_freshness_seconds gauge\n"
        f'sign_feed_freshness_seconds{{layer="sign_language",mode="{mode}"}} {freshness_seconds:.4f}\n'
        f"# HELP sign_feed_frozen Whether the feed process has stopped producing frames (real state)\n"
        f"# TYPE sign_feed_frozen gauge\n"
        f'sign_feed_frozen{{layer="sign_language",mode="{mode}"}} {frozen}\n'
        f"# HELP {JOB}_heartbeat_unix_time Exporter's own wall-clock time at last real push\n"
        f"# TYPE {JOB}_heartbeat_unix_time gauge\n"
        f'{JOB}_heartbeat_unix_time{{layer="sign_language"}} {time.time()}\n'
    )
    req = urllib.request.Request(
        f"{PUSHGATEWAY}/metrics/job/{JOB}/mode/{mode}", data=body.encode(), method="POST"
    )
    urllib.request.urlopen(req)
    print(f"  sample {sample_idx}: sign_feed_freshness_seconds={freshness_seconds:.3f} frozen={frozen}")


def run_live_paced(start: float, duration: float, out_path: str):
    cmd = [
        "ffmpeg", "-y", "-re",
        "-ss", str(start), "-i", SRC,
        "-t", str(duration),
        "-c:v", "libx264", "-an",
        "-progress", "pipe:1", "-nostats",
        out_path,
    ]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)


class LastFrameTracker:
    """Shared, thread-safe timestamp of when the most recent real frame arrived."""
    def __init__(self):
        self.last_frame_time = time.time()
        self.lock = threading.Lock()

    def mark_frame(self):
        with self.lock:
            self.last_frame_time = time.time()

    def freshness(self) -> float:
        with self.lock:
            return time.time() - self.last_frame_time


def frame_reader(proc, tracker: LastFrameTracker):
    for line in proc.stdout:
        if PROGRESS_RE.search(line):
            tracker.mark_frame()


def sample_loop(tracker: LastFrameTracker, mode: str, interval_s: float, count: int, frozen_after=None):
    for idx in range(count):
        time.sleep(interval_s)
        frozen = 1 if frozen_after is not None and idx >= frozen_after else 0
        push_sample(tracker.freshness(), mode, idx, frozen=frozen)


def baseline():
    print("=== BASELINE: sign feed runs continuously, sampler polls independently ===")
    tracker = LastFrameTracker()
    proc = run_live_paced(0, 12, f"{ROOT}/fixtures/sign_baseline.mp4")
    reader = threading.Thread(target=frame_reader, args=(proc, tracker), daemon=True)
    reader.start()
    sample_loop(tracker, "baseline", interval_s=1.0, count=10)
    proc.wait()
    reader.join(timeout=2)


def frozen_fault():
    print("=== FAULT: sign feed process genuinely killed mid-stream (real SIGKILL, no restart) ===")
    tracker = LastFrameTracker()
    proc = run_live_paced(0, 30, f"{ROOT}/fixtures/sign_frozen.mp4")
    reader = threading.Thread(target=frame_reader, args=(proc, tracker), daemon=True)
    reader.start()

    # Let it run live for a few real seconds first (healthy baseline window).
    for idx in range(4):
        time.sleep(1.0)
        push_sample(tracker.freshness(), "frozen", idx, frozen=0)

    print("  -- killing sign feed process now (real SIGKILL) --")
    proc.kill()

    # No new frames will ever arrive from this dead process. The sampler keeps polling
    # on its own clock; freshness grows for real because mark_frame() never fires again.
    for idx in range(4, 12):
        time.sleep(1.0)
        push_sample(tracker.freshness(), "frozen", idx, frozen=1)

    proc.wait()
    reader.join(timeout=2)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "baseline"
    if mode == "baseline":
        baseline()
    elif mode == "frozen":
        frozen_fault()
    else:
        print("usage: python sign_feed_with_telemetry.py <baseline|frozen>")
        sys.exit(1)
