from atlas import checks
from atlas.contracts import get_contract


def test_freshness_thresholds():
    assert checks.freshness("d", 0, 15, 5).status == "pass"
    assert checks.freshness("d", 12, 15, 5).status == "warn"
    assert checks.freshness("d", 20, 15, 5).status == "fail"


def test_volume_needs_baseline_then_flags_spikes_and_drops():
    assert checks.volume("d", 1000, [100] * 3).status == "pass"  # warming up
    history = [100, 95, 105, 98, 102, 100]
    assert checks.volume("d", 110, history).status == "pass"
    assert checks.volume("d", 800, history).status == "fail"
    assert checks.volume("d", 10, history).status == "fail"
    assert checks.volume("d", 250, history).status == "warn"


def test_schema_fails_only_when_required_column_missing():
    contract = get_contract("bronze.transactions")
    assert checks.schema("d", contract, contract.column_names).status == "pass"
    assert checks.schema("d", contract, contract.column_names | {"extra"}).status == "warn"
    assert checks.schema("d", contract, contract.column_names - {"amount"}).status == "fail"


def test_reconciliation():
    assert checks.reconciliation("d", 100, 1000.0, {"count": 100, "amount": 1000.0}).status == "pass"
    assert checks.reconciliation("d", 100, 1000.0, {"count": 70, "amount": 660.0}).status == "fail"


def test_blocking_only_for_failed_blocking_dimensions():
    fail_schema = checks.CheckResult("d", "s", "schema", "fail", 1, "", "")
    warn_schema = checks.CheckResult("d", "s", "schema", "warn", 1, "", "")
    fail_fresh = checks.CheckResult("d", "f", "freshness", "fail", 1, "", "")
    assert fail_schema.blocking and not warn_schema.blocking and not fail_fresh.blocking


def test_score_is_bounded():
    many_fails = [checks.CheckResult("d", "x", "pipeline", "fail", 1, "", "")] * 10
    assert checks.dataset_score(many_fails) == 0
    assert checks.dataset_score([]) == 100
