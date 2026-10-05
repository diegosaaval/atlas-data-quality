from collections import Counter

import pytest
from conftest import make_engine, next_day, open_incidents

from atlas.tables import SCENARIOS

EXPECTED = {  # escenario -> tipo de control que debe detectarlo
    "no_llega": "disponibilidad", "llega_tarde": "disponibilidad", "ingesta_borrada": "volumen",
    "carga_parcial": "volumen", "carga_duplicada": "unico", "archivo_de_ayer": "fecha_del_dia",
    "saldo_mayor_monto": "comparacion", "tasa_mora_negativa": "rango", "pico_desembolsos": "outlier",
    "nulos_documento": "no_nulos", "canal_invalido": "valores_permitidos", "columna_eliminada": "estructura",
    "trm_atipica": "sql",
}


def test_every_scenario_has_an_expected_detection():
    assert set(EXPECTED) == {s.id for s in SCENARIOS}


def test_history_is_built_with_the_seeded_incidents(engine):
    seeded = {i.scenario for i in engine.incidents.values() if i.scenario}
    assert {"ingesta_borrada", "carga_duplicada", "pico_desembolsos", "tasa_mora_negativa",
            "nulos_documento", "llega_tarde"} <= seeded
    assert all(i.resolved for i in engine.incidents.values() if i.scenario)
    assert len(engine.store.query("SELECT DISTINCT fecha FROM metrics WHERE tabla = '_global'")) == 70


def test_false_positive_rate_is_low():
    """Sobre 120 días normales (720 cargas) casi nunca se alerta sin motivo."""
    engine = make_engine()
    before = set(engine.incidents)
    for _ in range(120):
        next_day(engine)
    false_positives = [i for k, i in engine.incidents.items() if k not in before]
    engine.close()
    assert len(false_positives) / (120 * 6) < 0.015, Counter((i.table, i.title) for i in false_positives)


@pytest.mark.parametrize("scenario", list(EXPECTED))
def test_every_anomaly_is_detected_and_auto_resolved(engine, scenario):
    engine.inject(scenario)
    engine.run_day()
    sc = next(s for s in SCENARIOS if s.id == scenario)
    incs = [i for i in engine.incidents.values() if i.table == sc.table and i.opened[0] == engine.today.isoformat()]
    assert incs, f"{scenario} no fue detectado"
    inc = incs[0]
    assert EXPECTED[scenario] in {c["rule_type"] for c in inc.checks} | {c["check_id"] for c in inc.checks}
    assert inc.scenario == scenario
    if scenario == "llega_tarde":  # la tabla llega después y el incidente se cierra solo ese mismo día
        assert inc.resolved and "llegó" in inc.timeline[-1]["text"]
        return
    assert inc.resolved is None
    next_day(engine)
    engine.run_day()
    assert inc.resolved is not None and inc.resolution == "automática"


def test_one_incident_per_table_groups_all_failing_checks(engine):
    engine.inject("carga_duplicada")
    engine.run_day()
    incs = [i for i in open_incidents(engine) if i.table == "cartera_creditos"]
    assert len(incs) == 1
    kinds = {c["check_id"] for c in incs[0].checks}
    assert "volumen" in kinds and len(kinds) >= 2
    assert incs[0].severity == "critica"
    assert any(t["kind"] == "notificado" for t in incs[0].timeline)


def test_persisting_failure_keeps_the_same_incident(engine):
    engine.inject("tasa_mora_negativa")
    engine.run_day()
    next_day(engine)
    engine.inject("tasa_mora_negativa")
    engine.run_day()
    incs = [i for i in engine.incidents.values() if i.table == "indicadores_cartera" and i.scenario]
    latest = max(incs, key=lambda i: i.opened)
    assert latest.loads_failed == 2 and latest.resolved is None


def test_missing_table_is_flagged_after_grace_period(engine):
    engine.inject("no_llega")
    while engine.minute < 8 * 60 - 15:
        engine.tick()
    assert engine.tables["pagos"].status == "retrasada"
    engine.tick()
    engine.tick()
    assert engine.tables["pagos"].status == "no_disponible"
    assert any(i.table == "pagos" for i in open_incidents(engine))


def test_escalate_and_manual_resolve(engine):
    engine.inject("nulos_documento")
    engine.run_day()
    inc = next(i for i in open_incidents(engine) if i.table == "clientes")
    engine.escalate(inc.id)
    assert inc.status == "escalado"
    engine.resolve(inc.id, "Se completaron los documentos en origen")
    assert inc.resolved and inc.resolution == "manual"
    assert "Se completaron" in inc.timeline[-1]["text"]


def test_disabled_rules_are_not_evaluated(engine):
    rule = next(r for r in engine.rules.rules.values() if r.table == "indicadores_cartera" and r.type == "rango")
    engine.rules.update(rule.id, {"enabled": False})
    engine.run_day()
    assert rule.id not in {r.check_id for r in engine.tables["indicadores_cartera"].results}


def test_scenario_after_arrival_is_queued_for_tomorrow(engine):
    engine.run_day()
    assert engine.inject("canal_invalido")["when"] == "mañana"
    next_day(engine)
    engine.run_day()
    assert any(i.table == "pagos" and i.scenario == "canal_invalido" for i in engine.incidents.values())


def test_random_anomalies_mode_generates_incidents():
    engine = make_engine()
    engine.random_anomalies = True
    before = len(engine.incidents)
    for _ in range(20):
        next_day(engine)
    engine.close()
    assert len(engine.incidents) > before


def test_snapshot_and_table_detail_shapes(engine):
    engine.run_day()
    snap = engine.snapshot()
    assert snap["kpis"]["tables_arrived"] == 6
    assert len(snap["score_series"]) >= 60
    detail = engine.table_detail("pagos")
    assert detail["volume"] and detail["heatmap"]["rows"] and detail["profile"]
    assert any(p["lo"] is not None for p in detail["volume"])
