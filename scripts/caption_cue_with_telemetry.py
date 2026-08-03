"""Ring-1 captions producer: measures REAL caption-cue sync drift against an advancing
program clock, and exports it as caption_cue_sync_offset_seconds.

WHAT IS ACTUALLY MEASURED
-------------------------
Two threads with independent clocks:

  * The CUE PUBLISHER walks fixtures/captions/placeholder.en.vtt in real time. When
    wall-clock program time reaches a cue's start timestamp, it "publishes" that cue and
    records (a) the cue's own media timestamp and (b) the wall-clock instant it went out.
  * The SAMPLER polls on its own fixed interval, decoupled from cue arrival, and computes

        offset = program_clock_now - media_timestamp_of_last_published_cue

    i.e. how far the program has advanced past the last caption the viewer actually got.

In the healthy case this stays small and bounded by cue cadence (~2s cues => offset
oscillates within roughly one cue interval and resets on every publish). When the cue
publisher DIES, no new cue is ever published, so the term `media_timestamp_of_last_
published_cue` freezes while `program_clock_now` keeps advancing -- the offset climbs
monotonically and for real. Nothing is injected; the climb is arithmetic on two clocks,
one of which stopped.

WHY THIS IS NOT caption_sync_offset_seconds
-------------------------------------------
scripts/transcode_with_telemetry.py emits caption_sync_offset_seconds, which is
|wall_elapsed - ffmpeg_video_PTS| -- an ENCODER-drift measurement that never touches a
caption. This module is a different physical quantity (caption cue vs program clock) and
therefore uses a different metric name. The two must stay separable; see this file's
entry in README.md.

Modes:
  baseline        -- cue publisher runs to completion; offset stays at its real baseline.
  frozen_captions -- cue publisher thread is genuinely stopped mid-run (a real stop event
                     the publisher observes and exits on, not a fabricated number), and is
                     never restarted. Offset climbs for real from then on.
"""
import os
import re
import sys
import threading
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PUSHGATEWAY = "http://localhost:9091"

# Channel selection. CHANGEOVER_CHANNEL names a registered channel (config/channels.py),
# which resolves this producer's real film, sidecar and Prometheus job. Unset falls back to
# the single-channel placeholder so pre-generalization commands keep working unchanged.
CHANNEL = os.environ.get("CHANGEOVER_CHANNEL")
if CHANNEL:
    sys.path.insert(0, os.path.join(ROOT, "config"))
    import channels as _ch
    VTT_PATH = _ch.vtt_path(CHANNEL)
    JOB = _ch.job_name(CHANNEL, "captions")
else:
    VTT_PATH = os.path.join(ROOT, "fixtures", "captions", "placeholder.en.vtt")
    JOB = "media_pipeline_captions"

CUE_TIME_RE = re.compile(
    r"(\d{2}):(\d{2}):(\d{2})\.(\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})\.(\d{3})"
)

# Program-clock position at which the fault mode stops the cue publisher. Shared with
# hold_open() so the held-open state reproduces the same real stall point.
STALL_AT_PROGRAM_SECONDS = 15.0


def parse_vtt_cues(path: str) -> list:
    """Parses real cue start times (seconds) out of the WebVTT sidecar. Returns a sorted
    list of (start_seconds, text). Raises if the file yields no cues -- a silently empty
    cue list would make the producer look healthy while measuring nothing."""
    cues = []
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()

    for i, line in enumerate(lines):
        m = CUE_TIME_RE.search(line)
        if not m:
            continue
        h, mnt, s, ms = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
        start = h * 3600 + mnt * 60 + s + ms / 1000.0
        text = lines[i + 1].strip() if i + 1 < len(lines) else ""
        cues.append((start, text))

    if not cues:
        raise RuntimeError(f"no cues parsed from {path} -- refusing to measure nothing")
    cues.sort(key=lambda c: c[0])
    return cues


