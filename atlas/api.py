"""API HTTP / WebSocket y la interfaz web.

Local:  uvicorn atlas.api:app --reload
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__, copilot
from .config import get_settings
from .engine import Engine
from .rules import AGGREGATES, OPERATORS, RULE_TYPES, SEVERITIES, SEVERITY_LABEL, Rule
from .tables import BY_NAME, SCENARIOS_BY_ID, TABLES

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


class Hub:
    """Envía cada snapshot del motor a todos los navegadores conectados."""

    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()

    async def broadcast(self, payload: dict[str, Any]) -> None:
        message = json.dumps({"type": "snapshot", "data": payload}, default=str, ensure_ascii=False)
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
    paused_since: float | None = None
    while True:
        if settings.public_demo and not engine.running:  # en la demo pública nadie puede dejarla en pausa
            paused_since = paused_since or time.monotonic()
            if time.monotonic() - paused_since > settings.max_pause_seconds:
                engine.running, engine.speed = True, 1.0
        else:
            paused_since = None
        if engine.running:
            snapshot = await asyncio.to_thread(engine.tick)
            await hub.broadcast(snapshot)
        await asyncio.sleep(settings.tick_seconds / max(engine.speed, 0.1))


@asynccontextmanager
async def lifespan(_: FastAPI):
    task = asyncio.create_task(run_loop()) if settings.autostart else None
    yield
    if task:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


app = FastAPI(title="ATLAS · Monitor de calidad de datos", version=__version__, lifespan=lifespan,
              description="Valida las tablas que se cargan cada día y gestiona los incidentes de calidad.")


async def publish() -> dict[str, Any]:
    snapshot = engine.snapshot()
    await hub.broadcast(snapshot)
    return snapshot


def _incident_or_404(incident_id: str):
    inc = engine.incidents.get(incident_id)
    if not inc:
        raise HTTPException(404, "Incidente no encontrado")
    return inc


# ----------------------------------------------------------------- general
@app.get("/healthz", tags=["operación"])
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz", tags=["operación"])
def readyz() -> dict[str, Any]:
    return {"status": "ready", "date": engine.today.isoformat()}


@app.get("/api/meta", tags=["general"])
def meta() -> dict[str, Any]:
    return {"version": __version__, "github_url": settings.github_url,
            "copilot": "claude" if os.getenv("ANTHROPIC_API_KEY") else "plantilla", "public_demo": settings.public_demo}


@app.get("/api/state", tags=["general"])
def state() -> dict[str, Any]:
    return engine.snapshot()


# ------------------------------------------------------------------ tablas
@app.get("/api/tables", tags=["tablas"])
def tables() -> list[dict[str, Any]]:
    return [t.to_dict() for t in TABLES]


@app.get("/api/tables/{name}", tags=["tablas"])
def table(name: str) -> dict[str, Any]:
    if name not in BY_NAME:
        raise HTTPException(404, "Tabla no encontrada")
    return engine.table_detail(name)


# --------------------------------------------------------------- incidentes
@app.get("/api/incidents", tags=["incidentes"])
def incidents(status: str | None = None) -> list[dict[str, Any]]:
    items = sorted(engine.incidents.values(), key=lambda i: i.opened, reverse=True)
    return [engine.incident_dict(i) for i in items if status is None or i.status == status]


@app.get("/api/incidents/{incident_id}", tags=["incidentes"])
def incident(incident_id: str) -> dict[str, Any]:
    return engine.incident_dict(_incident_or_404(incident_id), full=True)


@app.post("/api/incidents/{incident_id}/copilot", tags=["incidentes"])
def incident_copilot(incident_id: str, refresh: bool = False) -> dict[str, Any]:
    _incident_or_404(incident_id)
    if refresh or incident_id not in engine.copilot_cache:
        engine.copilot_cache[incident_id] = copilot.analyze(engine, incident_id)
    return engine.copilot_cache[incident_id]


@app.post("/api/incidents/{incident_id}/escalate", tags=["incidentes"])
async def escalate(incident_id: str) -> dict[str, Any]:
    _incident_or_404(incident_id)
    inc = engine.escalate(incident_id)
    await publish()
    return engine.incident_dict(inc, full=True)


class ResolveRequest(BaseModel):
    note: str = Field("", max_length=500)


@app.post("/api/incidents/{incident_id}/resolve", tags=["incidentes"])
async def resolve(incident_id: str, body: ResolveRequest) -> dict[str, Any]:
    _incident_or_404(incident_id)
    inc = engine.resolve(incident_id, body.note)
    await publish()
    return engine.incident_dict(inc, full=True)


# ------------------------------------------------------------------- reglas
class RuleRequest(BaseModel):
    table: str
    type: str
    severity: str = "media"
    params: dict[str, Any] = Field(default_factory=dict)
    note: str = Field("", max_length=300)
    enabled: bool = True


@app.get("/api/rules", tags=["reglas"])
def rules() -> dict[str, Any]:
    return {
        "rules": [r.to_dict() for r in engine.rules.rules.values()],
        "types": RULE_TYPES, "operators": OPERATORS, "aggregates": list(AGGREGATES),
        "severities": {s: SEVERITY_LABEL[s] for s in SEVERITIES},
        "tables": {t.name: [{"name": c.name, "type": c.type} for c in t.columns] for t in TABLES},
    }


def _build_rule(body: RuleRequest, rule_id: str) -> Rule:
    rule = Rule(id=rule_id, table=body.table, type=body.type, severity=body.severity, params=dict(body.params),
                enabled=body.enabled, note=body.note, author="usuario")
    try:
        rule.validate()
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return rule


def _protect_system_rule(rule_id: str) -> None:
    if settings.public_demo and engine.rules.rules[rule_id].author != "usuario":
        raise HTTPException(403, "En la demo pública las reglas base no se pueden modificar. Crea una regla nueva.")


@app.post("/api/rules", tags=["reglas"], status_code=201)
def create_rule(body: RuleRequest) -> dict[str, Any]:
    if settings.public_demo and sum(r.author == "usuario" for r in engine.rules.rules.values()) >= settings.max_user_rules:
        raise HTTPException(429, "La demo pública ya tiene muchas reglas de visitantes. Elimina alguna antes de crear otra.")
    rule = engine.rules.add(_build_rule(body, engine.rules.next_id()))
    return rule.to_dict()


@app.post("/api/rules/preview", tags=["reglas"])
def preview_rule(body: RuleRequest) -> dict[str, Any]:
    try:
        return engine.preview_rule(_build_rule(body, "preview"))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


class RulePatch(BaseModel):
    enabled: bool | None = None
    severity: str | None = None
    params: dict[str, Any] | None = None
    note: str | None = None


@app.patch("/api/rules/{rule_id}", tags=["reglas"])
def update_rule(rule_id: str, body: RulePatch) -> dict[str, Any]:
    if rule_id not in engine.rules.rules:
        raise HTTPException(404, "Regla no encontrada")
    _protect_system_rule(rule_id)
    try:
        return engine.rules.update(rule_id, body.model_dump(exclude_none=True)).to_dict()
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.delete("/api/rules/{rule_id}", tags=["reglas"])
def delete_rule(rule_id: str) -> dict[str, bool]:
    if rule_id not in engine.rules.rules:
        raise HTTPException(404, "Regla no encontrada")
    _protect_system_rule(rule_id)
    engine.rules.delete(rule_id)
    return {"deleted": True}


# ------------------------------------------------------------- simulación
@app.post("/api/scenarios/{scenario_id}", tags=["simulación"])
async def inject(scenario_id: str) -> dict[str, str]:
    if scenario_id not in SCENARIOS_BY_ID:
        raise HTTPException(404, "Escenario no encontrado")
    result = engine.inject(scenario_id)
    await publish()
    return result


class ControlRequest(BaseModel):
    running: bool | None = None
    speed: float | None = Field(None, ge=0.25, le=10)
    random_anomalies: bool | None = None
    finish_day: bool = False
    next_day: bool = False
    reset: bool = False


@app.post("/api/control", tags=["simulación"])
async def control(body: ControlRequest) -> dict[str, Any]:
    if settings.public_demo:
        if body.reset:
            raise HTTPException(403, "La demo pública no se puede reiniciar.")
        if body.speed is not None and body.speed > 4:
            body.speed = 4
    if body.reset:
        await asyncio.to_thread(engine.reset)
    if body.finish_day or body.next_day:
        await asyncio.to_thread(engine.run_day)
    if body.next_day:
        await asyncio.to_thread(engine.tick)  # closes today and starts tomorrow at 05:30
    if body.running is not None:
        engine.running = body.running
    if body.speed is not None:
        engine.speed = body.speed
    if body.random_anomalies is not None:
        engine.random_anomalies = body.random_anomalies
    snap = await publish()
    return {"running": snap["running"], "speed": snap["speed"], "random_anomalies": snap["random_anomalies"]}


# ------------------------------------------------------------------ métricas
@app.get("/metrics", response_class=PlainTextResponse, tags=["operación"])
def metrics() -> str:
    snap = engine.snapshot()
    k = snap["kpis"]
    lines = [
        "# HELP atlas_quality_score Puntaje de calidad promedio de las tablas evaluadas hoy (0-100).",
        "# TYPE atlas_quality_score gauge",
        f"atlas_quality_score {k['score'] if k['score'] is not None else 'NaN'}",
        "# TYPE atlas_open_incidents gauge",
        f"atlas_open_incidents {k['open_incidents']}",
        "# TYPE atlas_tables_missing gauge",
        f"atlas_tables_missing {k['tables_missing']}",
        "# TYPE atlas_checks_failed gauge",
        f"atlas_checks_failed {k['checks_failed']}",
        "# TYPE atlas_table_score gauge",
    ]
    for t in snap["tables"]:
        if t["score"] is not None:
            lines.append(f'atlas_table_score{{tabla="{t["name"]}"}} {t["score"]}')
    lines.append("# TYPE atlas_table_rows gauge")
    for t in snap["tables"]:
        if t["rows"] is not None:
            lines.append(f'atlas_table_rows{{tabla="{t["name"]}"}} {t["rows"]}')
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- tiempo real
@app.websocket("/ws")
async def websocket(ws: WebSocket) -> None:
    await ws.accept()
    hub.clients.add(ws)
    try:
        await ws.send_text(json.dumps({"type": "snapshot", "data": engine.snapshot()}, default=str,
                                      ensure_ascii=False))
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        hub.clients.discard(ws)


app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
