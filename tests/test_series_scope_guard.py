"""Regression test for the fixture-contamination bug: a real fault must never read fresh.

The bug, reproduced against the live rig before the fix:

    sign_feed_freshness_seconds{job="media_pipeline_sign",  mode="frozen"}    = 8.10
    sign_feed_freshness_seconds{job="media_pipeline_sign",  mode="baseline"}  = 0.0107
    sign_feed_freshness_seconds{job="accessibility_layers"}                   = 0.20

    min(sign_feed_freshness_seconds{layer="sign_language"}) -> 0.0107

An 8.1-second frozen-feed fault reported as 0.0107s. The label sets below are the REAL ones
observed on the running stack, transcribed -- not invented for the test.

Run: python tests/test_series_scope_guard.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "agent"))

from series_scope_guard import check_scope, metric_name_of, pinned_labels

# Real label sets, as observed on the live rig (see module docstring).
CONTAMINATED_SIGN_SERIES = {
    "sign_feed_freshness_seconds": [
        {"job": "media_pipeline_sign", "mode": "frozen", "layer": "sign_language"},
        {"job": "media_pipeline_sign", "mode": "baseline", "layer": "sign_language"},
        {"job": "accessibility_layers", "layer": "sign_language"},
    ],
}

SINGLE_SERIES = {
    "caption_cue_sync_offset_seconds": [
        {"job": "media_pipeline_captions", "mode": "frozen_captions", "layer": "captions"},
    ],
}


def test_underscoped_query_is_refused():
    """THE regression: the exact query shape that let an 8.1s fault read as 0.0107s."""
    expr = 'sign_feed_freshness_seconds{layer="sign_language"}'
    result = check_scope(CONTAMINATED_SIGN_SERIES, [expr])
    assert not result.ok, "under-scoped query must be refused -- this is the 8.1s-reads-fresh bug"
    assert any(f["unpinned_label"] == "job" for f in result.findings), result.findings
    print("  PASS: under-scoped layer query refused")
    print(f"        {result.detail}")


def test_job_pinned_but_mode_free_is_refused():
    """Pinning job alone is NOT enough: baseline (0.0107) and frozen (8.10) share a job, so
    a job-only query can still let the healthy run answer for the faulted one."""
    expr = 'sign_feed_freshness_seconds{job="media_pipeline_sign"}'
    result = check_scope(CONTAMINATED_SIGN_SERIES, [expr])
    assert not result.ok, "job-only query must still be refused while mode is ambiguous"
    assert any(f["unpinned_label"] == "mode" for f in result.findings), result.findings
    print("  PASS: job-pinned but mode-free query refused")


def test_fully_scoped_query_is_allowed():
    """The correct query -- pins both discriminating labels -- must NOT be refused."""
    expr = 'sign_feed_freshness_seconds{job="media_pipeline_sign",mode="frozen"}'
    result = check_scope(CONTAMINATED_SIGN_SERIES, [expr])
    assert result.ok, f"correctly-scoped query must pass, got: {result.detail}"
    print("  PASS: fully-scoped query allowed (no over-refusal)")


def test_single_series_metric_needs_no_pinning():
    """No over-refusal: a metric with exactly one series cannot be confused with anything,
    so an unpinned query against it is safe and must be allowed."""
    expr = 'caption_cue_sync_offset_seconds{layer="captions"}'
    result = check_scope(SINGLE_SERIES, [expr])
    assert result.ok, f"single-series metric must not be refused, got: {result.detail}"
    print("  PASS: single-series metric allowed unpinned")


def test_regex_matcher_does_not_count_as_pinned():
    """mode=~".*" selects every mode -- it must not be mistaken for pinning the label."""
    assert "mode" not in pinned_labels('sign_feed_freshness_seconds{mode=~".*"}')
    assert "job" in pinned_labels('sign_feed_freshness_seconds{job="media_pipeline_sign"}')
    print("  PASS: regex matcher not counted as pinning")


def test_metric_name_parsing():
    assert metric_name_of('sign_feed_freshness_seconds{job="x"}') == "sign_feed_freshness_seconds"
    assert metric_name_of("not an expression") is None
    print("  PASS: metric-name parsing")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print(f"running {len(tests)} regression tests for the fixture-contamination bug\n")
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failed += 1
            print(f"  FAIL: {t.__name__}: {e}")
    print()
    if failed:
        print(f"{failed}/{len(tests)} FAILED")
        sys.exit(1)
    print(f"{len(tests)}/{len(tests)} passed")
