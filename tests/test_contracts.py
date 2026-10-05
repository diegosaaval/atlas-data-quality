import pytest

from atlas.contracts import get_contract, load_contracts

VALID_TXN = {
    "txn_id": "TXN-000001-0001", "account_id": "ACC-000001", "merchant_id": "MER-0001",
    "merchant_category": "grocery", "channel": "app", "amount": 125000.0, "currency": "COP",
    "event_time": "2026-10-01T08:00:00", "status": "approved",
}


def test_contracts_load():
    contracts = load_contracts()
    assert {"bronze.transactions", "bronze.accounts", "bronze.fx_rates"} <= contracts.keys()


def test_valid_record_passes():
    assert get_contract("bronze.transactions").validate(VALID_TXN) == []


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("amount", None, "amount: required"),
        ("amount", -5, "amount: -5 < min 0"),
        ("amount", "100", "amount: expected number, got str"),
        ("currency", "EUR", "currency: 'EUR' not in enum"),
        ("txn_id", "X-1", "txn_id: does not match ^TXN-"),
        ("event_time", "yesterday", "event_time: invalid timestamp"),
    ],
)
def test_record_violations(field, value, expected):
    record = {**VALID_TXN, field: value}
    assert expected in get_contract("bronze.transactions").validate(record)


def test_schema_diff_detects_drift():
    contract = get_contract("bronze.transactions")
    observed = (contract.column_names - {"amount"}) | {"amount_value", "fee"}
    assert contract.schema_diff(observed) == {"missing": ["amount"], "unexpected": ["amount_value", "fee"]}


def test_pii_is_classified():
    cols = {c.name: c for c in get_contract("bronze.accounts").columns}
    assert cols["holder_name"].classification == "pii"
