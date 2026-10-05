from conftest import open_incidents

from atlas import copilot


def test_template_email_has_evidence_cause_and_owner(engine):
    engine.inject("tasa_mora_negativa")
    engine.run_day()
    inc = next(i for i in open_incidents(engine) if i.table == "indicadores_cartera")
    mail = copilot.analyze(engine, inc.id, use_llm=False)
    assert mail["mode"] == "plantilla"
    assert mail["to"] == "riesgo.credito@banco.example"
    assert "[ATLAS][Crítica]" in mail["asunto"]
    assert "tasa_mora" in mail["cuerpo"] and "Riesgo de Crédito" in mail["cuerpo"]
    assert "error de cálculo" in mail["causa_probable"]
    assert "-" in mail["cuerpo"] and "9999999" not in mail["cuerpo"]  # números formateados


def test_volume_cause_distinguishes_drop_from_duplication(engine):
    engine.inject("ingesta_borrada")
    engine.inject("carga_duplicada")
    engine.run_day()
    by_table = {i.table: copilot.analyze(engine, i.id, use_llm=False) for i in open_incidents(engine)}
    assert "incompleta" in by_table["pagos"]["causa_probable"]
    assert "idempotente" in by_table["cartera_creditos"]["causa_probable"]


def test_llm_failure_falls_back_to_template(engine, monkeypatch):
    engine.inject("nulos_documento")
    engine.run_day()
    inc = next(i for i in open_incidents(engine) if i.table == "clientes")

    def boom(*_a, **_k):
        raise RuntimeError("sin red")

    monkeypatch.setattr(copilot, "claude_analysis", boom)
    mail = copilot.analyze(engine, inc.id, use_llm=True)
    assert mail["mode"] == "plantilla" and "sin red" in mail["fallback_reason"]
