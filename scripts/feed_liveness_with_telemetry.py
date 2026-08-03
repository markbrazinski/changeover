"""Feed-liveness producer for a STAND-IN feed carrying the sign_language layer label.

WHAT THIS HONESTLY MEASURES -- read this before using the metric
----------------------------------------------------------------
`feed_liveness_seconds` = wall-clock seconds since the monitored feed process last
produced a frame. A reader thread marks frame arrivals from real ffmpeg `-progress`
output; an INDEPENDENT sampler thread polls on its own clock. That decoupling is what
makes a freeze a real measured quantity rather than trivially zero: when the process is
killed, `mark_frame()` never fires again, so the gap grows for real.

WHAT IT IS NOT
--------------
This is NOT a sign-language-specific measurement, and nothing here should be described as
one. There is no sign-language content in this repository. The monitored feed is a second
ffmpeg process transcoding the SAME program video (`fixtures/source.mp4`) that the captions
path uses -- a STAND-IN feed. What is genuinely measured is feed liveness: "is this feed
process still delivering frames?" That is a real physical quantity, truthfully reported.
What it is not is anything specific to signing.

This naming is deliberate. The predecessor metric `caption_sync_offset_seconds` turned out
to be encoder drift wearing a caption name, and `sign_feed_freshness_seconds` was feed
liveness wearing a sign name. Both were renamed rather than left to imply more than they
measure. See README.md.

The `layer="sign_language"` label is retained ONLY because it is the routing key the
failover path and the proven backup-health checks already key on. It denotes which layer
slot this stand-in feed occupies, not that the content is signing.

Modes:
  baseline -- feed runs continuously; liveness stays near zero.
  frozen   -- the feed process is genuinely SIGKILLed mid-stream and never restarted;
              liveness climbs for real from that moment.
"""
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PUSHGATEWAY = "http://localhost:9091"
LAYER = "sign_language"

# Channel selection -- see the note in scripts/caption_cue_with_telemetry.py. Each channel
# monitors ITS OWN film, so the stand-in feeds are genuinely distinct across channels.
CHANNEL = os.environ.get("CHANGEOVER_CHANNEL")
if CHANNEL:
    sys.path.insert(0, os.path.join(ROOT, "config"))
    import channels as _ch
    SRC = _ch.program_path(CHANNEL)
    JOB = _ch.job_name(CHANNEL, "sign_language")
else:
    SRC = os.path.join(ROOT, "fixtures", "source.mp4")
    JOB = "media_pipeline_feed_liveness"

PROGRESS_RE = re.compile(r"out_time_ms=(\d+)")


def push_sample(liveness_seconds: float, mode: str, sample_idx: int, frozen: int = 0):
    # Heartbeat is this exporter's own wall clock at push time, decoupled from the domain
    # value, so agent/evidence_gate.py can distinguish "exporter alive, reporting a real
    # fault" from "exporter itself died" without interpreting the domain value.
    body = (
        f"# HELP feed_liveness_seconds Seconds since the monitored feed process last produced a frame\n"
        f"# TYPE feed_liveness_seconds gauge\n"
        f'feed_liveness_seconds{{layer="{LAYER}",mode="{mode}"}} {liveness_seconds:.4f}\n'
        f"# HELP feed_frozen Whether the feed process has stopped producing frames (real state)\n"
        f"# TYPE feed_frozen gauge\n"
        f'feed_frozen{{layer="{LAYER}",mode="{mode}"}} {frozen}\n'
        f"# HELP {JOB}_heartbeat_unix_time Exporter's own wall-clock time at last real push\n"
        f"# TYPE {JOB}_heartbeat_unix_time gauge\n"
        f'{JOB}_heartbeat_unix_time{{layer="{LAYER}"}} {time.time()}\n'
    )
    req = urllib.request.Request(
        f"{PUSHGATEWAY}/metrics/job/{JOB}/mode/{mode}", data=body.encode(), method="POST"
    )
    urllib.request.urlopen(req)
    print(f"  sample {sample_idx}: feed_liveness_seconds={liveness_seconds:.3f} frozen={frozen}")


