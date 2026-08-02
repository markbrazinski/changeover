"""Real fixtures for the stale/partial evidence gate (Task 2). Pushed to real Prometheus,
remote-written to real Grafana Cloud -- not simulated.

Uses python -c inline `time.time()` for the heartbeat value at push time, since Workflow-
style scripts can't call time.time() but this is a plain script run directly, not inside
a Workflow -- real wall-clock time is fine here.

STALE:   domain metrics present (a real, otherwise-healthy-looking reading), but NO
          heartbeat metric is pushed at all -- representing an exporter process that
          crashed after its last real push. The gate's own missing-heartbeat=stale
          fail-closed rule catches this without needing to interpret the domain value.
FRESH_CONTROL: same job, but WITH a fresh heartbeat (pushed at push-time), to prove the
          gate does NOT refuse when the exporter is genuinely alive, even though the
          domain reading is the same as the stale case -- isolates the heartbeat as the
          only variable, since domain-value interpretation must never be the staleness
          signal (see evidence_gate.py's design note on why that was rejected).
PARTIAL:  fresh heartbeat pushed, sign_feed_freshness_seconds pushed, sign_feed_frozen
          deliberately withheld -- genuine mixed evidence with a provably-alive exporter.
"""
import sys
import time
import urllib.request

PUSHGATEWAY = "http://localhost:9091"


def _fixtures():
    now = time.time()
    return {
        "stale": {
            "job": "media_pipeline_sign_stale_fixture",
            "body": """# HELP sign_feed_freshness_seconds Seconds since the last real frame was produced
# TYPE sign_feed_freshness_seconds gauge
sign_feed_freshness_seconds{layer="sign_language",mode="stale_fixture"} 0.02
# HELP sign_feed_frozen Whether the feed process has stopped producing frames
# TYPE sign_feed_frozen gauge
sign_feed_frozen{layer="sign_language",mode="stale_fixture"} 0
""",
            # No heartbeat pushed at all -- exporter crashed after its last real push.
        },
        "fresh_control": {
            "job": "media_pipeline_sign_fresh_control_fixture",
            "body": f"""# HELP sign_feed_freshness_seconds Seconds since the last real frame was produced
# TYPE sign_feed_freshness_seconds gauge
sign_feed_freshness_seconds{{layer="sign_language",mode="fresh_control_fixture"}} 0.02
# HELP sign_feed_frozen Whether the feed process has stopped producing frames
# TYPE sign_feed_frozen gauge
sign_feed_frozen{{layer="sign_language",mode="fresh_control_fixture"}} 0
# HELP media_pipeline_sign_fresh_control_fixture_heartbeat_unix_time Exporter's own wall-clock time at last real push
# TYPE media_pipeline_sign_fresh_control_fixture_heartbeat_unix_time gauge
media_pipeline_sign_fresh_control_fixture_heartbeat_unix_time{{layer="sign_language"}} {now}
""",
        },
        "partial": {
            "job": "media_pipeline_sign_partial_fixture",
            "body": f"""# HELP sign_feed_freshness_seconds Seconds since the last real frame was produced
# TYPE sign_feed_freshness_seconds gauge
sign_feed_freshness_seconds{{layer="sign_language",mode="partial_fixture"}} 0.02
# HELP media_pipeline_sign_partial_fixture_heartbeat_unix_time Exporter's own wall-clock time at last real push
# TYPE media_pipeline_sign_partial_fixture_heartbeat_unix_time gauge
media_pipeline_sign_partial_fixture_heartbeat_unix_time{{layer="sign_language"}} {now}
""",
            # sign_feed_frozen deliberately NOT pushed -- genuine partial evidence, despite
            # a provably fresh, alive exporter.
        },
    }


def push(name: str):
    fixtures = _fixtures()
    fx = fixtures[name]
    req = urllib.request.Request(
        f"{PUSHGATEWAY}/metrics/job/{fx['job']}", data=fx["body"].encode(), method="POST"
    )
    urllib.request.urlopen(req)
    print(f"pushed {name} fixture under job={fx['job']}")


def wipe(name: str):
    fx = _fixtures()[name]
    req = urllib.request.Request(f"{PUSHGATEWAY}/metrics/job/{fx['job']}", method="DELETE")
    try:
        urllib.request.urlopen(req)
    except Exception:
        pass


if __name__ == "__main__":
    names = list(_fixtures().keys())
    if len(sys.argv) != 2 or sys.argv[1] not in names:
        print(f"usage: python scripts/seed_evidence_quality_fixtures.py <{'|'.join(names)}>")
        sys.exit(1)
    push(sys.argv[1])
