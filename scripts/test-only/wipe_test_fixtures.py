"""Wipes every known test-fixture job from Pushgateway and reports whether any were
found -- run this before recording or presenting a live demo to guarantee the fixtures
that once caused a real misdiagnosis (see this directory's README) cannot be present.

Exits 0 and prints "clean" if nothing was found. Exits 1 and lists what it removed if the
rig was contaminated -- treat a non-zero exit as "do not record yet."
"""
import sys
import urllib.request

PUSHGATEWAY = "http://localhost:9091"

# Every job name any test-only script in this directory is known to push under.
TEST_FIXTURE_JOBS = [
    "media_pipeline_sign_stale_fixture",
    "media_pipeline_sign_partial_fixture",
    "media_pipeline_sign_fresh_control_fixture",
    "backup_sign_language",
]


def list_pushgateway_jobs() -> set:
    req = urllib.request.Request(f"{PUSHGATEWAY}/metrics")
    body = urllib.request.urlopen(req).read().decode()
    jobs = set()
    for line in body.splitlines():
        if 'job="' in line:
            start = line.index('job="') + 5
            end = line.index('"', start)
            jobs.add(line[start:end])
    return jobs


def wipe(job: str):
    req = urllib.request.Request(f"{PUSHGATEWAY}/metrics/job/{job}", method="DELETE")
    try:
        urllib.request.urlopen(req)
    except Exception:
        pass


if __name__ == "__main__":
    present = list_pushgateway_jobs()
    contaminating = [j for j in TEST_FIXTURE_JOBS if j in present]

    if not contaminating:
        print("clean: no test-fixture jobs present in Pushgateway.")
        sys.exit(0)

    print(f"CONTAMINATED: found {len(contaminating)} test-fixture job(s), wiping now:")
    for job in contaminating:
        wipe(job)
        print(f"  wiped: {job}")
    print(
        "\nWarning: Pushgateway wipe was just issued for the jobs above. Prometheus "
        "remote_write to Grafana Cloud may still show these for a few scrape cycles. "
        "Wait ~30s and re-run this script to confirm before recording."
    )
    sys.exit(1)
