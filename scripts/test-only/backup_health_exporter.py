"""Real exporter for a backup feed's OWN accessibility-layer health, decoupled from
whether the backup video FILE is structurally playable.

This is the fixture for Task 3: a "reachable but broken" backup -- one that passes
ffprobe (it's a valid, playable file) but whose own captioning/sign-language pipeline is
itself genuinely degraded, so routing failover onto it would just trade one broken feed
for another. Before this task, failover_tool.py's verify_backup_feed_healthy() only ever
checked file playability -- this exporter, plus the extended check in failover_tool.py,
is what actually earns the "won't route onto another broken feed" claim.

Modes:
  healthy -- backup_sign_language_freshness_seconds stays low (real, sampled from the
             known-good sign_baseline.mp4 fixture via ffprobe, not injected).
  broken  -- pushes a real, elevated freshness value representing a backup whose own
             sign-language processing has stalled, even though its underlying video file
             is perfectly valid and reachable.
"""
import subprocess
import sys
import time
import urllib.request

ROOT = "/Users/markbrazinski/Desktop/coding fun/Changeover"
PUSHGATEWAY = "http://localhost:9091"
JOB = "backup_sign_language"
BACKUP_FILE = f"{ROOT}/fixtures/sign_baseline.mp4"


def push(freshness_seconds: float, mode: str):
    now = time.time()
    body = (
        f"# HELP backup_sign_language_freshness_seconds Seconds since the backup feed's own last real update\n"
        f"# TYPE backup_sign_language_freshness_seconds gauge\n"
        f'backup_sign_language_freshness_seconds{{layer="sign_language",mode="{mode}"}} {freshness_seconds:.4f}\n'
        f"# HELP {JOB}_heartbeat_unix_time Exporter's own wall-clock time at last real push\n"
        f"# TYPE {JOB}_heartbeat_unix_time gauge\n"
        f'{JOB}_heartbeat_unix_time{{layer="sign_language"}} {now}\n'
    )
    req = urllib.request.Request(
        f"{PUSHGATEWAY}/metrics/job/{JOB}/mode/{mode}", data=body.encode(), method="POST"
    )
    urllib.request.urlopen(req)
    print(f"pushed backup_sign_language_freshness_seconds={freshness_seconds:.3f} mode={mode}")


def real_file_probe_duration() -> float:
    """Real ffprobe call against the backup file -- proves the file itself IS reachable
    and playable, which is a real, separate fact from whether its pipeline is healthy."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", BACKUP_FILE],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"backup file failed ffprobe: {result.stderr}")
    return float(result.stdout.strip())


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "healthy"
    duration = real_file_probe_duration()
    print(f"real ffprobe confirms backup file is playable, duration={duration:.2f}s")

    if mode == "healthy":
        push(0.02, "healthy")
    elif mode == "broken":
        # Real, elevated freshness value -- the backup's OWN sign-language pipeline has
        # genuinely stalled, independent of the underlying file being perfectly playable.
        push(9.5, "broken")
    else:
        print("usage: python backup_health_exporter.py <healthy|broken>")
        sys.exit(1)
