import pytest
from fastapi.testclient import TestClient

from atlas import api


@pytest.fixture
def client():
    api.engine.reset()
    with TestClient(api.app) as c:
        yield c


def test_health_and_static(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    assert "ATLAS" in client.get("/").text


def test_state(client):
    s = client.get("/api/state").json()
    assert {"kpis", "tables", "incidents", "events", "scenarios", "score_series"} <= s.keys()
    assert len(s["tables"]) == 6


def test_table_detail_and_404(client):
    api.engine.run_day()
    d = client.get("/api/tables/cartera_creditos").json()
    assert d["spec"]["owner"] == "Riesgo de Crédito" and d["results"]
    assert client.get("/api/tables/nope").status_code == 404


def test_rules_crud_and_preview(client):
    api.engine.run_day()
    body = {"table": "indicadores_cartera", "type": "rango", "severity": "critica",
            "params": {"column": "tasa_mora", "min": 0}, "note": "nunca negativa"}
    preview = client.post("/api/rules/preview", json=body).json()
    assert preview["status"] == "ok" and "tasa_mora < 0" in preview["sql"]
    created = client.post("/api/rules", json=body)
    assert created.status_code == 201
    rid = created.json()["id"]
    assert client.patch(f"/api/rules/{rid}", json={"enabled": False}).json()["enabled"] is False
    assert any(r["id"] == rid for r in client.get("/api/rules").json()["rules"])
    assert client.delete(f"/api/rules/{rid}").json() == {"deleted": True}


def test_invalid_rule_returns_422_with_spanish_message(client):
    r = client.post("/api/rules", json={"table": "pagos", "type": "rango", "params": {"column": "canal", "min": 0}})
    assert r.status_code == 422 and "no es numérica" in r.json()["detail"]


def test_full_incident_flow(client):
    assert client.post("/api/scenarios/carga_duplicada").json()["when"] == "hoy"
    api.engine.run_day()
    open_ = client.get("/api/incidents?status=abierto").json()
    inc_id = next(i["id"] for i in open_ if i["table"] == "cartera_creditos")
    mail = client.post(f"/api/incidents/{inc_id}/copilot").json()
    assert mail["asunto"].startswith("[ATLAS]")
    assert client.post(f"/api/incidents/{inc_id}/escalate").json()["status"] == "escalado"
    done = client.post(f"/api/incidents/{inc_id}/resolve", json={"note": "recargada"}).json()
    assert done["status"] == "resuelto" and done["resolution"] == "manual"


def test_control_next_day(client):
    day = client.get("/api/state").json()["date"]
    r = client.post("/api/control", json={"next_day": True, "running": False, "speed": 2})
    assert r.json() == {"running": False, "speed": 2.0, "random_anomalies": False}
    s = client.get("/api/state").json()
    assert s["date"] > day and s["time"] == "05:30"


def test_metrics(client):
    api.engine.run_day()
    text = client.get("/metrics").text
    assert "atlas_quality_score" in text and 'atlas_table_rows{tabla="pagos"}' in text


def test_websocket(client):
    with client.websocket_connect("/ws") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "snapshot" and msg["data"]["date"]
