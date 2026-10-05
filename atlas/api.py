"""HTTP / WebSocket API and static UI.

Run locally:  uvicorn atlas.api:app --reload
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__, catalog, copilot
from .config import get_settings
from .contracts import load_contracts
from .engine import Engine
from .faults import FAULTS_BY_ID
from .warehouse import GOLD_MODELS

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
Role = Literal["viewer", "engineer", "admin"]
ROLE_RANK = {"viewer": 0, "engineer": 1, "admin": 2}


class Hub:
    """Fan-out of engine snapshots to every connected WebSocket client."""

    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()

    async def broadcast(self, payload: dict[str, Any]) -> None:
        message = json.dumps({"type": "snapshot", "data": payload}, default=str)
        dead = []
        for ws in self.clients:
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)


settings = get_settings()
engine = Engine(settings)
hub = Hub()


async def run_loop() -> None:
    while True:
        if engine.running:
            snapshot = await asyncio.to_thread(engine.tick)
            await hub.broadcast(snapshot)
        await asyncio.sleep(settings.tick_seconds / max(engine.speed, 0.1))


@asynccontextmanager
async def lifespan(_: FastAPI):
    task = None
    if settings.autostart:
        for _ in range(36):  # three simulated hours of history so charts are never empty
            engine.tick()
        task = asyncio.create_task(run_loop())
    yield
    if task:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


app = FastAPI(
    title="ATLAS ONE",
    version=__version__,
    description="Reference implementation of a data reliability platform for financial data.",
    lifespan=lifespan,
)


# ------------------------------------------------------------------ security
def current_role(x_atlas_role: Annotated[str | None, Header()] = None) -> Role:
    role = (x_atlas_role or settings.default_role).lower()
    if role not in ROLE_RANK:
        raise HTTPException(400, f"Unknown role '{role}'")
    return role  # type: ignore[return-value]


def require(minimum: Role):
    def checker(role: Role = Depends(current_role)) -> Role:
        if ROLE_RANK[role] < ROLE_RANK[minimum]:
            raise HTTPException(403, f"Role '{role}' cannot perform this action (requires {minimum})")
        return role
    return checker


def mask(value: str | None, keep: int = 2) -> str | None:
    if not value:
        return value
    return value[:4] + "•" * max(0, len(value) - 4 - keep) + value[-keep:]


async def publish() -> dict[str, Any]:
    snapshot = engine.snapshot()
    await hub.broadcast(snapshot)
    return snapshot


# ---------------------------------------------------------------------- meta
@app.get("/healthz", tags=["ops"])
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz", tags=["ops"])
def readyz() -> dict[str, Any]:
    return {"status": "ready" if engine.tick_no >= 0 else "starting", "tick": engine.tick_no}


@app.get("/api/meta", tags=["meta"])
def meta(role: Role = Depends(current_role)) -> dict[str, Any]:
    return {
        "version": __version__,
        "role": role,
        "github_url": settings.github_url,
        "copilot_backend": "claude" if os.getenv("ANTHROPIC_API_KEY") else "rules",
        "tick_minutes": settings.tick_minutes,
        "tick_seconds": settings.tick_seconds,
    }


@app.get("/api/state", tags=["observability"])
def state() -> dict[str, Any]:
    return engine.snapshot()


# ------------------------------------------------------------------ datasets
@app.get("/api/datasets", tags=["catalog"])
def datasets() -> list[dict[str, Any]]:
    return [engine.dataset_dict(d.id) for d in catalog.DATASETS]


@app.get("/api/datasets/{dataset_id}", tags=["catalog"])
def dataset(dataset_id: str) -> dict[str, Any]:
    if dataset_id not in catalog.BY_ID:
        raise HTTPException(404, "Unknown dataset")
    data = engine.dataset_dict(dataset_id)
    data["history"] = engine.wh.query(
        """SELECT tick, check_name, status, value FROM check_results
           WHERE dataset = ? AND tick > ? ORDER BY tick""",
        (dataset_id, engine.tick_no - 48),
    )
    data["downstream"] = [d.id for d in catalog.blast_radius(dataset_id)]
    data["model_sql"] = " ".join(GOLD_MODELS[dataset_id].split()) if dataset_id in GOLD_MODELS else None
    return data


@app.get("/api/lineage", tags=["catalog"])
def lineage() -> dict[str, Any]:
    return {"nodes": [engine.dataset_dict(d.id) for d in catalog.DATASETS], "edges": catalog.edges()}


@app.get("/api/lineage/openlineage", tags=["catalog"])
def openlineage() -> list[dict[str, Any]]:
    """Last run expressed as OpenLineage RunEvents (one per derived dataset)."""
    when = engine.sim_time().isoformat() + "Z"
    events = []
    for d in catalog.DATASETS:
        if d.layer not in ("silver", "gold"):
            continue
        events.append({
            "eventType": "FAIL" if engine.state[d.id].status == "critical" else "COMPLETE",
            "eventTime": when,
            "producer": f"https://github.com/atlas-one/atlas/v{__version__}",
            "run": {"runId": f"{d.id}-{engine.tick_no}"},
            "job": {"namespace": "atlas", "name": f"build_{d.id}"},
            "inputs": [{"namespace": "atlas", "name": u} for u in d.upstream],
            "outputs": [{"namespace": "atlas", "name": d.id,
                         "facets": {"dataQualityMetrics": {"rowCount": engine.state[d.id].last_rows}}}],
        })
    return events


@app.get("/api/contracts", tags=["governance"])
def contracts() -> list[dict[str, Any]]:
    violations = {r["dataset"]: r["n"] for r in engine.wh.query(
        "SELECT dataset, COUNT(*) AS n FROM quarantine GROUP BY dataset")}
    out = []
    for c in load_contracts().values():
        silver = c.dataset.replace("bronze.", "silver.")
        out.append({**c.to_dict(), "quarantined_rows": violations.get(silver, 0)})
    return out


@app.get("/api/models", tags=["catalog"])
def models() -> dict[str, str]:
    return {k: " ".join(v.split()) for k, v in GOLD_MODELS.items()}


@app.get("/api/risk/signals", tags=["data-products"])
def risk_signals(role: Role = Depends(current_role), limit: int = Query(10, le=50)) -> dict[str, Any]:
    rows = engine.wh.query(
        """SELECT f.*, a.holder_name, a.segment, a.risk_tier
           FROM gold_fraud_features f LEFT JOIN silver_accounts a USING (account_id)
           ORDER BY risk_score DESC LIMIT :limit""",
        {"limit": limit},
    )
    for r in rows:
        if role != "admin":
            r["holder_name"] = mask(r["holder_name"], keep=0)
        if role == "viewer":
            r["account_id"] = mask(r["account_id"])
    return {"role": role, "masked": role != "admin", "rows": rows,
            "stale": engine.state["gold.fraud_features"].status != "healthy"}


# ----------------------------------------------------------------- incidents
@app.get("/api/incidents", tags=["incidents"])
def incidents(status: str | None = None) -> list[dict[str, Any]]:
    items = sorted(engine.incidents.values(), key=lambda i: -i.opened_tick)
    return [engine.incident_dict(i) for i in items if status is None or i.status == status]


@app.get("/api/incidents/{incident_id}", tags=["incidents"])
def incident(incident_id: str) -> dict[str, Any]:
    inc = engine.incidents.get(incident_id)
    if not inc:
        raise HTTPException(404, "Unknown incident")
    return engine.incident_dict(inc, full=True)


@app.post("/api/incidents/{incident_id}/copilot", tags=["incidents"])
def incident_copilot(incident_id: str, refresh: bool = False) -> dict[str, Any]:
    if incident_id not in engine.incidents:
        raise HTTPException(404, "Unknown incident")
    if refresh or incident_id not in engine.copilot_cache:
        engine.copilot_cache[incident_id] = copilot.analyze(engine, incident_id)
    return engine.copilot_cache[incident_id]


@app.post("/api/incidents/{incident_id}/remediate", tags=["incidents"])
async def remediate(incident_id: str, role: Role = Depends(require("engineer"))) -> dict[str, Any]:
    if incident_id not in engine.incidents:
        raise HTTPException(404, "Unknown incident")
    inc = engine.remediate(incident_id, actor=role)
    await publish()
    return engine.incident_dict(inc, full=True)


# -------------------------------------------------------------- failure lab
class InjectRequest(BaseModel):
    auto_heal_ticks: int | None = Field(None, ge=1, le=100)


@app.get("/api/faults", tags=["failure-lab"])
def faults() -> list[dict[str, Any]]:
    return engine.snapshot()["faults"]


@app.post("/api/faults/{fault_id}", tags=["failure-lab"], status_code=201)
async def inject(fault_id: str, body: InjectRequest | None = None,
                 role: Role = Depends(require("engineer"))) -> dict[str, Any]:
    if fault_id not in FAULTS_BY_ID:
        raise HTTPException(404, "Unknown fault")
    fault = engine.inject(fault_id, body.auto_heal_ticks if body else None, actor=role)
    await publish()
    return fault.to_dict()


@app.delete("/api/faults/{fault_id}", tags=["failure-lab"])
async def clear(fault_id: str, role: Role = Depends(require("engineer"))) -> dict[str, bool]:
    cleared = engine.clear_fault(fault_id, actor=role)
    await publish()
    return {"cleared": cleared}


class ControlRequest(BaseModel):
    running: bool | None = None
    speed: float | None = Field(None, ge=0.25, le=10)
    chaos: bool | None = None
    reset: bool = False


@app.post("/api/control", tags=["ops"])
async def control(body: ControlRequest, role: Role = Depends(require("engineer"))) -> dict[str, Any]:
    if body.reset:
        if ROLE_RANK[role] < ROLE_RANK["admin"]:
            raise HTTPException(403, "Reset requires admin")
        engine.reset()
        for _ in range(36):
            engine.tick()
    if body.running is not None:
        engine.running = body.running
    if body.speed is not None:
        engine.speed = body.speed
    if body.chaos is not None:
        engine.chaos = body.chaos
    snapshot = await publish()
    return {"running": snapshot["running"], "speed": snapshot["speed"], "chaos": snapshot["chaos"]}


@app.post("/api/tick", tags=["ops"])
async def manual_tick(role: Role = Depends(require("engineer"))) -> dict[str, Any]:
    """Advance one pipeline run (useful while paused)."""
    snapshot = await asyncio.to_thread(engine.tick)
    await hub.broadcast(snapshot)
    return {"tick": snapshot["tick"]}


# ------------------------------------------------------------------- metrics
@app.get("/metrics", response_class=PlainTextResponse, tags=["ops"])
def metrics() -> str:
    snap = engine.snapshot()
    p = snap["platform"]
    lines = [
        "# HELP atlas_platform_reliability_score Criticality-weighted reliability score (0-100).",
        "# TYPE atlas_platform_reliability_score gauge",
        f"atlas_platform_reliability_score {p['score']}",
        "# HELP atlas_freshness_slo_ratio Share of tier-1 runs within freshness SLA (%).",
        "# TYPE atlas_freshness_slo_ratio gauge",
        f"atlas_freshness_slo_ratio {p['freshness_slo']}",
        "# TYPE atlas_open_incidents gauge",
        f"atlas_open_incidents {p['open_incidents']}",
        "# TYPE atlas_rows_ingested_total counter",
        f"atlas_rows_ingested_total {p['rows_ingested']}",
        "# TYPE atlas_rows_quarantined_total counter",
        f"atlas_rows_quarantined_total {p['rows_quarantined']}",
        "# TYPE atlas_rows_blocked_total counter",
        f"atlas_rows_blocked_total {p['rows_blocked']}",
        "# TYPE atlas_tick_duration_ms gauge",
        f"atlas_tick_duration_ms {p['tick_ms']}",
        "# TYPE atlas_dataset_reliability_score gauge",
    ]
    for d in snap["datasets"]:
        lines.append(f'atlas_dataset_reliability_score{{dataset="{d["id"]}",layer="{d["layer"]}"}} {d["score"]}')
    lines.append("# TYPE atlas_dataset_freshness_minutes gauge")
    for d in snap["datasets"]:
        if d["freshness_minutes"] is not None and d["layer"] != "consumer":
            lines.append(f'atlas_dataset_freshness_minutes{{dataset="{d["id"]}"}} {d["freshness_minutes"]}')
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------- realtime
@app.websocket("/ws")
async def websocket(ws: WebSocket) -> None:
    await ws.accept()
    hub.clients.add(ws)
    try:
        await ws.send_text(json.dumps({"type": "snapshot", "data": engine.snapshot()}, default=str))
        while True:
            await ws.receive_text()  # keep-alive pings from the client
    except WebSocketDisconnect:
        pass
    finally:
        hub.clients.discard(ws)


app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
