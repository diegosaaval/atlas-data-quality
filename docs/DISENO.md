# ATLAS · decisiones de diseño

Este documento explica **por qué** ATLAS es como es: qué problema ataca, qué hacen las herramientas del mercado, qué tomé de ellas y qué dejé fuera a propósito.

## 1. El problema, contado desde la operación

En un banco, las áreas de negocio consumen tablas que otro equipo (TI o integración de datos) carga cada madrugada. Quien usa los datos no controla la carga, pero responde por lo que sale de ellos. Los problemas típicos que veía a diario (descritos de forma general; ATLAS no replica la arquitectura ni los procesos de ninguna entidad):

| Lo que pasa | Cómo se nota (tarde) |
|---|---|
| La tabla del día no llega | El reporte sale con los datos de ayer y nadie lo advierte |
| La carga se ejecuta dos veces | Saldos y provisiones duplicados |
| La carga queda vacía o a medias | Indicadores que "caen" de un día para otro |
| Se recarga el archivo de ayer | Todo parece normal, pero la fecha de corte es vieja |
| Un cálculo en origen falla | Valores imposibles: tasa de mora negativa, saldo mayor que el monto |
| Un pico real o un error de unidades | Montos 10 veces más altos que lo normal |

**Causa raíz:** la validación dependía de revisiones manuales y de umbrales fijos que no entienden la estacionalidad (un domingo siempre tiene menos pagos que un lunes).

## 2. Referentes del mercado

| Herramienta | Qué hace bien | Qué tomé | Qué no |
|---|---|---|---|
| **Great Expectations** | Biblioteca de "expectativas" declarativas sobre datos | Reglas declarativas por tabla y columna | Su complejidad de configuración |
| **Soda** | Reglas en un lenguaje simple (SodaCL) y chequeos de frescura | Reglas legibles para negocio, frescura como control de primera clase | Lenguaje propio: aquí cada regla es SQL estándar |
| **Monte Carlo / Bigeye** (observabilidad) | Monitores automáticos de volumen, frescura y esquema con líneas base aprendidas | Monitores automáticos sin configuración y líneas base por estacionalidad | El linaje completo y el costo empresarial |
| **dbt tests** | Pruebas `unique`, `not_null`, `accepted_values`, `relationships` junto al modelo | Los mismos tipos básicos de regla | Acoplar las pruebas al pipeline: ATLAS es independiente de quién carga |
| **Elementary** | Anomalías sobre resultados de dbt y alertas | Agrupar alertas para no generar ruido | Depender de dbt |

ATLAS es una **implementación de referencia**, no un reemplazo de estas herramientas. Las reglas son SQL, así que migrarlas a cualquiera de ellas es directo.

## 3. Decisiones y trade-offs

### Monitorear, no transformar
ATLAS no carga ni corrige datos: observa. Así se puede poner encima de cualquier carga existente sin pedir permiso para tocar el pipeline. *Trade-off:* no puede evitar que el dato malo llegue; puede avisar en minutos y con evidencia.

### Línea base por día de la semana, robusta y "limpia"
- Centro = **mediana** (un día atípico no la arrastra).
- Dispersión = máximo entre **desviación estándar**, **1,4826·MAD** y un piso relativo (5%). Para conteos, además un piso de **√n** (comportamiento tipo Poisson).
- Las tablas incrementales se comparan con **el mismo día de la semana** (últimas 6 semanas); las fotos diarias, con los últimos 14 días.
- **Los días anómalos no alimentan la línea base.** Si lo hicieran, una semana de cargas malas terminaría pareciendo normal.
- Umbral de ±3,5σ para volumen y ±4σ para sumas de dinero, que tienen colas pesadas.

*Resultado medido:* menos de 1,5% de falsos positivos en 720 cargas simuladas, con el 100% de los 13 tipos de anomalía detectados. Ambos números los verifica un test, así que una regresión rompe el CI.

### Un incidente por tabla
Una carga duplicada dispara a la vez duplicados, volumen y outlier. Abrir tres alertas confunde al equipo responsable, así que se abre **un caso por tabla** con todos los controles fallidos como evidencia. Si la falla persiste al día siguiente, el mismo incidente suma "cargas con falla" en vez de abrir otro. Se **cierra solo** cuando la siguiente carga cumple todo.

### Severidad explícita
Cada control tiene severidad (crítica, alta, media o baja) y el incidente toma la mayor. El puntaje de calidad pondera por severidad: un duplicado crítico pesa 4 veces más que un ingreso mensual negativo.

### Reglas = SQL visible
Cada regla se traduce a una consulta que el analista puede leer, copiar y correr en su propia base. Las reglas SQL personalizadas se validan (sin `;`, comentarios ni comandos de escritura) y se ejecutan con `PRAGMA query_only`: no pueden modificar datos.

### La IA explica, no decide
Si una tabla pasa o falla lo decide una regla determinística y probada. El copiloto solo convierte la evidencia en un correo claro. Funciona con plantillas sin internet; si hay API key usa Claude y, ante cualquier error, vuelve a la plantilla. El monitor nunca depende de un modelo.

### Simple de correr
SQLite, interfaz sin compilación y un lanzador de doble clic. Cualquier reclutador puede verlo funcionando en un minuto. *Trade-off:* no está pensado para millones de filas por tabla; ver la sección 5.

## 4. Demo pública
Con `ATLAS_PUBLIC_DEMO=1` (lo activa `render.yaml`) la demo se protege para visitantes que la comparten: las reglas base no se pueden editar ni borrar, cada visitante puede crear hasta 15 reglas propias, no se puede reiniciar, la velocidad máxima es 4× y, si alguien la deja en pausa, se reanuda sola a los 2 minutos.

## 5. Próximas versiones
1. **Conector a una base real** (PostgreSQL o SQL Server, usuario de solo lectura) que lea solo la partición del día.
2. **Conector cloud:** S3 + Athena con un rol IAM de solo lectura.
3. **Notificaciones reales** por Teams, Slack o correo.
4. **Monitorear MIDAS**, mi pipeline financiero en AWS: MIDAS convierte datos crudos en tablas gold, ATLAS verifica que sean confiables.
5. Autenticación por roles y bitácora de quién resolvió qué.
