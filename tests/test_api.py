import pytest
from fastapi.testclient import TestClient

from atlas import api


@pytest.fixture
def client():
    api.engine.reset()
    for _ in range(40):
        api.engine.tick()
    with TestClient(api.app) as c:
        yield c


def test_health(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").json()["status"] == "ready"


def test_state_shape(client):
    s = client.get("/api/state").json()
    assert {"platform", "datasets", "incidents", "faults", "series", "dag", "edges"} <= s.keys()
    assert s["platform"]["score"] == 100


def test_static_ui_is_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "ATLAS" in r.text


def test_dataset_detail_and_404(client):
    d = client.get("/api/datasets/gold.daily_ledger").json()
    assert d["model_sql"].startswith("SELECT")
    assert "bi.finance_pnl" in d["downstream"]
    assert client.get("/api/datasets/nope").status_code == 404


def test_viewer_cannot_inject(client):
    r = client.post("/api/faults/schema_drift", headers={"X-Atlas-Role": "viewer"})
    assert r.status_code == 403


def test_full_incident_flow_over_http(client):
    assert client.post("/api/faults/schema_drift", json={}).status_code == 201
    api.engine.tick()
    incidents = client.get("/api/incidents?status=open").json()
    assert len(incidents) == 1
    inc_id = incidents[0]["id"]

    detail = client.get(f"/api/incidents/{inc_id}").json()
    assert detail["category"] == "schema_drift" and detail["runbook"]["steps"]

    cp = client.post(f"/api/incidents/{inc_id}/copilot").json()
    assert cp["probable_root_cause"]

    assert client.post(f"/api/incidents/{inc_id}/remediate").json()["status"] == "mitigating"
    for _ in range(3):
        api.engine.tick()
    assert client.get(f"/api/incidents/{inc_id}").json()["status"] == "resolved"


def test_pii_masking_by_role(client):
    viewer = client.get("/api/risk/signals", headers={"X-Atlas-Role": "viewer"}).json()["rows"][0]
    engineer = client.get("/api/risk/signals", headers={"X-Atlas-Role": "engineer"}).json()["rows"][0]
    admin = client.get("/api/risk/signals", headers={"X-Atlas-Role": "admin"}).json()["rows"][0]
    assert "•" in viewer["account_id"] and "•" in viewer["holder_name"]
    assert "•" not in engineer["account_id"] and "•" in engineer["holder_name"]
    assert "•" not in admin["holder_name"]


def test_reset_requires_admin(client):
    assert client.post("/api/control", json={"reset": True}).status_code == 403
    assert client.post("/api/control", json={"reset": True}, headers={"X-Atlas-Role": "admin"}).status_code == 200


def test_prometheus_metrics(client):
    text = client.get("/metrics").text
    assert "atlas_platform_reliability_score 100" in text
    assert 'atlas_dataset_reliability_score{dataset="silver.transactions",layer="silver"}' in text


def test_openlineage_events(client):
    events = client.get("/api/lineage/openlineage").json()
    ledger = next(e for e in events if e["job"]["name"] == "build_gold.daily_ledger")
    assert ledger["inputs"] == [{"namespace": "atlas", "name": "silver.transactions"}]


def test_contracts_endpoint(client):
    names = {c["dataset"] for c in client.get("/api/contracts").json()}
    assert "bronze.transactions" in names


def test_websocket_streams_snapshots(client):
    with client.websocket_connect("/ws") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "snapshot"
        assert msg["data"]["tick"] == api.engine.tick_no