class CueTracker:
    """Thread-safe record of the most recently PUBLISHED cue's media timestamp, plus the
    program clock origin. The sampler reads this; the publisher writes it."""

    def __init__(self, program_start: float):
        self.program_start = program_start
        self.last_cue_media_ts = 0.0
        self.published_count = 0
        self.lock = threading.Lock()

    def publish(self, media_ts: float):
        with self.lock:
            self.last_cue_media_ts = media_ts
            self.published_count += 1

    def program_clock(self) -> float:
        return time.time() - self.program_start

    def offset(self) -> tuple:
        """The real measurement: how far the program has advanced past the last caption
        actually delivered. Returns (offset_seconds, last_cue_media_ts, program_now)."""
        with self.lock:
            last = self.last_cue_media_ts
        now = self.program_clock()
        return now - last, last, now


def cue_publisher(cues: list, tracker: CueTracker, stop_event: threading.Event):
    """Walks the real cue list against the real program clock. Exits immediately if the
    stop event fires -- that stop is the fault: a cue producer that died."""
    for start_ts, text in cues:
        while tracker.program_clock() < start_ts:
            if stop_event.is_set():
                print(f"  -- cue publisher STOPPED (real thread exit) at program "
                      f"t={tracker.program_clock():.3f}s; no further cues will publish --")
                return
            time.sleep(0.02)
        if stop_event.is_set():
            print(f"  -- cue publisher STOPPED (real thread exit) at program "
                  f"t={tracker.program_clock():.3f}s; no further cues will publish --")
            return
        tracker.publish(start_ts)
        print(f"  [cue] published media_ts={start_ts:.3f}s :: {text[:48]}")


def push_sample(offset_seconds: float, mode: str, sample_idx: int,
                stalled: int, last_cue_ts: float, program_now: float):
    # A negative offset means the program clock is BEHIND a cue that was already published,
    # which cannot happen in a real feed and always indicates a clock-origin bug in this
    # producer (it did, once -- see hold_open's program_start comment). Fail loudly rather
    # than pushing a physically impossible value into the metric the agent diagnoses from.
    if offset_seconds < 0:
        raise RuntimeError(
            f"refusing to push negative caption offset ({offset_seconds:.4f}s): program "
            f"clock {program_now:.4f}s is behind last published cue {last_cue_ts:.4f}s -- "
            f"this is a producer clock bug, not a real measurement"
        )
    # Heartbeat is this exporter's own wall clock at push time, decoupled from the domain
    # value -- lets agent/evidence_gate.py distinguish "exporter alive, reporting a real
    # fault" from "exporter itself died" without interpreting the domain value. Same
    # contract as scripts/sign_feed_with_telemetry.py.
    body = (
        f"# HELP caption_cue_sync_offset_seconds Program clock minus media timestamp of the "
        f"last published caption cue\n"
        f"# TYPE caption_cue_sync_offset_seconds gauge\n"
        f'caption_cue_sync_offset_seconds{{layer="captions",mode="{mode}"}} {offset_seconds:.4f}\n'
        f"# HELP caption_cue_publisher_stalled Whether the cue publisher has stopped producing cues\n"
        f"# TYPE caption_cue_publisher_stalled gauge\n"
        f'caption_cue_publisher_stalled{{layer="captions",mode="{mode}"}} {stalled}\n'
        f"# HELP caption_last_cue_media_timestamp_seconds Media timestamp of the last cue actually published\n"
        f"# TYPE caption_last_cue_media_timestamp_seconds gauge\n"
        f'caption_last_cue_media_timestamp_seconds{{layer="captions",mode="{mode}"}} {last_cue_ts:.4f}\n'
        f"# HELP caption_program_clock_seconds Program clock position at sample time\n"
        f"# TYPE caption_program_clock_seconds gauge\n"
        f'caption_program_clock_seconds{{layer="captions",mode="{mode}"}} {program_now:.4f}\n'
        f"# HELP {JOB}_heartbeat_unix_time Exporter's own wall-clock time at last real push\n"
        f"# TYPE {JOB}_heartbeat_unix_time gauge\n"
        f'{JOB}_heartbeat_unix_time{{layer="captions"}} {time.time()}\n'
    )
    req = urllib.request.Request(
        f"{PUSHGATEWAY}/metrics/job/{JOB}/mode/{mode}", data=body.encode(), method="POST"
    )
    urllib.request.urlopen(req)
    print(f"  sample {sample_idx}: caption_cue_sync_offset_seconds={offset_seconds:.3f} "
          f"(program={program_now:.2f}s last_cue={last_cue_ts:.2f}s) stalled={stalled}")


