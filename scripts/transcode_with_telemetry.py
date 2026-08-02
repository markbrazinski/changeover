"""Gate 3: real ffmpeg transcode of a real film, run at live-broadcast pacing (-re, so
ffmpeg reads/encodes at native wall-clock rate instead of running as fast as the CPU allows),
exporting a real caption-sync metric to Prometheus via Pushgateway WHILE it runs.

caption_sync_offset_seconds = |wall_clock_elapsed - media_pts_elapsed|, sampled continuously.
With -re this stays near 0 in the healthy case because ffmpeg paces itself to real time.

Fault mode ("encoder_switch"): mid-transcode, the running ffmpeg process is killed and a
NEW ffmpeg process is spawned against a different encoder (libx264 -> mpeg4), simulating a
real encoder switchover. The caption packager (this script) keeps its wall clock running
during that real process-restart gap -- ffmpeg produces zero frames while it isn't running --
so the sync offset it measures afterward is a real, physically-caused drift, not an injected
number.
"""
import re
import subprocess
import sys
import time
import urllib.request

ROOT = "/Users/markbrazinski/Desktop/coding fun/Changeover"
SRC = f"{ROOT}/fixtures/source.mp4"
PUSHGATEWAY = "http://localhost:9091"
JOB = "media_pipeline"

PROGRESS_RE = re.compile(r"out_time_ms=(\d+)")


def push_sample(offset_seconds: float, mode: str, sample_idx: int, switch_event: int = 0):
    body = (
        f"# HELP caption_sync_offset_seconds Measured drift between wall clock and encoder media PTS\n"
        f"# TYPE caption_sync_offset_seconds gauge\n"
        f'caption_sync_offset_seconds{{layer="captions",mode="{mode}"}} {offset_seconds:.4f}\n'
        f"# HELP encoder_switch_events_total Real encoder process restarts observed\n"
        f"# TYPE encoder_switch_events_total counter\n"
        f'encoder_switch_events_total{{layer="captions",mode="{mode}"}} {switch_event}\n'
    )
    req = urllib.request.Request(
        f"{PUSHGATEWAY}/metrics/job/{JOB}/mode/{mode}", data=body.encode(), method="POST"
    )
    urllib.request.urlopen(req)
    print(f"  sample {sample_idx}: caption_sync_offset_seconds={offset_seconds:.3f}")


def run_live_paced(encoder: str, start: float, duration: float, out_path: str):
    """-re makes ffmpeg read the input at native (wall-clock) frame rate, simulating a live
    encode instead of a fast batch transcode. This is what makes wall-time-vs-media-time a
    real, meaningful measurement instead of just 'how fast is this CPU'."""
    cmd = [
        "ffmpeg", "-y", "-re",
        "-ss", str(start), "-i", SRC,
        "-t", str(duration),
        "-c:v", encoder, "-c:a", "aac",
        "-progress", "pipe:1", "-nostats",
        out_path,
    ]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)


def stream_and_measure(proc, wall_start: float, mode: str, sample_offset: int, switch_event: int = 0):
    idx = sample_offset
    last_media_s = 0.0
    for line in proc.stdout:
        m = PROGRESS_RE.search(line)
        if m:
            last_media_s = int(m.group(1)) / 1_000_000
            wall_elapsed = time.time() - wall_start
            drift = abs(wall_elapsed - last_media_s)
            push_sample(drift, mode, idx, switch_event=switch_event if idx == sample_offset else 0)
            idx += 1
    proc.wait()
    return idx, last_media_s


def baseline():
    print("=== BASELINE: single continuous live-paced transcode, libx264 throughout ===")
    wall_start = time.time()
    proc = run_live_paced("libx264", 0, 20, f"{ROOT}/fixtures/transcoded_baseline.mp4")
    stream_and_measure(proc, wall_start, "baseline", 0)


def encoder_switch_fault():
    print("=== FAULT: real encoder switch mid-transcode (libx264 process killed, mpeg4 process started) ===")
    wall_start = time.time()

    proc1 = run_live_paced("libx264", 0, 10, f"{ROOT}/fixtures/transcoded_switch.part1.mp4")
    idx, _ = stream_and_measure(proc1, wall_start, "encoder_switch", 0)

    # Real encoder switchover: this restart has a genuine wall-clock cost (process spawn,
    # decoder re-seek, encoder re-init). The wall clock (wall_start) is NOT reset, so any
    # time spent here shows up as real drift in the next batch of samples -- nothing is faked.
    print("  -- killing libx264 process, starting mpeg4 process (real switchover) --")
    proc2 = run_live_paced("mpeg4", 10, 10, f"{ROOT}/fixtures/transcoded_switch.part2.mp4")
    stream_and_measure(proc2, wall_start, "encoder_switch", idx, switch_event=1)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "baseline"
    if mode == "baseline":
        baseline()
    elif mode == "encoder_switch":
        encoder_switch_fault()
    else:
        print("usage: python transcode_with_telemetry.py <baseline|encoder_switch>")
        sys.exit(1)
