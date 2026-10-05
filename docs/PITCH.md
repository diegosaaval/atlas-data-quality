# ATLAS · guía para CV y entrevistas

## La idea en una frase

> *"En el banco validaba las cargas que hacía TI: duplicados, nulos, desviaciones y reglas de negocio como que la tasa de mora no fuera negativa, para escalar incidentes a tiempo. ATLAS es la versión mejorada de ese trabajo: valida cada tabla apenas llega, detecta cargas anómalas contra su histórico y abre el incidente con la evidencia y el correo para el equipo responsable."*

Este proyecto **cuenta tu experiencia real**. No es un ejercicio de curso: es lo que ya hacías, construido como lo haría un equipo de datos.

## Por qué esto es Data Engineering

Lo que hace ATLAS se llama **Data Quality / Data Observability**, y aparece explícitamente en las vacantes:

- **SURA:** "calidad, seguridad y gobernabilidad de las plataformas de datos"
- **Addi:** "Data Quality automático, observabilidad, SLO/SLI, runbooks"
- **Protección:** "gobierno, calidad, monitoreo"

Hay empresas enteras dedicadas solo a esto (Monte Carlo, Soda, Great Expectations). Tu diferencial es que vienes del negocio: sabes **qué** validar y **por qué importa**.

## Glosario rápido (por si te preguntan)

| Término | Qué es |
|---|---|
| **Data quality** | Que los datos sean completos, únicos, válidos, oportunos y consistentes |
| **Data observability** | Vigilar automáticamente la salud de los datos (volumen, frescura, esquema, distribución) |
| **Freshness / disponibilidad** | Que la tabla esté actualizada a tiempo |
| **Outlier / z-score** | Valor que se aleja más de N desviaciones estándar de su comportamiento normal |
| **Línea base** | Lo "normal" para comparar; ATLAS usa la mediana del mismo día de la semana |
| **Idempotencia** | Que re-ejecutar una carga no duplique datos (la causa típica de duplicados) |
| **Medallion: bronze/silver/gold** | Capas de un data lake: datos crudos → limpios → listos para el negocio. ATLAS vigilaría sobre todo silver y gold |
| **SLA** | Compromiso de hora de entrega de una tabla (ej. "pagos antes de las 7:00") |

## Demo de 2 minutos (o usa el botón Ver demo)

| Tiempo | Qué muestras | Qué dices |
|---|---|---|
| 0:00 | Resumen | "Cada mañana TI carga 6 tablas del banco. ATLAS valida cada una apenas llega." |
| 0:15 | Línea de tiempo | "Cada tabla tiene una hora acordada. Si no llega, ATLAS lo detecta solo." |
| 0:25 | Simular anomalía → carga duplicada + pagos no llega | "Simulo dos problemas que me pasaban en el banco." |
| 0:40 | La cartera se pone roja | "El doble de registros y créditos repetidos: incidente crítico, notificado a Riesgo de Crédito." |
| 0:55 | Pagos 'No disponible' | "Son las 8:00 y pagos no llegó: otro incidente, con Recaudo como responsable." |
| 1:10 | Incidentes → correo | "Un incidente por tabla, con evidencia y ejemplos. El correo de escalamiento se redacta con un clic." |
| 1:30 | Tablas → gráfico de volumen | "La banda azul es lo normal para cada día de la semana. Los puntos rojos son las cargas anómalas." |
| 1:45 | Reglas | "El negocio crea reglas sin programar, por ejemplo tasa de mora entre 0 y 100. Cada regla es SQL." |
| 1:55 | Cierre | "Cuando la siguiente carga llega bien, el incidente se cierra solo y queda medido el tiempo de resolución." |

## Bullets para el CV

**Español**
- **ATLAS: monitor de calidad de datos para tablas bancarias** (Python, SQL, FastAPI, Docker, GitHub Actions)
  - Diseñé un monitor que valida 6 tablas diarias apenas llegan: disponibilidad frente al horario acordado, volumen frente al mismo día de la semana y estructura.
  - Implementé 8 tipos de reglas de calidad que se traducen a SQL (duplicados, nulos, rangos, valores permitidos, comparaciones, fecha del día, outliers por desviación estándar y SQL personalizado), creadas desde una interfaz web sin programar.
  - Construí la gestión de incidentes: un caso por tabla con evidencia, registros de ejemplo, impacto, recurrencia y cierre automático; el correo de escalamiento lo redacta la IA (Claude) o una plantilla.
  - Calibré la detección con líneas base robustas (mediana y desviación por día de la semana): menos de 1,5% de falsos positivos en 720 cargas simuladas, y detección del 100% de 13 tipos de anomalía, verificado con 69 tests automáticos.

**English**
- **ATLAS: data-quality monitor for daily banking tables** (Python, SQL, FastAPI, Docker, GitHub Actions)
  - Built a monitor that validates 6 daily tables on arrival: SLA-based availability, weekday-aware volume anomalies and schema checks.
  - Implemented 8 rule types compiled to SQL (uniqueness, nulls, ranges, accepted values, cross-column, freshness, standard-deviation outliers, custom SQL), managed from a web UI without code.
  - Designed incident management: one case per table with evidence, sample records, impact, recurrence and auto-resolution; escalation emails drafted by AI (Claude) or templates.
  - Tuned detection with robust weekday baselines: <1.5% false positives over 720 simulated loads and 13/13 anomaly types detected, enforced by 69 automated tests.

> Pon este proyecto **justo debajo de tu experiencia en el banco**. Lo fuerte del CV es la combinación: *"lo hice en producción con datos reales"* + *"aquí está la versión mejorada, pública y probada"*. No infles cifras del banco; las del proyecto son verificables en el repo.

## Preguntas difíciles (y buenas respuestas)

**¿Cómo evitas falsas alarmas?**
"Comparo cada carga contra el mismo día de la semana, con mediana y desviación, y los días anómalos nunca entran a la línea base. Lo medí: menos de 1,5% de falsos positivos en 720 cargas. Además agrupo todo en un incidente por tabla para no inundar al equipo."

**¿Por qué desviación estándar y no un umbral fijo?**
"Un umbral fijo no entiende que el domingo hay la mitad de pagos. La desviación se adapta al comportamiento de cada tabla y de cada día. Para métricas con mucha variación (sumas de dinero) uso 4σ en vez de 3σ."

**¿Qué pasa si alguien crea una regla SQL maliciosa?**
"La condición se valida (no se permiten ; ni comandos de escritura) y se ejecuta con la base en modo solo lectura (`PRAGMA query_only`). Hay un test que lo verifica."

**¿Cómo lo llevarías a producción?**
"Cambiar SQLite por un conector a la base real (SQL Server, Oracle o Snowflake) que lea solo la partición del día, programar la validación con Airflow o cuando llegue la carga, y mandar las notificaciones a Teams o correo. El motor de reglas y los incidentes no cambian."

**¿Por qué no usaste Great Expectations o Soda?**
"Quería mostrar que entiendo la lógica de fondo: líneas base, agrupación de incidentes, severidad, escalamiento. Mis reglas son SQL, así que migrarlas a esas herramientas es directo."

## Siguiente paso recomendado

1. Sube el repo a GitHub (público), cambia `diegosaaval` en el README y en `ATLAS_GITHUB_URL`.
2. Graba un video de 60 segundos con el botón **Ver demo** y ponlo arriba del README.
3. Despliégalo gratis (Render, Fly.io o Railway con el Dockerfile) y pon el link en tu CV y en LinkedIn.
4. Próxima mejora con impacto: un conector a una base real (PostgreSQL o SQL Server) para validar tablas propias.
