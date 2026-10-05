from datetime import date

import pytest

from atlas.rules import FAIL, OK, Rule, RuleStore, baseline, evaluate
from atlas.store import Store
from atlas.tables import BY_NAME

DAY = date(2026, 10, 5)
ISO = DAY.isoformat()


def pagos(**overrides):
    row = {"id_pago": "PG-1", "id_credito": "CR-1", "fecha_pago": ISO, "valor_pago": 100.0, "canal": "app",
           "estado": "aplicado"}
    return {**row, **overrides}


@pytest.fixture
def store():
    s = Store()
    rows = [pagos(id_pago=f"PG-{i}") for i in range(10)]
    rows[0]["id_credito"] = None
    rows[1]["valor_pago"] = -5.0
    rows[2]["canal"] = "fax"
    rows.append(pagos(id_pago="PG-3"))  # duplicado
    s.insert_load(BY_NAME["pagos"], ISO, "07:00", rows, BY_NAME["pagos"].column_names)
    yield s
    s.close()


def rule(type_, **params):
    r = Rule("T1", "pagos", type_, "alta", params)
    r.validate()
    return r


@pytest.mark.parametrize(
    ("type_", "params", "bad"),
    [
        ("no_nulos", {"column": "id_credito"}, 1),
        ("rango", {"column": "valor_pago", "min": 1}, 1),
        ("valores_permitidos", {"column": "canal", "values": "app, pse, oficina, corresponsal"}, 1),
        ("unico", {"columns": ["id_pago"]}, 1),
        ("comparacion", {"column": "valor_pago", "operator": ">", "other_column": "valor_pago"}, 11),
        ("fecha_del_dia", {"column": "fecha_pago"}, 0),
        ("sql", {"condition": "estado = 'aplicado' AND valor_pago < 0"}, 1),
    ],
)
def test_row_rules_count_bad_records(store, type_, params, bad):
    result = evaluate(rule(type_, **params), store, DAY, 11)
    assert result.failing_rows == bad
    assert result.status == (FAIL if bad else OK)
    if bad:
        assert result.examples
    assert "SELECT" in result.sql


def test_tolerance_allows_a_few_nulls(store):
    assert evaluate(rule("no_nulos", column="id_credito", max_pct=20), store, DAY, 11).status == OK


@pytest.mark.parametrize(
    ("type_", "params", "message"),
    [
        ("no_nulos", {"column": "nope"}, "no existe"),
        ("rango", {"column": "canal", "min": 1}, "no es numérica"),
        ("rango", {"column": "valor_pago"}, "mínimo"),
        ("comparacion", {"column": "valor_pago", "operator": "LIKE", "other_column": "valor_pago"}, "Operador"),
        ("sql", {"condition": "1=1; DROP TABLE t_pagos"}, "solo puede leer"),
        ("sql", {"condition": "id_pago IN (SELECT 1) OR delete"}, "solo puede leer"),
    ],
)
def test_invalid_rules_are_rejected(type_, params, message):
    with pytest.raises(ValueError, match=message):
        Rule("X", "pagos", type_, "media", params).validate()


def test_sql_rules_cannot_write(store):
    r = Rule("X", "pagos", "sql", "media", {"condition": "1=1"})
    r.validate()
    store.query_readonly("SELECT 1")
    with pytest.raises(Exception, match="readonly"):
        store.query_readonly("DELETE FROM t_pagos")
    assert evaluate(r, store, DAY, 11).failing_rows == 11


def test_broken_user_rule_reports_error_instead_of_crashing(store):
    r = Rule("X", "pagos", "sql", "media", {"condition": "no_such_column > 1"})
    r.validate()
    assert evaluate(r, store, DAY, 11).status == "error"


def test_rule_descriptions_are_readable():
    assert rule("rango", column="valor_pago", min=0, max=100).describe() == "valor_pago entre 0 y 100"
    assert rule("unico", columns=["id_pago"]).describe() == "id_pago no se puede repetir"
    assert "sin valores atípicos" in rule("outlier", column="valor_pago", aggregate="suma").describe()


def test_baseline_is_robust_and_uses_same_weekday():
    history = [(date.fromordinal(DAY.toordinal() - i).isoformat(), 100.0 if i % 7 else 500.0) for i in range(56, 0, -1)]
    center, spread = baseline(history, DAY, same_weekday=True)
    assert center == 500  # mondays only
    center, _ = baseline(history, DAY, same_weekday=False)
    assert center == 100
    assert baseline(history[:3], DAY, same_weekday=False) is None


def test_rule_store_persists(tmp_path):
    path = tmp_path / "reglas.json"
    rs = RuleStore(str(path))
    n = len(rs.rules)
    new = rs.add(Rule(rs.next_id(), "pagos", "rango", "baja", {"column": "valor_pago", "max": 1e9}))
    rs.update(new.id, {"enabled": False})
    reloaded = RuleStore(str(path))
    assert len(reloaded.rules) == n + 1
    assert reloaded.rules[new.id].enabled is False
    reloaded.delete(new.id)
    assert len(RuleStore(str(path)).rules) == n