def run_live_paced(start: float, duration: float, out_path: str):
    """-re paces ffmpeg at native wall-clock frame rate, so frame arrival is a real-time
    event rather than 'as fast as the CPU allows'."""
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
    """Thread-safe timestamp of when the most recent real frame arrived."""

    def __init__(self):
        self.last_frame_time = time.time()
        self.lock = threading.Lock()

    def mark_frame(self):
        with self.lock:
            self.last_frame_time = time.time()

    def liveness(self) -> float:
        with self.lock:
            return time.time() - self.last_frame_time


def frame_reader(proc, tracker: LastFrameTracker):
    for line in proc.stdout:
        if PROGRESS_RE.search(line):
            tracker.mark_frame()


def baseline():
    print("=== BASELINE: stand-in feed runs continuously, sampler polls independently ===")
    tracker = LastFrameTracker()
    out = os.path.join(ROOT, "fixtures", "liveness_baseline.mp4")
    proc = run_live_paced(0, 40, out)
    reader = threading.Thread(target=frame_reader, args=(proc, tracker), daemon=True)
    reader.start()
    for idx in range(8):
        time.sleep(5.0)
        push_sample(tracker.liveness(), "baseline", idx, frozen=0)
    proc.kill()
    reader.join(timeout=2)


def hold_healthy(seconds: float, interval_s: float = 5.0):
    """Keeps a HEALTHY liveness exporter alive and pushing for `seconds`.

    Needed whenever this layer is the peer being ruled out: a finite baseline run that has
    exited is a genuinely DEAD exporter, and agent/evidence_gate.py refuses on it (tier=
    stale) before any diagnosis happens -- which tests the gate rather than the behaviour
    under test. The feed keeps really running, so the liveness value stays at its real
    healthy level; nothing is held constant or asserted.
    """
    tracker = LastFrameTracker()
    out = os.path.join(ROOT, "fixtures", ".liveness_healthy_probe.mp4")
    proc = run_live_paced(0, seconds + 15, out)
    reader = threading.Thread(target=frame_reader, args=(proc, tracker), daemon=True)
    reader.start()

    print(f"  holding HEALTHY liveness exporter open for {seconds:.0f}s")
    idx = 0
    deadline = time.time() + seconds
    while time.time() < deadline:
        time.sleep(interval_s)
        # If the film runs out, restart the feed so it stays genuinely healthy rather than
        # drifting into a fault we did not intend to inject.
        if proc.poll() is not None:
            proc = run_live_paced(0, seconds + 15, out)
            reader = threading.Thread(target=frame_reader, args=(proc, tracker), daemon=True)
            reader.start()
            continue
        push_sample(tracker.liveness(), "baseline", idx, frozen=0)
        idx += 1

    proc.kill()
    reader.join(timeout=2)


def frozen_fault(hold_seconds: float = 0.0):
    print("=== FAULT: stand-in feed process genuinely killed mid-stream (real SIGKILL) ===")
    tracker = LastFrameTracker()
    out = os.path.join(ROOT, "fixtures", "liveness_frozen.mp4")
    proc = run_live_paced(0, 60, out)
    reader = threading.Thread(target=frame_reader, args=(proc, tracker), daemon=True)
    reader.start()

    # Healthy window first, sampled at Prometheus's 5s scrape interval so the stored series
    # resolves the real ramp rather than aliasing it into a spike.
    for idx in range(2):
        time.sleep(5.0)
        push_sample(tracker.liveness(), "frozen", idx, frozen=0)

    print("  -- killing feed process now (real SIGKILL) --")
    proc.kill()

    # No further frames can arrive. The sampler keeps polling on its own clock, so the
    # liveness gap grows for real -- nothing is injected.
    idx = 2
    deadline = time.time() + hold_seconds if hold_seconds else None
    while True:
        time.sleep(5.0)
        push_sample(tracker.liveness(), "frozen", idx, frozen=1)
        idx += 1
        if deadline is None:
            if idx >= 10:
                break
        elif time.time() >= deadline:
            break

    reader.join(timeout=2)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "baseline"
    if mode == "baseline":
        baseline()
    elif mode == "frozen":
        hold = float(sys.argv[2]) if len(sys.argv) > 2 else 0.0
        frozen_fault(hold)
    elif mode == "hold_healthy":
        seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 180.0
        hold_healthy(seconds)
    else:
        print("usage: python feed_liveness_with_telemetry.py "
              "<baseline|frozen [hold_seconds]|hold_healthy [seconds]>")
        sys.exit(1)
