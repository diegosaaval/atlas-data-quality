# Monitorear tus propias tablas (conectores)

ATLAS trae una demo con un banco simulado, pero está hecho para vigilar **cualquier tabla que le indiques**. Un conector es un archivo YAML en `conectores/` que dice dónde están los datos y qué tablas mirar. ATLAS lee la estructura de cada tabla por su cuenta (columnas y tipos) y nunca modifica los datos.

## 1. Crea el conector

`conectores/ventas.yaml`:

```yaml
fuente: ventas
titulo: Ventas diarias
descripcion: Archivos que publica el equipo comercial cada mañana.
ruta: /ruta/a/mis/datos          # carpeta con los archivos (o una lista: se usa la primera que exista; o ATLAS_FUENTE_RUTA)
formato: parquet                 # parquet o csv
manifiesto: ""                   # opcional (ver abajo)
hora_esperada: "07:00"           # antes de esta hora (+60 min) debe haber datos nuevos
responsable: Equipo Comercial
correo: comercial@empresa.example
url_corrida: ""                  # opcional: enlace a cada corrida del pipeline, con {run_id}

tablas:
  - nombre: ventas               # lee ventas.parquet (o usa "archivo: otro_nombre.parquet")
    titulo: Ventas
    tipo: incremental            # incremental: se valida la fecha de cada carga · snapshot: foto completa
    columna_fecha: fecha_venta
    consumidores: [Tablero comercial]
  - nombre: clientes
    tipo: snapshot

reglas:                          # opcional: si una tabla no tiene reglas, ATLAS sugiere unas iniciales
  - {table: ventas, type: unico, severity: critica, params: {columns: [id_venta]}}
  - {table: ventas, type: rango, severity: alta, params: {column: monto, min: 0}}
```

Los tipos de regla son los mismos de la interfaz: `no_nulos`, `unico`, `rango`, `valores_permitidos`, `comparacion`, `fecha_del_dia`, `outlier` y `sql`. Después de conectar, también se pueden crear, probar y apagar desde la pestaña **Reglas**.

## 2. Conéctalo

- **Desde la interfaz:** botón **Fuente de datos** → *Usar esta fuente*. Para volver, elige **Demo**.
- **Al arrancar:** `start.sh --fuente ventas` (o `Iniciar ATLAS.command --fuente ventas`), o la variable `ATLAS_FUENTE=ventas`.

## 3. Qué hace ATLAS con tus tablas

| Paso | Detalle |
|---|---|
| Lee la estructura | Nombre y tipo de cada columna, directamente del Parquet o CSV |
| Valida la historia | Con manifiesto, cada fecha publicada; sin manifiesto, el estado actual de los archivos |
| Sugiere reglas | Para tablas sin reglas: identificadores sin vacíos y únicos, números que nunca fueron negativos, catálogos cortos como valores permitidos (quedan marcadas como *sugerida por ATLAS*) |
| Vigila en vivo | Revisa cada pocos segundos si hay datos nuevos; si no los hay a la hora acordada, abre un incidente de disponibilidad |
| Re-procesos | Si una fecha se vuelve a publicar, la reemplaza (no la duplica) |

## Cuándo ATLAS sabe que "hay una carga nueva"

- **Con manifiesto** (recomendado): un `_manifest.json` junto a los archivos, que se reescribe en cada publicación:

  ```json
  {
    "run_id": "20261005T125217-fc0b6b",
    "published_at": "2026-10-05T12:54:41+00:00",
    "dates": ["2026-09-29", "2026-09-30"],
    "datasets": {"ventas": 4102, "clientes": 1743}
  }
  ```

  `published_at` (con zona horaria) y `dates` son obligatorios; `run_id` y `datasets` son opcionales y se muestran en la interfaz.

- **Sin manifiesto:** ATLAS detecta que la fecha de modificación de los archivos cambió y valida el estado actual con la fecha de hoy.

## Próximos conectores

El diseño admite cualquier fuente que DuckDB pueda leer. Los siguientes pasos naturales son S3 (Parquet con credenciales de **solo lectura**), Athena y bases como PostgreSQL o SQL Server con un usuario de solo lectura.