def run(mode: str, sample_count: int, interval_s: float, stall_after: int = None):
    cues = parse_vtt_cues(VTT_PATH)
    print(f"parsed {len(cues)} real cues from {os.path.relpath(VTT_PATH, ROOT)}")

    program_start = time.time()
    tracker = CueTracker(program_start)
    stop_event = threading.Event()

    pub = threading.Thread(target=cue_publisher, args=(cues, tracker, stop_event), daemon=True)
    pub.start()

    observed = []
    for idx in range(sample_count):
        time.sleep(interval_s)
        if stall_after is not None and idx == stall_after:
            print("  -- injecting REAL fault: stopping the cue publisher thread --")
            stop_event.set()
            pub.join(timeout=3)

        stalled = 1 if (stall_after is not None and idx >= stall_after) else 0
        offset, last_cue_ts, program_now = tracker.offset()
        push_sample(offset, mode, idx, stalled, last_cue_ts, program_now)
        observed.append(offset)

    stop_event.set()
    pub.join(timeout=3)

    print(f"\nmeasured offset range: min={min(observed):.4f}s max={max(observed):.4f}s "
          f"final={observed[-1]:.4f}s")
    return observed


def hold_healthy(seconds: float, interval_s: float = 5.0):
    """Keeps a HEALTHY captions exporter alive and publishing for `seconds`.

    Needed for the discrimination scenario: to show the agent correctly naming the faulted
    layer and ruling out the healthy one, the healthy layer's exporter must actually be
    ALIVE. A finite baseline run that has exited is a dead exporter, and the evidence gate
    refuses on it (correctly) before any diagnosis happens -- which tests the gate, not
    discrimination.

    The cue publisher keeps running, so the offset stays at its real healthy value and is
    measured the same way as everywhere else -- nothing is asserted or held constant.
    """
    cues = parse_vtt_cues(VTT_PATH)
    program_start = time.time()
    tracker = CueTracker(program_start)
    stop_event = threading.Event()

    pub = threading.Thread(target=cue_publisher, args=(cues, tracker, stop_event), daemon=True)
    pub.start()

    print(f"  holding HEALTHY captions exporter open for {seconds:.0f}s")
    idx = 0
    deadline = time.time() + seconds
    while time.time() < deadline:
        time.sleep(interval_s)
        offset, last_cue_ts, program_now = tracker.offset()
        # The sidecar is finite; once its cues are exhausted the publisher exits and the
        # offset would climb like a fault. Restart the program clock to loop the sidecar,
        # keeping the feed genuinely healthy for the whole window.
        if not pub.is_alive():
            program_start = time.time()
            tracker = CueTracker(program_start)
            stop_event = threading.Event()
            pub = threading.Thread(target=cue_publisher, args=(cues, tracker, stop_event), daemon=True)
            pub.start()
            continue
        push_sample(offset, "baseline", idx, 0, last_cue_ts, program_now)
        idx += 1

    stop_event.set()
    pub.join(timeout=3)


def recover(seconds: float, interval_s: float = 5.0):
    """Post-failover recovery: the layer is now being served by the healthy BACKUP feed, so
    its measured cue offset genuinely returns to baseline.

    Pushes to the SAME series the fault was pushed under (mode="frozen_captions"), because
    that is the series the post-swap verifier reads -- the metric recovering IS the physical
    fact that the backup feed is delivering cues on time. The offset is measured exactly as
    everywhere else (program clock minus last published cue); nothing is asserted or pinned
    to a healthy-looking constant.
    """
    cues = parse_vtt_cues(VTT_PATH)
    program_start = time.time()
    tracker = CueTracker(program_start)
    stop_event = threading.Event()
    pub = threading.Thread(target=cue_publisher, args=(cues, tracker, stop_event), daemon=True)
    pub.start()

    print(f"  RECOVERED: backup feed publishing cues for {seconds:.0f}s")
    idx = 0
    deadline = time.time() + seconds
    while time.time() < deadline:
        time.sleep(interval_s)
        if not pub.is_alive():  # sidecar exhausted -- loop it to keep the feed healthy
            program_start = time.time()
            tracker = CueTracker(program_start)
            stop_event = threading.Event()
            pub = threading.Thread(target=cue_publisher, args=(cues, tracker, stop_event), daemon=True)
            pub.start()
            continue
        offset, last_cue_ts, program_now = tracker.offset()
        push_sample(offset, "frozen_captions", idx, 0, last_cue_ts, program_now)
        idx += 1

    stop_event.set()
    pub.join(timeout=3)


