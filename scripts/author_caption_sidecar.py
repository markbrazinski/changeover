"""Authors a caption sidecar for a real film, to the spec in README.md.

HONESTY NOTE -- read before using the output.
The cue TEXT this writes is authored placeholder content for the Changeover demo rig. It is
NOT a transcript of the film and does not claim to be; the films ship without embedded
subtitle streams, and inventing dialogue would be a fabrication of the film's content.
What IS real and load-bearing is the cue TIMING: a monotonic ~2s-cadence timeline spanning
the film's actual measured duration, which is the only thing the caption metric reads
(program clock minus last published cue's media timestamp -- see
scripts/caption_cue_with_telemetry.py).

Duration comes from a real ffprobe of the film, not an assumption.
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CUE_SECONDS = 2.002      # cue on-screen duration
GAP_SECONDS = 0.5        # gap before the next cue
FIRST_CUE_AT = 2.002     # first cue start (spec: <= 5s)

# Rotating placeholder lines. Deliberately generic operational text -- it describes the
# monitoring rig, never the film, so it can never be mistaken for a transcript.
LINES = [
    "Program feed active. Accessibility layers nominal.",
    "Caption layer reporting in sync with the program clock.",
    "Feed-liveness monitor reporting frame delivery.",
    "All monitored layers holding steady.",
    "Program clock advancing normally.",
    "Caption cue cadence nominal.",
    "No drift detected across monitored layers.",
    "Encoder output stable.",
    "Caption timing continues to track the program clock.",
    "Layer health checks passing.",
    "Monitoring window continuing.",
    "Caption sync holding at baseline.",
    "Cue delivery on schedule.",
    "Downstream distribution nominal.",
    "Accessibility telemetry reporting normally.",
]


def probe_duration(path: str) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", path],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def ts(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def author(channel_id: str, film_title: str, license_note: str) -> str:
    channel_dir = os.path.join(ROOT, "fixtures", "films", channel_id)
    program = os.path.join(channel_dir, "program.mp4")
    duration = probe_duration(program)

    cues = []
    start = FIRST_CUE_AT
    idx = 0
    # Leave a cue-length margin so the last cue ends before the film does.
    while start + CUE_SECONDS < duration - CUE_SECONDS:
        cues.append((start, start + CUE_SECONDS, LINES[idx % len(LINES)]))
        start += CUE_SECONDS + GAP_SECONDS
        idx += 1

    header = f"""WEBVTT

NOTE
Caption sidecar for the "{channel_id}" channel of the Changeover demo rig.

Film: {film_title}
{license_note}

THE CUE TEXT BELOW IS AUTHORED PLACEHOLDER CONTENT, NOT A TRANSCRIPT. This film
ships without an embedded subtitle stream, and inventing dialogue would fabricate
the film's content. The lines describe the monitoring rig itself so they can never
be mistaken for the film's script.

What IS real: the cue TIMING. {len(cues)} cues at a {CUE_SECONDS}s cadence with
{GAP_SECONDS}s gaps, spanning this film's actual ffprobe-measured duration of
{duration:.3f}s. Cue timing is the only thing the caption metric reads -- the offset
is program-clock-now minus the media timestamp of the last published cue.

Cadence is deliberately ~2s so a stalled cue publisher becomes measurable against
the program clock within a few seconds.
"""

    body = "\n".join(
        f"\n{ts(a)} --> {ts(b)}\n<v Narrator>{text}" for a, b, text in cues
    )

    path = os.path.join(channel_dir, "captions.en.vtt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(header + body + "\n")
    print(f"  {channel_id}: {len(cues)} cues spanning {duration:.1f}s -> "
          f"{os.path.relpath(path, ROOT)}")
    return path


if __name__ == "__main__":
    CHANNELS = {
        "tears_of_steel": (
            "Tears of Steel (2012), Blender Foundation",
            "License: CC BY 3.0 -- embedded in the file's own metadata\n"
            "(comment tag: license:http://creativecommons.org/licenses/by/3.0/).\n"
            "Source: http://archive.org/details/Tears-of-Steel",
        ),
        "sintel": (
            "Sintel (2010), Blender Foundation",
            "License: CC BY 3.0 (Blender open movie project).\n"
            "Note: this particular file carries a title tag but no embedded license tag;\n"
            "the license is asserted from the film's known publication terms, not read\n"
            "from the file.",
        ),
    }
    targets = sys.argv[1:] or list(CHANNELS)
    for ch in targets:
        title, lic = CHANNELS[ch]
        author(ch, title, lic)
