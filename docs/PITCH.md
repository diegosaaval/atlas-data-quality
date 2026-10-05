# ATLAS ONE: guía para CV y entrevistas

## La frase de una línea

> *"Construí ATLAS ONE, una implementación de referencia de una plataforma de confiabilidad de datos financieros: detecta fallos con controles determinísticos, bloquea datos malos antes de que lleguen a gold con un quality gate basado en linaje, abre incidentes con causa raíz y blast radius, y se recupera sola. Tiene un Failure Lab para romperla en vivo."*

No la vendas como "reemplazo del core de un banco". Véndela como **reference implementation** o **production-style prototype**. Eso transmite criterio.

## Demo de 2 minutos (o usa el botón ▶ Demo)

| Tiempo | Qué muestras | Qué dices |
|---|---|---|
| 0:00 | Overview | "Simula una plataforma bancaria: core, pagos, réplica y FX fluyen por bronze, silver y gold cada 5 minutos simulados. El score pondera por criticidad de negocio." |
| 0:20 | Lineage | "17 activos desde los sistemas fuente hasta el reporte regulatorio. El linaje no es decorativo: con él se decide qué bloquear y a quién avisar." |
| 0:35 | Failure Lab → *Schema drift* | "El gateway despliega un cambio de payload sin avisar. Pasa en la vida real." |
| 0:45 | Timeline del Lab | "Se detecta en la misma corrida: el contrato falla, se cuarentenan los registros y el gate retiene los 4 productos gold. Los consumidores siguen con la última versión buena." |
| 1:05 | Lineage (aristas rojas) | "Esto es el blast radius: 9 activos, 5 tier-1, incluido el reporte regulatorio. Por eso queda SEV1." |
| 1:20 | Incidents → Copilot | "Un solo incidente, no ocho alertas: los síntomas de abajo se agrupan bajo la causa raíz. El copiloto redacta el resumen y el update para stakeholders. Los checks deciden, la IA explica." |
| 1:40 | Apply remediation | "Se aplica el runbook. Se cierra solo después de 2 corridas en verde, y queda medido el MTTD y el MTTR." |
| 1:55 | Cierre | "Todo corre con `docker compose up`, tiene 63 tests y CI. El roadmap lo lleva a S3, Databricks, Airflow y Terraform." |

## Bullets para el CV

Úsalos en la sección **Proyectos**. Ajusta el link.

**Español**
- **ATLAS ONE: plataforma de confiabilidad de datos financieros** (Python, FastAPI, SQL, Docker, GitHub Actions). [github.com/…/atlas-one]
  - Diseñé una arquitectura medallion (bronze/silver/gold) con data contracts versionados, cuarentena, merge idempotente, integridad referencial y reconciliación contra réplica.
  - Implementé 10 dimensiones de calidad y un *quality gate* basado en linaje que bloquea datos inválidos antes de llegar a productos de finanzas, riesgo y fraude.
  - Construí un motor de incidentes que agrupa síntomas por causa raíz, calcula blast radius y severidad según impacto regulatorio, y mide MTTD/MTTR.
  - Desarrollé un *Failure Lab* con 10 modos de falla reales (schema drift, duplicate load, IAM, replicación…) que también funciona como suite de regresión: 63 tests y 95% de cobertura en CI.
  - Agregué un copiloto de incidentes con IA (Claude) que resume la evidencia sin reemplazar los controles determinísticos, más observabilidad con Prometheus y eventos OpenLineage.

**English**
- **ATLAS ONE: data reliability platform for financial data** (Python, FastAPI, SQL, Docker, GitHub Actions)
  - Designed a medallion architecture with versioned data contracts, quarantine, idempotent merges, referential integrity and replica reconciliation.
  - Built 10 data-quality dimensions and a lineage-aware quality gate that keeps invalid data out of finance, risk and fraud data products.
  - Implemented root-cause incident grouping, lineage-based blast radius, impact-based severity and MTTD/MTTR tracking.
  - Shipped a Failure Lab with 10 production failure modes that doubles as a regression suite (63 tests, 95% coverage, CI).
  - Added an AI incident copilot (Claude) that explains evidence without overriding deterministic checks; Prometheus metrics and OpenLineage events.

