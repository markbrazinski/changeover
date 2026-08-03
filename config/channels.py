"""Channel registry -- the single source of truth for what films exist and where.

Adding a film means adding an entry here plus its files under fixtures/films/<id>/. No
agent logic changes: agent/assembled_agent.py reads this registry and is instanced per
channel, which is exactly what the generalization phase is meant to prove.

Per channel, each layer gets:
  * its own Prometheus job (so series never collide across channels)
  * its own program feed and its OWN DISTINCT backup feed -- no two channels, and no two
    layers, share a backup file
  * its own ceiling, derived from that channel's observed baseline (config/ceilings.json,
    produced by scripts/derive_ceilings.py -- never hand-set)

Films are NOT committed (see .gitignore); they are supplied out-of-band per the drop-in
spec in README.md.
"""
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FILMS_DIR = os.path.join(ROOT, "fixtures", "films")
CEILINGS_PATH = os.path.join(ROOT, "config", "ceilings.json")

# Attribution is recorded here so no claim about a film's identity or license is ever
# invented in prose again. `license_verified_in_file` is True only when the license is
# readable from the file's OWN metadata, not asserted from outside knowledge.
CHANNELS = {
    "tears_of_steel": {
        "title": "Tears of Steel (2012), Blender Foundation",
        "license": "CC BY 3.0",
        "license_verified_in_file": True,
        "license_evidence": "comment tag: license:http://creativecommons.org/licenses/by/3.0/",
        "source": "http://archive.org/details/Tears-of-Steel",
        "note": (
            "Authentic ToS: ffprobe duration 734.12s matches the film's real 12:14 runtime "
            "and the title tag names archive.org. Resolution is 864x360 -- a low-res "
            "transcode despite '1080p' in the supplied filename; aspect 2.40:1 is correct. "
            "Resolution is irrelevant to both metrics (no pixels are read)."
        ),
    },
    "sintel": {
        "title": "Sintel (2010), Blender Foundation",
        "license": "CC BY 3.0",
        "license_verified_in_file": False,
        "license_evidence": (
            "title tag 'Sintel' present; NO license tag embedded. CC BY 3.0 is asserted "
            "from the film's known publication terms, not read from the file."
        ),
        "source": "Blender open movie project",
        "note": "ffprobe duration 888.06s, 2048x872 -- consistent with Sintel's native release.",
    },
}

LAYERS = ("captions", "sign_language")


def channel_dir(channel: str) -> str:
    return os.path.join(FILMS_DIR, channel)


def program_path(channel: str) -> str:
    return os.path.join(channel_dir(channel), "program.mp4")


def backup_path(channel: str) -> str:
    """Each channel's backup is a DISTINCT file, cut from a different film than its own
    program feed -- so backup verification for one channel can never be satisfied by
    another channel's (or its own primary's) media."""
    return os.path.join(channel_dir(channel), "backup.mp4")


def vtt_path(channel: str) -> str:
    return os.path.join(channel_dir(channel), "captions.en.vtt")


def job_name(channel: str, layer: str) -> str:
    """Prometheus job for a channel+layer. Distinct per channel so series never collide --
    the same discipline the scope guard enforces within a channel."""
    suffix = "captions" if layer == "captions" else "feed_liveness"
    return f"media_pipeline_{channel}_{suffix}"


def backup_job_name(channel: str, layer: str) -> str:
    suffix = "captions" if layer == "captions" else "feed_liveness"
    return f"backup_{channel}_{suffix}"


def metric_names(layer: str) -> dict:
    if layer == "captions":
        return {
            "metric": "caption_cue_sync_offset_seconds",
            "flag": "caption_cue_publisher_stalled",
            "backup_metric": "backup_captions_cue_offset_seconds",
            "fault_mode": "frozen_captions",
        }
    return {
        "metric": "feed_liveness_seconds",
        "flag": "feed_frozen",
        "backup_metric": "backup_feed_liveness_seconds",
        "fault_mode": "frozen",
    }


def load_ceilings() -> dict:
    if not os.path.exists(CEILINGS_PATH):
        raise RuntimeError(
            f"{CEILINGS_PATH} missing -- run scripts/derive_ceilings.py first. Ceilings are "
            f"derived from each channel's observed baseline and are never hand-set."
        )
    with open(CEILINGS_PATH) as f:
        return json.load(f)


def ceiling_for(channel: str, layer: str) -> float:
    data = load_ceilings()
    try:
        return float(data["channels"][channel][layer]["ceiling"])
    except KeyError:
        raise RuntimeError(
            f"no derived ceiling for {channel}/{layer} -- run "
            f"scripts/derive_ceilings.py {channel}"
        )


def available_channels() -> list:
    """Channels whose real files are actually present on disk. Films are not committed, so
    a registry entry alone is not proof the media exists."""
    out = []
    for ch in CHANNELS:
        if all(os.path.exists(p) for p in
               (program_path(ch), backup_path(ch), vtt_path(ch))):
            out.append(ch)
    return out


if __name__ == "__main__":
    print("registered channels:")
    for ch, meta in CHANNELS.items():
        present = ch in available_channels()
        print(f"\n  {ch}  {'[files present]' if present else '[FILES MISSING]'}")
        print(f"    {meta['title']}")
        print(f"    license: {meta['license']} "
              f"({'verified in file' if meta['license_verified_in_file'] else 'asserted, not in file'})")
        for layer in LAYERS:
            try:
                c = ceiling_for(ch, layer)
                print(f"    {layer:14s} job={job_name(ch, layer):44s} ceiling={c}s")
            except RuntimeError as e:
                print(f"    {layer:14s} ceiling unavailable: {e}")
