# ATLAS · Monitor de calidad de datos

**Valida cada mañana las tablas que carga TI, detecta cargas anómalas y gestiona los incidentes con el equipo responsable.**

[![ci](https://github.com/<tu-usuario>/atlas-one/actions/workflows/ci.yml/badge.svg)](https://github.com/<tu-usuario>/atlas-one/actions/workflows/ci.yml)
![python](https://img.shields.io/badge/python-3.11%2B-blue)
![tests](https://img.shields.io/badge/tests-63%20pasando-brightgreen)
![coverage](https://img.shields.io/badge/cobertura-95%25-brightgreen)

> 🇬🇧 *ATLAS is a data-quality monitor for the tables an IT team loads every morning: availability, volume anomalies (weekday-aware), duplicates, nulls, business rules compiled to SQL, standard-deviation outliers and incident management with an AI-drafted escalation email. Spanish UI, banking demo data.*

---

## El problema

Las áreas de negocio dependen de tablas que otro equipo carga cada día. Cuando una carga llega **duplicada, vacía, tarde, con el archivo de ayer o con valores imposibles** (una tasa de mora negativa), normalmente alguien se entera cuando el reporte ya salió mal.

ATLAS no mueve datos: **los vigila**. Valida cada tabla apenas llega, compara contra su propio histórico y, si algo falla, abre un incidente con la evidencia y el correo listo para el equipo dueño.

## Qué revisa

| Pregunta | Control | Ejemplo |
|---|---|---|
| ¿Llegó la tabla de hoy? | **Disponibilidad** (automático) | "pagos no ha llegado; se esperaba a las 07:00" |
| ¿Llegó completa? | **Volumen** vs. el mismo día de la semana (automático) | "Llegaron 0 registros; lo normal un lunes es ~740" |
| ¿Trae las columnas acordadas? | **Estructura** (automático) | "Falta la columna canal" |
| ¿Hay repetidos? | Regla *sin duplicados* | `id_credito` repetido 1.800 veces |
| ¿Hay vacíos? | Regla *sin vacíos* (con tolerancia) | 30% de clientes sin `numero_documento` |
| ¿Los valores tienen sentido? | Reglas de *rango*, *valores permitidos*, *comparación* | `tasa_mora` entre 0 y 100 · `saldo_capital <= monto_desembolsado` |
| ¿Es el archivo de hoy? | Regla *datos del día* | `fecha_corte` trae la fecha de ayer |
| ¿Es un valor normal? | Regla *outlier* con desviación estándar | "Suma de desembolsos 6,4σ por encima de lo normal" |
| ¿Algo muy específico del negocio? | Regla *SQL personalizada* | `indicador = 'TRM' AND (valor < 2500 OR valor > 7000)` |

Las reglas se crean **desde la interfaz, sin programar**. Cada una se traduce a una consulta SQL que se puede ver y **probar con la última carga antes de guardarla**. Las reglas SQL personalizadas se ejecutan en modo solo lectura.

## Cómo funciona

```
 05:30 ─────────── 06:00 ── 06:30 ── 07:00 ── 07:30 ── 08:00 ── 08:30 ─────────── 10:30
                 clientes  cartera   pagos    tasas  desembolsos indicadores
                    │         │        │        │        │         │
                    ▼         ▼        ▼        ▼        ▼         ▼
            ┌──────────────────────────────────────────────────────────────┐
            │  ATLAS valida cada tabla apenas llega                        │
            │  monitores automáticos + reglas de negocio (SQL) + outliers  │
            └──────────────────────────────────────────────────────────────┘
                    │ falla                                  │ todo OK
                    ▼                                        ▼
        Incidente por tabla (severidad, evidencia,     Puntaje de calidad
        ejemplos, impacto, recurrencia, responsable)   e historial diario
                    │
                    ▼
        Correo de escalamiento redactado (plantilla o Claude)
        Se cierra solo cuando la siguiente carga cumple todo
```

- **Simulación realista.** Un banco sintético genera 6 tablas cada mañana (clientes, cartera, pagos, tasas, desembolsos, indicadores de cartera), con estacionalidad semanal, cartera estable y mora entre 3% y 6%. Arranca con **70 días de historia**.
- **Línea base robusta.** El "volumen normal" se calcula con la mediana y la desviación de los **mismos días de la semana** (un domingo no se compara con un lunes). Los días anómalos **nunca** entran a la línea base.
- **Un incidente por tabla, no una alerta por regla.** Una carga duplicada dispara la regla de duplicados, el volumen y el outlier, pero genera **un solo caso** con toda la evidencia.
- **Falsos positivos medidos.** En 120 días simulados (720 cargas) la tasa de alertas sin motivo es menor a 1,5%. Lo verifica un test.

## La interfaz

| Vista | Qué muestra |
|---|---|
| **Resumen** | Línea de tiempo de llegadas del día, KPIs (puntaje, tablas recibidas, incidentes, controles fallidos), tarjeta por tabla con semáforo y tendencia de 30 días, actividad en vivo |
| **Tablas** | Ficha de cada tabla: controles de hoy con registros de ejemplo y SQL, **volumen diario con banda de normalidad**, outliers, historial de 30 días (mapa de calor) y perfil de columnas |
| **Incidentes** | Qué falló, ejemplos, impacto, recurrencia, línea de tiempo, **correo de escalamiento** y resolución |
| **Reglas** | Lista de reglas activas, crear / probar / activar / eliminar |

Además: **⚡ Simular anomalía** (13 problemas típicos: tabla que no llega, carga vacía, parcial, duplicada, archivo de ayer, tasa de mora negativa, pico de desembolsos, nulos, columna eliminada…), **▶ Demo** guiada de 2 minutos, modo claro/oscuro y modo móvil.

## Cómo correrlo

**Con un clic** (solo necesita Python 3.11+; el lanzador ofrece instalarlo si no está):

| Sistema | Qué hacer |
|---|---|
| Windows | doble clic en **`start.bat`** |
| macOS | doble clic en **`start.command`** (la primera vez: clic derecho → Abrir) |
| Linux | `./start.sh` |

Crea el entorno, instala dependencias (solo la primera vez), busca un puerto libre y abre el navegador. `--test` corre los tests y `--reinstall` reconstruye el entorno.

**Con Docker:**

```bash
docker compose up --build        # http://localhost:8000
```

Opcional: define `ANTHROPIC_API_KEY` para que Claude redacte los correos. Sin ella se usan plantillas; el monitor nunca depende de la IA.

## API

Documentación interactiva en `/docs`. Lo principal:

| Método | Ruta | |
|---|---|---|
| `WS` | `/ws` | estado en tiempo real |
| `GET` | `/api/state` | resumen del día |
| `GET` | `/api/tables/{tabla}` | controles, historial, outliers, perfil |
| `GET/POST/PATCH/DELETE` | `/api/rules` | gestión de reglas |
| `POST` | `/api/rules/preview` | probar una regla con la última carga |
| `POST` | `/api/incidents/{id}/copilot` | redactar correo de escalamiento |
| `POST` | `/api/incidents/{id}/escalate` · `/resolve` | gestión del incidente |
| `POST` | `/api/scenarios/{id}` | simular una anomalía |
| `GET` | `/metrics` | métricas Prometheus |

## Estructura

```
atlas/
  tables.py     6 tablas bancarias, responsables, horarios y el banco sintético que las carga
  store.py      SQLite: datos cargados + metadatos (cargas, métricas diarias, resultados)
  rules.py      tipos de regla, traducción a SQL, outliers, línea base y reglas por defecto
  monitors.py   disponibilidad, volumen y estructura (automáticos)
  engine.py     reloj de la mañana, validación al llegar, puntajes e incidentes
  copilot.py    correo de escalamiento (plantilla o Claude)
  api.py        FastAPI: REST + WebSocket + métricas
web/            interfaz sin build (HTML, CSS, JavaScript, gráficos SVG)
tests/          63 tests: reglas, monitores, cada anomalía, falsos positivos, API
```

## Decisiones de diseño

- **Las reglas son SQL.** Así el analista ve exactamente qué se evalúa, y la misma regla se puede llevar a un warehouse, a dbt o a Great Expectations.
- **Los controles deciden, la IA redacta.** Si una tabla pasa o falla lo decide una regla determinística y probada. El copiloto solo convierte la evidencia en un correo claro.
- **Comparar contra el mismo día de la semana.** Evita falsas alarmas por estacionalidad (fines de semana, lunes).
- **SQLite para la demo.** Corre en cualquier computador en segundos. En producción el mismo diseño se conecta a SQL Server, Oracle, Snowflake o Databricks.

## Alcance

Es un **prototipo con calidad de producción** sobre datos sintéticos, no un producto terminado. Siguientes pasos naturales: conectores a bases reales, notificaciones por Teams/Slack/correo, autenticación por roles y detección de deriva por columna.

## Licencia

MIT
