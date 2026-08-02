"""Wipe all pushed metrics and re-inject one named fault scenario into Prometheus via Pushgateway.

Usage: python scripts/reseed.py <scenario>
Scenarios: baseline, caption_delay, sign_frozen, caption_gen_failure
"""
import sys
import time
import urllib.request

PUSHGATEWAY = "http://localhost:9091"
PROMETHEUS = "http://localhost:9090"
JOB = "accessibility_layers"


def wipe():
    # Pushgateway groups pushed metrics by the full job/<grouping> path, so a bare
    # DELETE on job/{JOB} does not clear groupings pushed under job/{JOB}/scenario/<name>.
    # Delete every known scenario grouping explicitly.
    for name in SCENARIOS:
        req = urllib.request.Request(
            f"{PUSHGATEWAY}/metrics/job/{JOB}/scenario/{name}", method="DELETE"
        )
        try:
            urllib.request.urlopen(req)
        except Exception:
            pass
    # Also delete the already-ingested series from Prometheus's own TSDB, since it
    # keeps serving the last-scraped sample until staleness kicks in (~5m) otherwise.
    req = urllib.request.Request(
        f"{PROMETHEUS}/api/v1/admin/tsdb/delete_series?match[]={{job=%22{JOB}%22}}",
        method="POST",
    )
    try:
        urllib.request.urlopen(req)
    except Exception:
        pass


def push(metrics: str, grouping: str):
    url = f"{PUSHGATEWAY}/metrics/job/{JOB}/{grouping}"
    req = urllib.request.Request(url, data=metrics.encode(), method="POST")
    urllib.request.urlopen(req)


SCENARIOS = {
    "baseline": """
# HELP caption_sync_offset_seconds Caption stream offset from audio, seconds
# TYPE caption_sync_offset_seconds gauge
caption_sync_offset_seconds{layer="captions"} 0.05
# HELP sign_feed_freshness_seconds Seconds since last sign-language frame update
# TYPE sign_feed_freshness_seconds gauge
sign_feed_freshness_seconds{layer="sign_language"} 0.2
# HELP caption_gen_success_rate Fraction of caption-gen requests succeeding
# TYPE caption_gen_success_rate gauge
caption_gen_success_rate{layer="captions_upstream"} 1.0
# HELP layer_up Layer health (1=up)
# TYPE layer_up gauge
layer_up{layer="captions"} 1
layer_up{layer="sign_language"} 1
layer_up{layer="audio_description"} 1
""",
    "caption_delay": """
# HELP caption_sync_offset_seconds Caption stream offset from audio, seconds
# TYPE caption_sync_offset_seconds gauge
caption_sync_offset_seconds{layer="captions"} 4.8
# HELP sign_feed_freshness_seconds Seconds since last sign-language frame update
# TYPE sign_feed_freshness_seconds gauge
sign_feed_freshness_seconds{layer="sign_language"} 0.2
# HELP caption_gen_success_rate Fraction of caption-gen requests succeeding
# TYPE caption_gen_success_rate gauge
caption_gen_success_rate{layer="captions_upstream"} 1.0
# HELP encoder_switch_events_total Encoder switchover count
# TYPE encoder_switch_events_total counter
encoder_switch_events_total{layer="captions"} 1
# HELP layer_up Layer health (1=up)
# TYPE layer_up gauge
layer_up{layer="captions"} 1
layer_up{layer="sign_language"} 1
layer_up{layer="audio_description"} 1
""",
    "sign_frozen": """
# HELP caption_sync_offset_seconds Caption stream offset from audio, seconds
# TYPE caption_sync_offset_seconds gauge
caption_sync_offset_seconds{layer="captions"} 0.05
# HELP sign_feed_freshness_seconds Seconds since last sign-language frame update
# TYPE sign_feed_freshness_seconds gauge
sign_feed_freshness_seconds{layer="sign_language"} 47.0
# HELP caption_gen_success_rate Fraction of caption-gen requests succeeding
# TYPE caption_gen_success_rate gauge
caption_gen_success_rate{layer="captions_upstream"} 1.0
# HELP layer_up Layer health (1=up)
# TYPE layer_up gauge
layer_up{layer="captions"} 1
layer_up{layer="sign_language"} 1
layer_up{layer="audio_description"} 1
""",
    "caption_gen_failure": """
# HELP caption_sync_offset_seconds Caption stream offset from audio, seconds
# TYPE caption_sync_offset_seconds gauge
caption_sync_offset_seconds{layer="captions"} 0.05
# HELP sign_feed_freshness_seconds Seconds since last sign-language frame update
# TYPE sign_feed_freshness_seconds gauge
sign_feed_freshness_seconds{layer="sign_language"} 0.2
# HELP caption_gen_success_rate Fraction of caption-gen requests succeeding
# TYPE caption_gen_success_rate gauge
caption_gen_success_rate{layer="captions_upstream"} 0.03
# HELP layer_up Layer health (1=up)
# TYPE layer_up gauge
layer_up{layer="captions"} 0
layer_up{layer="sign_language"} 1
layer_up{layer="audio_description"} 1
""",
}


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in SCENARIOS:
        print(f"usage: python scripts/reseed.py <{'|'.join(SCENARIOS)}>")
        sys.exit(1)
    scenario = sys.argv[1]
    wipe()
    time.sleep(0.5)
    push(SCENARIOS[scenario], f"scenario/{scenario}")
    print(f"reseeded scenario: {scenario}")
