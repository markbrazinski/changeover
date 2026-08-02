# Test-only scripts — quarantined from the demo path

**Do not run these before or during a recorded demo.** They push synthetic fixture
metrics under distinct job names (`media_pipeline_sign_stale_fixture`,
`media_pipeline_sign_partial_fixture`, `backup_sign_language`, etc.) that exist only to
exercise the evidence-quality gate (`agent/evidence_gate.py`) and the backup-health check
(`agent/failover_tool.py`) in isolation.

**Known incident:** during development, leftover fixtures from these scripts were left
running in the same Pushgateway/Prometheus/Grafana Cloud instance used for the real demo
pipeline. This caused the diagnosis agent to see the fixtures' `mode` labels mixed in
alongside real production data (`media_pipeline_sign`) and, on one run, misjudge a real
8-second fault as "fresh." See `docs/reports/2026-08-02-task2-stale-partial-gate.md` for
the full incident writeup (not included in this public repo -- ask the project owner if
you need it).

## Before recording or presenting a live demo

Run `scripts/test-only/wipe_test_fixtures.py` to confirm no test-fixture job is present
in Pushgateway. It exits non-zero and lists anything found if the rig is contaminated.

## Files here

- `seed_evidence_quality_fixtures.py` — pushes stale/partial/fresh_control fixtures
- `backup_health_exporter.py` — pushes healthy/broken backup-telemetry fixtures
- `run_task2_matrix.sh` — the 15-run frozen-bar test driver
- `reverify_arm1.py` — offline re-verification of saved Arm1 transcripts
- `wipe_test_fixtures.py` — cleanup tool; run this before any demo
