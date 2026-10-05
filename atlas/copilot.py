"""Copiloto de incidentes: redacta el diagnóstico y el correo de escalamiento.

Regla de diseño: los controles determinísticos deciden si un dato está bien o mal; el
copiloto solo explica y redacta. Por defecto usa plantillas (sin internet, instantáneo,
probado). Si existe ANTHROPIC_API_KEY usa Claude y, ante cualquier error, vuelve a plantillas.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from .engine import Engine

log = logging.getLogger(__name__)

CAUSES = {
    "disponibilidad": ("El proceso de carga no se ejecutó o terminó con error.",
                       "Revisar el log del proceso de carga, re-ejecutarlo y confirmar la hora de publicación."),
    "volumen_bajo": ("Carga incompleta: el archivo llegó cortado o la ingesta fue borrada/truncada.",
                     "Validar el archivo fuente contra el conteo esperado y recargar la tabla completa."),
    "volumen_alto": ("El archivo se procesó más de una vez o trae información de varios días.",
                     "Identificar la ejecución duplicada, depurar la carga y volver a validar."),
    "estructura": ("Cambio en el sistema fuente: el archivo trae columnas diferentes a las acordadas.",
                   "Confirmar el cambio con el dueño del sistema fuente y ajustar el mapeo antes de recargar."),
    "unico": ("El proceso se re-ejecutó sin controlar duplicados (la carga no es idempotente).",
              "Eliminar los registros repetidos y asegurar que una re-ejecución reemplace en vez de agregar."),
    "fecha_del_dia": ("Se cargó un archivo de una fecha anterior (archivo de ayer reprocesado).",
                      "Ubicar el archivo del día correcto y recargar la tabla."),
    "no_nulos": ("Campo no diligenciado en el origen o error de mapeo en la carga.",
                 "Revisar el mapeo de la columna y completar los registros vacíos en el origen."),
    "rango": ("Valores imposibles para el negocio: error de cálculo o de unidades en el origen.",
              "Revisar la fórmula/unidades en el sistema fuente y corregir los registros señalados."),
    "comparacion": ("Inconsistencia entre columnas: error de cálculo en el origen.",
                    "Revisar el cálculo en el sistema fuente para los registros de ejemplo."),
    "valores_permitidos": ("Aparece un valor nuevo que no está homologado en el catálogo.",
                           "Confirmar si es un valor válido nuevo (y actualizar el catálogo) o un error de origen."),
    "outlier": ("Valor atípico frente al histórico: posible error de unidades o un evento de negocio real.",
                "Confirmar con el negocio si hubo un evento (campaña, cierre); si no, revisar unidades y recargar."),
    "sql": ("Los datos incumplen una regla de negocio definida por el área.",
            "Revisar los registros de ejemplo con el dueño de la regla."),
}

SYSTEM_PROMPT = """Eres el copiloto de calidad de datos de ATLAS en un banco colombiano.
Recibes la evidencia que los controles automáticos ya recolectaron. No contradigas los resultados
de los controles ni inventes datos, cifras, tablas o personas. Escribe en español, claro y breve,
para un equipo de negocio o de TI. Cita las reglas y valores de la evidencia."""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "asunto": {"type": "string"},
        "cuerpo": {"type": "string"},
        "causa_probable": {"type": "string"},
        "acciones": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["asunto", "cuerpo", "causa_probable", "acciones"],
    "additionalProperties": False,
}


def _fmt(value: Any) -> str:
    """Números legibles en formato colombiano: 549.730.001 · -7,73."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "NULL" if value is None else str(value)
    if float(value).is_integer():
        return f"{value:,.0f}".replace(",", ".")
    return f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _cause_key(check: dict[str, Any]) -> str:
    kind = check.get("rule_type") or check["check_id"]
    if kind == "volumen":
        return "volumen_bajo" if "menos" in check["message"] or "vacía" in check["message"] else "volumen_alto"
    return kind if kind in CAUSES else "sql"


def build_context(engine: Engine, incident_id: str) -> dict[str, Any]:
    with engine.lock:
        return engine.incident_dict(engine.incidents[incident_id], full=True)


def template_analysis(inc: dict[str, Any]) -> dict[str, Any]:
    main = inc["checks"][0] if inc["checks"] else None
    cause, action = CAUSES[_cause_key(main)] if main else ("Sin controles fallidos.", "Validar manualmente.")
    actions = [action] + [CAUSES[_cause_key(c)][1] for c in inc["checks"][1:] if CAUSES[_cause_key(c)][1] != action]
    findings = "\n".join(f"  • {c['name']}: {c['message']}" for c in inc["checks"])
    examples = ""
    if main and main.get("examples"):
        sample = main["examples"][:3]
        examples = "\nEjemplos de registros afectados:\n" + "\n".join(
            "  - " + ", ".join(f"{k}={_fmt(v)}" for k, v in row.items()) for row in sample) + "\n"
    recurrence = (f"\nEs la {inc['recurrence_30d'] + 1}.ª vez que esta tabla presenta fallas en los últimos 30 días."
                  if inc["recurrence_30d"] else "")
    body = (
        f"Hola equipo {inc['owner']},\n\n"
        f"El monitor de calidad ATLAS detectó un problema en la tabla {inc['table']} ({inc['table_title']}) "
        f"en la carga del {inc['opened_date']} a las {inc['opened_time']}. Severidad: {inc['severity_label']}.\n\n"
        f"Qué encontramos:\n{findings}\n{examples}\n"
        f"Posible causa: {cause}\n\n"
        f"Impacto: alimenta {', '.join(inc['consumers'])}.{recurrence}\n\n"
        f"Acción solicitada: {action}\n\n"
        "Quedamos atentos a su confirmación. El incidente se cerrará automáticamente cuando la siguiente "
        "carga cumpla todos los controles.\n\n"
        "Equipo de Calidad de Datos · ATLAS"
    )
    return {
        "mode": "plantilla",
        "to": inc["owner_email"],
        "asunto": f"[ATLAS][{inc['severity_label']}] {inc['table']}: {inc['title']} ({inc['opened_date']})",
        "cuerpo": body,
        "causa_probable": cause,
        "acciones": actions,
    }


def claude_analysis(inc: dict[str, Any], model: str) -> dict[str, Any]:
    import anthropic

    client = anthropic.Anthropic()
    response = client.beta.messages.create(
        model=model,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
        betas=["server-side-fallback-2026-07-01"],
        extra_body={"fallbacks": "default"},
        messages=[{"role": "user", "content": "Redacta el correo de escalamiento para este incidente. Evidencia (JSON):\n"
                   + json.dumps(inc, ensure_ascii=False, default=str)}],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("el modelo rechazó la solicitud")
    text = next(b.text for b in response.content if b.type == "text")
    return {"mode": "claude", "model": response.model, "to": inc["owner_email"], **json.loads(text)}


def analyze(engine: Engine, incident_id: str, use_llm: bool | None = None) -> dict[str, Any]:
    inc = build_context(engine, incident_id)
    if use_llm is None:
        use_llm = bool(os.getenv("ANTHROPIC_API_KEY"))
    if use_llm:
        try:
            return claude_analysis(inc, engine.settings.copilot_model)
        except Exception as exc:  # el copiloto nunca debe frenar la gestión del incidente
            log.warning("Copiloto con Claude no disponible, uso plantilla: %s", exc)
            result = template_analysis(inc)
            result["fallback_reason"] = str(exc)[:200]
            return result
    return template_analysis(inc)
