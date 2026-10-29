# Memoria técnica — Observatorio de actividad sísmica

## 1. Objetivo y alcance

El sistema responde dónde se registran terremotos y cómo se compara una ventana reciente con la actividad catalogada durante un periodo histórico equivalente. La solución separa la recogida, transporte, preparación, persistencia, consulta y visualización; Angular no descarga ni transforma directamente el feed de USGS.

Alcance de referencia: feed móvil mundial de las últimas 24 horas y catálogo FDSN mundial de 2025 con `eventtype=earthquake` y `minmagnitude=4`, además del catálogo de países Natural Earth proporcionado. La comparación usa filtros geográficos y de magnitud equivalentes; se puede seleccionar magnitud mínima 4 desde el panel. El histórico es una instantánea actual del catálogo y no permite reconstruir todas sus revisiones pasadas.

## 2. Arquitectura y flujo

1. El productor consulta el feed cada 60 segundos, conserva la respuesta GeoJSON y la consulta, compara `(event_id, updated_ms)` con `published_state` y publica novedades/revisiones con clave Kafka `event_id`.
2. `earthquakes.live` y `earthquakes.replay` son topics separados. Kafka conserva mensajes durante siete días o hasta el límite configurado de 1 GiB.
3. El consumidor valida el mensaje, guarda eventos no sísmicos en `excluded_events`, inválidos en `quarantine` y eventos válidos en `event_versions` y `events_current`.
4. El consumidor integra cada punto con el índice STRtree del catálogo; el punto en una frontera queda cubierto y, ante varias coincidencias, se selecciona el identificador ADM0_A3 menor lexicográficamente. Un punto sin coincidencia permanece sin país. Los casos de demostración se marcan con `test_case=true`, son distinguibles en el mapa/tabla y se excluyen de estadísticas y consultas de calidad.
5. FastAPI consulta PostgreSQL. El panel solicita GeoJSON paginado y agregaciones completas; mapa, filtros y tabla comparten la misma selección. Los eventos reproducidos se mantienen aislados por modo.

La escritura de versión y estado actual es transaccional. El offset Kafka se confirma después de la escritura. La entrega es al menos una vez; las restricciones únicas y el upsert comparando `updated_ms` hacen idempotentes los reenvíos. No se implementa una transacción distribuida Kafka–PostgreSQL.

## 3. Modelo y diccionario

| Entidad/campo | Grano y significado | Tipo/unidad |
|---|---|---|
| `events_current (mode,event_id)` | Una fila por evento y espacio live/replay/historical; contiene solo la versión con `updated_ms` más alto | Estado actual |
| `event_versions (mode,event_id,updated_ms)` | Cada versión recibida una vez, junto con su evento GeoJSON original y bandera `test_case` para revisiones sintéticas | Historial |
| `published_state (mode,event_id)` | Mayor actualización confirmada por el productor a Kafka | Timestamp USGS en ms |
| `ingestion_runs` | Una fila por consulta, resultado, duración, filtros y error | UTC; latencia ms |
| `raw_snapshots` | Respuesta de fuente por hash, con consulta y expiración | GeoJSON JSONB |
| `countries (country_id)` | Catálogo de entidades Natural Earth | ADM0_A3, no necesariamente ISO |
| `event_id` | Identificador estable USGS | Texto |
| `event_time` | Ocurrencia del sismo; convertido desde `properties.time` | `timestamptz`, UTC |
| `updated_ms` | Revisión publicada por USGS; conserva el valor y unidad original | Entero, ms Unix |
| `ingested_at` / `replayed_at` | Recepción del mensaje / momento de reproducción | `timestamptz`, UTC |
| `magnitude`, `magnitude_type` | Magnitud y escala informada por la fuente | Decimal; texto; pueden ser null |
| `longitude`, `latitude`, `depth_km` | Punto geográfico y profundidad | Grados WGS84; km |
| `country_id`, `country_name` | Resultado de la unión espacial | ADM0_A3 y nombre del catálogo; null si no asignado |
| `original` | GeoJSON de la versión; mantiene campos opcionales y procedencia | JSONB |

El modelo físico indexa estado actual por `(mode,event_id)`, tiempo `(mode,event_time)`, país/tiempo y magnitud; indexa versiones por `(mode,event_id,updated_ms)`. El grano histórico permite auditoría y evita que una versión antigua reemplace al estado actual.

## 4. Extracción, calidad e integración

El productor captura el GeoJSON antes de transformarlo y deduplica instantáneas iguales por SHA-256. Conserva metadatos de solicitud, respuesta, latencia, registros y error. Los originales JSONB vencen tras `RAW_RETENTION_DAYS` (30 días por defecto); las respuestas históricas descargadas a `/data` se conservan para reproducir la carga. La política de limpieza no infiere eliminaciones a partir de la ausencia en una ventana móvil.

El consumidor convierte tiempos Unix de milisegundos a UTC; conserva nulos en magnitud, profundidad y país; exige geometría Point, tiempo, actualización y coordenadas finitas dentro de WGS84. No rechaza automáticamente magnitudes o profundidades negativas. No asigna un país por proximidad. Los tipos distintos de `earthquake` se registran con motivo; datos inválidos quedan en cuarentena junto al original.

Para obtener evidencias antes/después, ejecutar `backend/sql/quality.sql` contra el feed y la tabla preparada. La primera consulta analiza la última respuesta bruta; las posteriores cuantifican completitud, unicidad, validez, consistencia geográfica y actualidad del estado procesado, y listan cuarentena/exclusiones. Guardar periodo, denominador, resultado y decisión en la entrega; las consultas no fabrican valores faltantes ni confunden “sin país” con error.

