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
    deleted = client.delete(f"/api/rules/{rid}")
    assert deleted.json() == {"deleted": True}


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


@pytest.fixture
def public(client, monkeypatch):
    from dataclasses import replace

    monkeypatch.setattr(api, "settings", replace(api.settings, public_demo=True, max_user_rules=2))
    return client


def test_public_demo_protects_base_rules_and_reset(public):
    assert public.get("/api/meta").json()["public_demo"] is True
    base = next(r["id"] for r in public.get("/api/rules").json()["rules"] if r["author"] == "sistema")
    blocked = public.delete(f"/api/rules/{base}")
    assert blocked.status_code == 403
    assert public.patch(f"/api/rules/{base}", json={"enabled": False}).status_code == 403
    assert public.post("/api/control", json={"reset": True}).status_code == 403
    assert public.post("/api/control", json={"speed": 10}).json()["speed"] == 4


def test_public_demo_lets_visitors_manage_their_own_rules_with_a_cap(public):
    body = {"table": "pagos", "type": "rango", "params": {"column": "valor_pago", "max": 1e9}}
    first = public.post("/api/rules", json=body).json()["id"]
    assert public.post("/api/rules", json=body).status_code == 201
    assert public.post("/api/rules", json=body).status_code == 429
    assert public.patch(f"/api/rules/{first}", json={"enabled": False}).status_code == 200
    own = public.delete(f"/api/rules/{first}")
    assert own.status_code == 200


def test_security_headers(client):
    r = client.get("/")
    csp = r.headers["content-security-policy"]
    assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp and "wss://testserver" in csp
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"


def test_public_demo_rate_limits_writes_per_visitor(client, monkeypatch):
    from dataclasses import replace

    monkeypatch.setattr(api, "settings", replace(api.settings, public_demo=True, max_writes_per_minute=3))
    api._writes.clear()
    codes = [client.post("/api/control", json={"speed": 1}).status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200] and codes[3:] == [429, 429]
    assert client.get("/api/state").status_code == 200  # leer nunca se limita
    api._writes.clear()


def test_runaway_sql_rule_is_cancelled_not_frozen(client):
    api.engine.run_day()
    body = {"table": "pagos", "type": "sql", "params": {
        "condition": "1 IN (WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT x FROM c WHERE x < 0)"}}
    import time

    t0 = time.monotonic()
    r = client.post("/api/rules/preview", json=body).json()
    assert time.monotonic() - t0 < 3
    assert r["status"] == "error" and "canceló" in r["message"]


def test_oversized_rules_are_rejected(client):
    long = {"table": "pagos", "type": "sql", "params": {"condition": "valor_pago > 0 AND " * 60}}
    assert client.post("/api/rules", json=long).status_code == 422
    many = {"table": "pagos", "type": "valores_permitidos", "params": {"column": "canal", "values": [str(i) for i in range(200)]}}
    assert client.post("/api/rules", json=many).status_code == 422


def test_sources_listing_and_switching(client, monkeypatch):
    data = client.get("/api/sources").json()
    assert data["active"] == "demo" and data["sources"][0]["name"] == "demo"
    assert client.post("/api/source", json={"name": "no_existe"}).status_code == 422
    assert client.get("/api/state").json()["mode"] == "simulacion"  # si falla, sigue en la demo
    assert client.post("/api/source", json={"name": "demo"}).json()["mode"] == "simulacion"
    with pytest.raises(ValueError):
        api.engine.source = object()  # cualquier fuente real bloquea los controles de simulación
        api.engine.run_day()
    api.engine.source = None


def test_public_demo_cannot_switch_source(public):
    assert public.post("/api/source", json={"name": "finflow"}).status_code == 403
    assert [s["name"] for s in public.get("/api/sources").json()["sources"]] == ["demo"]