def baseline():
    print("=== BASELINE: cue publisher runs normally against the real program clock ===")
    # Same 5s sampling as the fault mode so both series are stored at the same real
    # resolution and are directly comparable in Grafana.
    run("baseline", sample_count=10, interval_s=5.0)


def hold_open(mode: str, seconds: float, interval_s: float = 5.0):
    """Keeps the exporter alive after the fault window, still pushing the SAME real frozen
    state, so the evidence gate sees a live heartbeat while the agent investigates.

    This is not cosmetic and it is not a way around the gate. agent/evidence_gate.py
    refuses (tier=stale) when a job's heartbeat is older than 90s, which is correct: a
    one-shot producer that exited genuinely IS a dead exporter, and no value from it should
    be trusted. But the real-world case being demonstrated is a LIVE pipeline whose cue
    publisher has stalled -- the exporter is healthy and reporting, the thing it measures
    is broken. Holding the exporter open reproduces that real condition faithfully.

    The offset it pushes keeps CLIMBING for real (the program clock keeps advancing while
    last_cue stays frozen); nothing is re-pushed as a constant.
    """
    cues = parse_vtt_cues(VTT_PATH)

    # The program clock must RESUME at the stall point, not restart at zero. A live program
    # feed does not rewind when the cue publisher dies -- it keeps advancing from where it
    # was. Backdating program_start by STALL_AT_PROGRAM_SECONDS makes program_clock() start
    # at the stall position, so the very first sample is already the true offset and keeps
    # climbing from there. Starting at zero instead produced NEGATIVE offsets (the clock
    # was behind cues that had already been published), which is physically meaningless for
    # a "how far has the program advanced past the last caption" measurement.
    program_start = time.time() - STALL_AT_PROGRAM_SECONDS
    tracker = CueTracker(program_start)

    # Reproduce the fault's real end-state: publish cues up to the stall point, then stop.
    stall_at_media_ts = None
    for start_ts, _text in cues:
        if start_ts <= STALL_AT_PROGRAM_SECONDS:
            tracker.publish(start_ts)
            stall_at_media_ts = start_ts
    print(f"  holding exporter open for {seconds:.0f}s; cue publisher remains stopped "
          f"(last cue media_ts={stall_at_media_ts:.3f}s)")

    idx = 0
    deadline = time.time() + seconds
    while time.time() < deadline:
        time.sleep(interval_s)
        offset, last_cue_ts, program_now = tracker.offset()
        push_sample(offset, mode, idx, 1, last_cue_ts, program_now)
        idx += 1


def frozen_captions_fault():
    print("=== FAULT: cue publisher thread genuinely stopped mid-run, never restarted ===")
    # Sampling interval is matched to Prometheus's 5s scrape_interval. At the producer's
    # native 1s cadence the climb is real but Prometheus only stores every ~5th sample, so
    # a 15s climb renders as ~3 stored points -- a spike, not a curve. Sampling at the
    # scrape interval means (almost) every pushed sample is actually persisted, so the
    # stored series resolves the real ramp. This changes the RESOLUTION of the recording,
    # not the measurement: each pushed value is still the true measured offset.
    run("frozen_captions", sample_count=14, interval_s=5.0, stall_after=2)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "baseline"
    if mode == "baseline":
        baseline()
    elif mode == "frozen_captions":
        frozen_captions_fault()
    elif mode == "hold_open":
        seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 180.0
        hold_open("frozen_captions", seconds)
    elif mode == "hold_healthy":
        seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 180.0
        hold_healthy(seconds)
    elif mode == "recover":
        seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 180.0
        recover(seconds)
    else:
        print("usage: python caption_cue_with_telemetry.py "
              "<baseline|frozen_captions|hold_open [seconds]>")
        sys.exit(1)
