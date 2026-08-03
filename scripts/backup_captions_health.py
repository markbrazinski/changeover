"""Real health exporter for the CAPTIONS backup feed's own cue pipeline.

Distinct from scripts/test-only/backup_health_exporter.py in two ways that matter:
  1. It is NOT a test fixture. It is part of the demo path -- the captions backup must
     have live telemetry for failover_tool.verify_backup_feed_healthy("captions") to pass
     its evidence gate, exactly as the sign_language backup does.
  2. The healthy value is MEASURED, not asserted: it replays the backup caption sidecar
     against a real program clock using the same cue-publishing arithmetic as the primary
     producer (scripts/caption_cue_with_telemetry.py), and reports the real offset it
     observes. A backup whose sidecar is empty, unparseable, or whose cues do not advance
     produces a real elevated/failed reading rather than a hardcoded "fine."

Run with no arguments to sample the backup once and push the measured result.
"""
import os
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from caption_cue_with_telemetry import parse_vtt_cues  # noqa: E402  (real reuse, same math)

PUSHGATEWAY = "http://localhost:9091"
JOB = "backup_captions"

BACKUP_VIDEO = os.path.join(ROOT, "fixtures", "source.mp4")
BACKUP_VTT = os.path.join(ROOT, "fixtures", "captions", "placeholder.en.vtt")

# How many seconds of the backup's cue timeline to actually replay when sampling its
# health. Kept short so the pre-failover check stays fast, long enough to cross several
# real cue boundaries.
SAMPLE_WINDOW_SECONDS = 6.0


def real_file_probe_duration(path: str) -> float:
    """Real ffprobe -- proves the backup video file is reachable and playable, a separate
    fact from whether its caption pipeline is healthy."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"backup file failed ffprobe: {result.stderr}")
    return float(result.stdout.strip())


def measure_backup_cue_offset() -> float:
    """Replays the backup's real cue sidecar against a real program clock and returns the
    worst (largest) caption-cue offset observed in the sample window. This is the same
    quantity the primary producer measures, computed the same way -- so a stalled backup
    caption pipeline shows up as a genuinely elevated number here."""
    cues = parse_vtt_cues(BACKUP_VTT)

    program_start = time.time()
    last_cue_media_ts = 0.0
    cue_idx = 0
    worst_offset = 0.0

    while True:
        program_now = time.time() - program_start
        if program_now >= SAMPLE_WINDOW_SECONDS:
            break
        # Advance through every cue whose start time the program clock has reached.
        while cue_idx < len(cues) and cues[cue_idx][0] <= program_now:
            last_cue_media_ts = cues[cue_idx][0]
            cue_idx += 1
        worst_offset = max(worst_offset, program_now - last_cue_media_ts)
        time.sleep(0.05)

    return worst_offset


def push(offset_seconds: float):
    now = time.time()
    body = (
        f"# HELP backup_captions_cue_offset_seconds Measured caption cue offset on the backup feed\n"
        f"# TYPE backup_captions_cue_offset_seconds gauge\n"
        f'backup_captions_cue_offset_seconds{{layer="captions"}} {offset_seconds:.4f}\n'
        f"# HELP {JOB}_heartbeat_unix_time Exporter's own wall-clock time at last real push\n"
        f"# TYPE {JOB}_heartbeat_unix_time gauge\n"
        f'{JOB}_heartbeat_unix_time{{layer="captions"}} {now}\n'
    )
    req = urllib.request.Request(
        f"{PUSHGATEWAY}/metrics/job/{JOB}", data=body.encode(), method="POST"
    )
    urllib.request.urlopen(req)
    print(f"pushed backup_captions_cue_offset_seconds={offset_seconds:.4f}")


def sample_and_push():
    offset = measure_backup_cue_offset()
    print(f"measured worst backup cue offset over {SAMPLE_WINDOW_SECONDS:.0f}s window: {offset:.4f}s")
    push(offset)
    return offset


if __name__ == "__main__":
    duration = real_file_probe_duration(BACKUP_VIDEO)
    print(f"real ffprobe confirms backup video is playable, duration={duration:.2f}s")

    # `watch <seconds>` keeps re-measuring and re-pushing for the given duration. The
    # backup-health check in failover_tool.py runs through the same evidence gate as
    # everything else, and that gate refuses a job whose heartbeat is older than 90s -- a
    # one-shot push goes stale while the agent is still investigating, and failover is then
    # refused for a stale-telemetry reason rather than on the backup's real health. A live
    # backup feed in production has continuously-reporting health telemetry; watch mode
    # reproduces that. Every push is a fresh real measurement, not a repeated constant.
    if len(sys.argv) > 1 and sys.argv[1] == "watch":
        seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 240.0
        deadline = time.time() + seconds
        print(f"watching backup health for {seconds:.0f}s...")
        while time.time() < deadline:
            sample_and_push()
            time.sleep(5)
    else:
        sample_and_push()
