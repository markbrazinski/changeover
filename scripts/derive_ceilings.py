"""Derives each channel's healthy ceiling from its OWN observed baseline (T3).

Replaces the hand-set 1.5s thresholds. Nothing here is chosen by taste: each ceiling comes
from real samples measured on that channel's real film, by the same producers the demo runs.

DERIVATION
----------
For each channel and layer, run the real producer in baseline mode, collect every sample it
measures, and compute:

    ceiling = max(observed_baseline) * SAFETY_FACTOR

Why max-times-a-factor rather than a percentile or mean+k*sigma:

  * The caption offset is a SAWTOOTH, not a noisy scalar. It ramps from ~0 up to roughly one
    cue interval and resets on every publish. Its distribution is near-uniform across that
    ramp, so mean and standard deviation describe it poorly, while the max IS the physically
    meaningful bound -- the worst a healthy feed can look is one cue interval behind.
  * A percentile would clip that legitimate peak and cause false alarms at the top of every
    normal sawtooth.
  * The healthy/faulted separation is enormous (healthy ~0.5s vs faulted tens of seconds),
    so a loose-but-honest bound costs no detection power.

SAFETY_FACTOR covers scheduler jitter and scrape-alignment slack -- a healthy sample can
land slightly past the observed peak without the feed being unhealthy.

Feed-liveness is measured the same way, but its healthy value is bounded by ffmpeg's frame
cadence rather than a cue interval, so it derives a much tighter ceiling naturally.

Output: config/ceilings.json, consumed by the agent. Re-run when a channel's film or its
cue cadence changes.
"""
import json
import os
import statistics
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "config", "ceilings.json")

SAFETY_FACTOR = 1.5
BASELINE_SAMPLES = 8
SAMPLE_INTERVAL = 5.0


def observe_captions(channel: str) -> list:
    """Runs the real caption producer against this channel's real film and sidecar,
    returning every offset it measured."""
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import caption_cue_with_telemetry as cap

    vtt = os.path.join(ROOT, "fixtures", "films", channel, "captions.en.vtt")
    cues = cap.parse_vtt_cues(vtt)

    import threading
    import time

    program_start = time.time()
    tracker = cap.CueTracker(program_start)
    stop = threading.Event()
    pub = threading.Thread(target=cap.cue_publisher, args=(cues, tracker, stop), daemon=True)
    pub.start()

    observed = []
    for _ in range(BASELINE_SAMPLES):
        time.sleep(SAMPLE_INTERVAL)
        offset, _last, _now = tracker.offset()
        observed.append(offset)
    stop.set()
    pub.join(timeout=3)
    return observed


def observe_liveness(channel: str) -> list:
    """Runs a real ffmpeg feed off this channel's real film and measures frame-arrival gaps
    exactly as the liveness producer does."""
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import feed_liveness_with_telemetry as liv

    import threading
    import time

    program = os.path.join(ROOT, "fixtures", "films", channel, "program.mp4")
    out = os.path.join(ROOT, "fixtures", "films", channel, ".ceiling_probe.mp4")
    cmd = [
        "ffmpeg", "-y", "-re", "-ss", "0", "-i", program,
        "-t", str(BASELINE_SAMPLES * SAMPLE_INTERVAL + 10),
        "-c:v", "libx264", "-preset", "veryfast", "-an",
        "-progress", "pipe:1", "-nostats", out,
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    tracker = liv.LastFrameTracker()
    reader = threading.Thread(target=liv.frame_reader, args=(proc, tracker), daemon=True)
    reader.start()

    observed = []
    for _ in range(BASELINE_SAMPLES):
        time.sleep(SAMPLE_INTERVAL)
        observed.append(tracker.liveness())

    proc.kill()
    reader.join(timeout=2)
    if os.path.exists(out):
        os.remove(out)
    return observed


def derive(samples: list) -> dict:
    peak = max(samples)
    return {
        "samples": [round(s, 4) for s in samples],
        "n": len(samples),
        "observed_min": round(min(samples), 4),
        "observed_max": round(peak, 4),
        "observed_median": round(statistics.median(samples), 4),
        "safety_factor": SAFETY_FACTOR,
        "ceiling": round(peak * SAFETY_FACTOR, 4),
        "rule": "ceiling = observed_max * safety_factor",
    }


if __name__ == "__main__":
    channels = sys.argv[1:] or ["tears_of_steel", "sintel"]
    result = {"safety_factor": SAFETY_FACTOR, "channels": {}}

    for ch in channels:
        print(f"=== {ch} ===")
        print("  observing captions baseline (real film + real sidecar)...")
        cap_samples = observe_captions(ch)
        cap_d = derive(cap_samples)
        print(f"    max={cap_d['observed_max']}s -> ceiling={cap_d['ceiling']}s")

        print("  observing feed-liveness baseline (real ffmpeg off the same film)...")
        liv_samples = observe_liveness(ch)
        liv_d = derive(liv_samples)
        print(f"    max={liv_d['observed_max']}s -> ceiling={liv_d['ceiling']}s")

        result["channels"][ch] = {"captions": cap_d, "sign_language": liv_d}

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(result, f, indent=2)
    print(f"\nwrote {os.path.relpath(OUT, ROOT)}")