> Pon junto a este proyecto tu experiencia real (procesos financieros críticos, controles, automatizaciones). Lo que hace fuerte el CV es la combinación de **experiencia bancaria real** y este proyecto. No infles cifras: las que vienen del proyecto son verificables en el repo.

## Cómo adaptarlo a cada vacante

| Empresa | Enfatiza | Muestra en la demo |
|---|---|---|
| SURA / Protección | calidad, gobierno, documentación, seguridad | Contracts (clasificación PII), RBAC por rol, ADRs, runbooks |
| Nequi | AWS, PySpark, orquestación, fintech | Roadmap fase 1–3, DAG del overview, reconciliación de réplica |
| Addi | SLO/SLI, CDC, schema evolution, observabilidad, ADRs | Freshness SLO, `/metrics`, schema drift, carpeta `docs/adr` |
| Rappi | pipelines financieros, Airflow, data products | Gold models (SQL), ledger diario, payment ops |
| Mercado Libre / Mercado Pago | pagos, riesgo, features de fraude, anomalías | Fault *Fraud pattern*, Risk signals, distribución no bloqueante |
| EPM | confiabilidad, trazabilidad, operación crítica | Linaje, blast radius, MTTD/MTTR, recuperación con backfill |
| KPMG / consultoras | criterio, comunicación, certificaciones | Copiloto (stakeholder update) + certificación cloud del roadmap |

## Preguntas difíciles (y buenas respuestas)

**¿Por qué SQLite y no Spark?**
"Para que cualquiera lo corra en 10 segundos y los tests sean determinísticos. Los modelos gold son SQL puro y los checks son funciones puras, así que portarlo a Databricks es mover el SQL a dbt y el merge a Delta `MERGE INTO`. Está en el ADR 0002 y en el roadmap."

**¿Por qué no usaste Great Expectations o Soda?**
"Quería mostrar que entiendo el problema de fondo: agrupación por causa raíz, gate por linaje, severidad por impacto. Esas herramientas resuelven la parte de expectativas; en producción las integraría y ATLAS consumiría sus resultados como `CheckResult`."

**¿Qué pasa si el volumen sube 8x por un evento real?**
"El gate bloquea, porque publicar dinero duplicado en un ledger es peor que llegar 10 minutos tarde. El runbook incluye 'acknowledge and re-baseline'. Está documentado como decisión en el ADR 0001."

**¿Cómo evitas que la IA invente cosas?**
"La IA no decide nada. Recibe solo la evidencia recolectada, responde con un JSON schema y tiene prohibido contradecir los checks. Si falla, el sistema usa reglas. El incidente nunca depende del modelo."

**¿Cómo evitas el alert fatigue?**
"Un check es causa raíz solo si ningún ancestro en el linaje está fallando. Todo lo demás son síntomas del incidente de arriba. Un fallo de IAM produce 1 incidente, no 8."

**¿Cómo lo escalarías?**
"Particionado por batch en S3/Delta, checks como jobs independientes por dataset, metadata de checks en una tabla, y el gate como un operador del DAG en Airflow. El motor de incidentes es stateless sobre esa tabla."

## Siguiente paso recomendado

1. Sube el repo a GitHub (público), reemplaza `<your-user>` en el README y el `ATLAS_GITHUB_URL`.
2. Graba un GIF o video de 60 s con el botón **▶ Demo** y ponlo arriba del README.
3. Despliégalo gratis (Render / Fly.io / Railway con el Dockerfile) y pon el link en tu CV y LinkedIn.
4. Empieza la fase 1 del roadmap (Terraform + S3 + IAM): es lo que más piden Nequi y SURA.
