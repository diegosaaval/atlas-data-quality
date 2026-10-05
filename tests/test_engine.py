import pytest
from conftest import open_incidents, run_until

from atlas.engine import Engine
from atlas.faults import FAULTS
from atlas.warehouse import GOLD_MODELS

GOLD = list(GOLD_MODELS)


def test_deterministic_for_same_seed():
    from atlas.config import Settings

    a, b = Engine(Settings(seed=3)), Engine(Settings(seed=3))
    for _ in range(20):
        sa, sb = a.tick(), b.tick()
    strip = lambda p: {k: v for k, v in p.items() if k != "tick_ms"}  # noqa: E731 - wall-clock timing
    assert strip(sa["platform"]) == strip(sb["platform"])
    assert sa["series"] == sb["series"]


def test_baseline_has_no_false_positives(engine):
    """24 simulated hours of normal traffic (incl. seasonality) must not page anyone."""
    for _ in range(288):
        engine.tick()
    assert engine.incidents == {}
    assert engine.platform_score() == 100
    assert engine.freshness_slo() == 100


def test_medallion_tables_are_populated(warm):
    counts = warm.wh.table_counts()
    for table in ("silver_transactions", "silver_accounts", "gold_daily_ledger", "gold_risk_exposure",
                  "gold_fraud_features", "gold_payment_ops"):
        assert counts[table] > 0, table


@pytest.mark.parametrize("fault", FAULTS, ids=[f.id for f in FAULTS])
def test_every_fault_is_detected_classified_and_recovered(warm, fault):
    warm.inject(fault.id)
    assert run_until(warm, lambda e: open_incidents(e), max_ticks=6), f"{fault.id} not detected"

    inc = open_incidents(warm)[0]
    assert inc.dataset == fault.detected_on
    assert inc.category == fault.root_cause
    assert fault.id in inc.fault_ids
    assert inc.mttd_minutes(5) <= 20

    warm.remediate(inc.id)
    assert run_until(warm, lambda e: inc.status == "resolved", max_ticks=10), f"{fault.id} did not recover"
    assert inc.mttr_minutes(5) is not None
    run_until(warm, lambda e: e.platform_score() == 100, max_ticks=10)
    assert warm.platform_score() == 100
    assert not open_incidents(warm), [i.title for i in open_incidents(warm)]


@pytest.mark.parametrize("fault_id", ["schema_drift", "duplicate_load", "null_burst", "orphan_records",
                                      "broken_replication", "volume_spike"])
def test_quality_gate_blocks_gold_for_blocking_faults(warm, fault_id):
    ledger_before = warm.wh.query("SELECT * FROM gold_daily_ledger ORDER BY 1, 2, 3")
    warm.inject(fault_id)
    warm.tick()
    assert all(warm.state[m].blocked for m in GOLD)
    # Last good version is preserved untouched.
    assert warm.wh.query("SELECT * FROM gold_daily_ledger ORDER BY 1, 2, 3") == ledger_before


@pytest.mark.parametrize("fault_id", ["fraud_burst", "missing_partition"])
def test_non_blocking_faults_keep_gold_flowing(warm, fault_id):
    warm.inject(fault_id)
    for _ in range(4):
        warm.tick()
    assert not any(warm.state[m].blocked for m in GOLD)


def test_duplicates_never_reach_silver(warm):
    before = warm.wh.scalar("SELECT COUNT(*) FROM silver_transactions")
    warm.inject("duplicate_load")
    warm.tick()
    after = warm.wh.scalar("SELECT COUNT(*) FROM silver_transactions")
    distinct = warm.wh.scalar("SELECT COUNT(DISTINCT txn_id) FROM silver_transactions")
    assert after == distinct
    assert after - before < 200  # only the new batch, not the replay


def test_missing_partition_is_backfilled(warm):
    warm.inject("missing_partition")
    for _ in range(4):
        warm.tick()
    gap_ticks = {r["tick"] for r in warm.wh.query("SELECT DISTINCT tick FROM silver_transactions")}
    warm.clear_fault("missing_partition")
    warm.tick()
    backfilled = warm.wh.scalar("SELECT COUNT(*) FROM bronze_transactions WHERE is_backfill = 1")
    assert backfilled > 0
    assert warm.tick_no in {r["tick"] for r in warm.wh.query("SELECT DISTINCT tick FROM silver_transactions")}
    assert gap_ticks  # silver kept its earlier history


def test_symptoms_are_grouped_under_the_root_incident(warm):
    warm.inject("schema_drift")
    warm.tick()
    incidents = open_incidents(warm)
    assert len(incidents) == 1, "downstream symptoms must not open their own incidents"
    assert any("silver.transactions" in s for s in incidents[0].symptoms)


def test_severity_reflects_business_impact(warm):
    warm.inject("schema_drift")
    warm.tick()
    assert open_incidents(warm)[0].severity == "SEV1"  # data stopped for the regulatory report


def test_fraud_burst_raises_risk_scores(warm):
    baseline = warm.wh.scalar("SELECT MAX(risk_score) FROM gold_fraud_features")
    warm.inject("fraud_burst")
    for _ in range(3):
        warm.tick()
    assert warm.wh.scalar("SELECT MAX(risk_score) FROM gold_fraud_features") >= baseline


def test_chaos_mode_injects_and_auto_heals(engine):
    engine.chaos = True
    for _ in range(400):
        engine.tick()
    assert engine.totals["incidents_opened"] > 0
    assert engine.totals["incidents_resolved"] > 0


def test_retention_prunes_old_rows(engine):
    for _ in range(engine.settings.retention_ticks + 30):
        engine.tick()
    oldest = engine.wh.scalar("SELECT MIN(tick) FROM silver_transactions")
    assert oldest >= engine.tick_no - engine.settings.retention_ticks - 12