La exactitud absoluta no se puede certificar sin una verdad-terreno independiente. Se puede contrastar la geometría frente al GeoJSON original y los límites ADM0_A3 del catálogo, pero USGS puede revisar eventos y Natural Earth generaliza costas y territorios pequeños. Documentar ejemplos marítimos y de frontera de la ejecución real.

## 5. Análisis y conclusiones

`GET /api/estadisticas` aplica el mismo periodo, país y magnitud que el mapa. Calcula ventanas horarias UTC, recuento por país (incluye «Sin país asignado»), distribuciones y ausencias. Cuando hay filtro mínimo de magnitud, el número de magnitudes ausentes se calcula aparte sobre la misma ventana/país antes del filtro; una magnitud nula no cuenta como cero ni satisface el mínimo.

El panel calcula estas tres conclusiones dinámicas a partir de resultados de la API:

1. Cantidad y proporción de eventos sin magnitud antes del filtro mínimo.
2. País con más eventos en la selección y cantidad sin país asignado.
3. Ritmo diario de la selección reciente frente al promedio por día de 2025 (denominador 365, incluidos días con cero eventos).

Los valores se actualizan con los filtros y el backend; no se fijan cifras ficticias en la memoria. Antes de defender, guardar capturas y anotar las cifras, URL/fecha de consulta, periodo UTC, modo, país y magnitud. La ventana reciente puede estar incompleta por retrasos/revisiones del catálogo. La comparación no predice sismos ni evalúa riesgo.

## 6. Complejidad, rendimiento y memoria

Se ejecutó `app.benchmark` con datos **sintéticos**, semilla determinista y fuera de la ingesta/análisis sísmico. Medición en Microsoft Windows 11 Pro x64, Intel Core 7 240H, 31,6 GiB RAM, Python 3.9.13; son tiempos de este equipo y no una garantía de producción.

| Registros sintéticos | Memoria pico asignada a filas + índice | Búsqueda lineal (100 consultas) | Diccionario (100 consultas) | Unión STRtree | Escaneo geométrico lineal | Agregación hora-país |
|---:|---:|---:|---:|---:|---:|---:|
| 1.000 | 665.326 B | 0,00187 s | 0,000016 s | 0,169 s | 0,795 s | 0,00022 s |
| 5.000 | 3.181.020 B | 0,00942 s | 0,000021 s | 0,941 s | 11,337 s | 0,00222 s |
| 10.000 | 6.483.640 B | 0,05508 s | 0,000072 s | 3,810 s | 25,863 s | 0,00410 s |

La búsqueda media en diccionario es O(1), peor caso O(n); el recorrido lineal es O(n) por consulta. STRtree reduce candidatos espaciales en promedio, aunque el peor caso puede acercarse al escaneo de todas las geometrías; con `covers`, los puntos frontera tienen una regla explícita. La agregación hash por hora/país es O(n) esperada y O(n) memoria para las claves distintas. El número de polígonos (177) es pequeño; a 100 millones de eventos conviene conservar índice espacial, particionar temporalmente, agregar por lotes y evaluar PostGIS/materializaciones. No proyectar los tiempos de esta prueba como coste real.

La medición de memoria es `tracemalloc` de las estructuras Python de la prueba, no RSS total del proceso, librerías nativas de GEOS, Kafka ni PostgreSQL. El comando reproduce y guarda el informe de máquina en `/data/benchmark.json`.

## 7. Costes, límites y mejoras

No se estiman precios ni ROI sin tarifa y consumo reales. Para documentar el coste local: electricidad = potencia media medida (kW) × horas encendido × tarifa €/kWh; sumar amortización de equipo, almacenamiento y copias, transferencia, cuotas de túnel/hosting si aplican y horas de desarrollo/mantenimiento. Registrar supuestos y fecha de cada precio. Una cuota gratuita no significa coste total cero.

Como **escenario ilustrativo, no como factura observada**: un equipo que promedia 60 W encendido 24 h/día durante 30 días consumiría 43,2 kWh; a 0,25 €/kWh serían 10,80 €/mes. Si se atribuyen 40 h de un proyecto a un equipo de 1.000 € amortizado en 4 años y 2.000 h productivas/año, la fracción de hardware sería 5 €. Una valoración docente de 40 h × 15 €/h añadiría 600 € de tiempo. Sustituye potencia, tarifa, coste/vida del equipo y horas por datos reales; agrega almacenamiento y transferencia según volumen facturado. No se asigna un ROI: no hay beneficio económico medido.

Riesgos/límites: el equipo, Docker y túnel deben estar encendidos durante la demostración; el feed móvil no permite recuperar automáticamente una parada mayor a 24 h; la instantánea anual puede cambiar si se repite; Kafka de un nodo no ofrece alta disponibilidad; y el acceso público depende del CORS/ngrok configurado por quien publica. Mejoras justificables incluyen pruebas de integración con Compose, recuperación histórica programada tras cortes, métricas/alertas de latencia, particionado y PostGIS si escala el catálogo.

## 8. Ejecución y estado de validación

Instrucciones reproducibles, despliegue, consultas, replay y aceptación están en `README.md`. En este entorno se verificaron la compilación Angular, la generación OpenAPI, ocho pruebas unitarias y `docker compose config`. El motor Docker no estaba disponible, así que aquí no se levantaron Kafka/PostgreSQL ni se ejecutó la carga real de 2025; no se presentan resultados de sismicidad ni pruebas de aceptación del backend como si se hubieran observado. Completar esas evidencias al ejecutar el stack con Docker activo.
