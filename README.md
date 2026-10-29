# Observatorio de actividad sísmica

Proyecto de Sistemas de Big Data. Este paquete incluye el frontend Angular de apoyo y una implementación de referencia del productor, consumidor, almacenamiento, API, carga histórica y reproducción.

## Arquitectura

```text
USGS feed ──> productor Python ──> Kafka (earthquakes.live)
                                      │
                                      v
USGS histórico ─> carga inicial   consumidor Python ──> PostgreSQL
                                       ^                    │
Reproducción ──> Kafka (earthquakes.replay)                 v
                                                    FastAPI /api
                                                         │
                                              ngrok HTTPS │
                                                         v
                                              Angular + Leaflet
```

Los cinco servicios obligatorios son contenedores separados en Compose: `producer`, `kafka`, `consumer`, `postgres` y `api`. Productor y consumidor comparten una imagen Python con comandos diferentes. Kafka y PostgreSQL no se publican en el host. Solo se expone la API en `127.0.0.1:8000`.

El frontend ofrece dos modos: demostración sintética para comprobar el mapa y API para el trabajo con datos procesados. Nunca utiliza el feed USGS directamente desde el navegador.

## Requisitos

- Docker Desktop con Compose v2 en ejecución.
- Node.js 22.12 o posterior en la rama 22, o Node 24, y npm para desarrollo local del panel.
- Acceso de red a USGS, npm y, si se activa, ngrok/Netlify.

## Puesta en marcha

Desde esta carpeta del proyecto:

```powershell
Copy-Item .env.example .env
```

Edita `.env`: sustituye `POSTGRES_PASSWORD` por una contraseña local URL-safe (letras, números, `_` o `-`) y `CORS_ORIGINS` por `http://localhost:4200` y el origen exacto de tu sitio de Netlify. No subas `.env`, authtokens ni claves al repositorio.

Levanta los cinco servicios:

```powershell
docker compose up --build -d
docker compose ps
docker compose logs -f producer consumer api
```

PostgreSQL crea el esquema al inicializar el volumen; la API verifica el esquema y registra los 177 países del GeoJSON al arrancar. Los contenedores esperan a las comprobaciones de salud de sus dependencias. El productor consulta el feed cada 60 segundos; el primer ciclo se inicia al arrancar.

Comprobaciones rápidas:

```powershell
Invoke-RestMethod http://localhost:8000/api/salud
Invoke-RestMethod http://localhost:8000/api/eventos
```

`/api/salud` devuelve estado de base de datos y última ingesta correcta. Para detener:

```powershell
docker compose down
```

Los volúmenes `postgres_data` y `kafka_data` persisten al detener los servicios. `docker compose down -v` elimina esos datos de manera irreversible; no lo uses si deseas conservarlos. Los GeoJSON originales descargados se guardan bajo `data/`, fuera de Git.

## Carga del histórico USGS

La carga propuesta es mundial, año 2025, `eventtype=earthquake`, magnitud mínima 4. Consulta por meses; subdivide automáticamente intervalos que alcancen 20.000 respuestas y deduplica por `event_id`. Reutiliza copias locales por intervalo en `data/historical/2025`; `--refresh` vuelve a consultar USGS. Conserva cada respuesta original por su hash y registra URL/filtros, hora y conteo en `raw_snapshots`/`ingestion_runs`.

Con los servicios arriba:

```powershell
docker compose run --rm api python -m app.load_historical --year 2025 --output /data/historical/2025
```

Para obtener una instantánea nueva del catálogo, añade `--refresh`.

La carga se puede repetir: las claves únicas hacen idempotentes los eventos. Las consultas y las respuestas originales son instantáneas de la fecha de extracción; no representan revisiones históricas completas de USGS. El feed móvil de 24 horas no es un sustituto de la recuperación de huecos: tras una parada superior a 24 horas, vuelve a ejecutar la carga del intervalo afectado mediante el catálogo FDSN y conserva los parámetros usados.

## API

- `GET /api/salud`: disponibilidad de API/base de datos y última ingesta correcta.
- `GET /api/paises`: identificadores ADM0_A3 y nombres del catálogo suministrado.
- `GET /api/eventos`: GeoJSON de estado actual, por defecto últimas 24 horas, una sola versión por evento. Filtros: `start` inclusivo, `end` exclusivo, `min_mag`, `country_id`, `mode=live|replay`, `limit` y `offset`. `country_id=__none__` filtra eventos sin país. La API da como máximo 5.000 filas por página y comunica el total y `truncated`; las estadísticas no se calculan sobre esa página.
- `GET /api/estadisticas`: conteo por país y hora UTC, distribuciones, nulos y comparación con el promedio diario de 2025. Usa los mismos filtros y la tabla completa. El histórico incluye los 365 días, también los días con cero eventos.

Todas las fechas de la API son milisegundos Unix UTC. GeoJSON usa longitud, latitud y profundidad en km. Magnitud, profundidad y país desconocidos se conservan como null/«Sin país asignado». Los errores de base de datos son HTTP 503 explícitos, nunca una respuesta vacía exitosa.

CORS permite `GET`/`OPTIONS` desde los orígenes configurados y la cabecera `ngrok-skip-browser-warning`. CORS no autentica; el API solo consulta datos públicos y no ofrece rutas administrativas.

## Replay aislado

El modo de reproducción se guarda con `mode=replay`, separado de `live` e `historical`, y se distribuye por el topic `earthquakes.replay`. Conserva el tiempo original y añade `replayed_at`. En el panel elige **Reproducción** para consultarlo.

```powershell
docker compose run --rm --no-deps api python -m app.replay --limit 1000 --delay 0.02 --include-cases
```

`--include-cases` envía una versión repetida, una revisión posterior y luego una versión antigua del mismo evento. El estado actual debe conservar la revisión y el historial guardar las versiones distintas una sola vez. La revisión de prueba lleva `test_case=true`, es visible y está excluida de las estadísticas. Es necesario haber cargado el histórico y tener Kafka/consumidor en ejecución.

## Pruebas y análisis de calidad

Pruebas deterministas de transformación y geografía:

```powershell
docker compose run --rm --no-deps api python -m unittest discover -s tests -v
```

Consultas de calidad y procedencia (ajusta modo/fechas dentro del archivo antes de entregar resultados):

```powershell
docker compose exec -T postgres psql -U observatorio -d observatorio -f /tmp/quality.sql
```

El SQL comprueba completitud, unicidad, validez, claves geográficas, actualidad, cuarentena y exclusiones. No reemplaza la interpretación de resultados en la memoria. Un nulo no se convierte en cero, una profundidad negativa no se rechaza automáticamente y los casos sintéticos se mantienen separados de datos reales.

Aceptación manual recomendada:

1. Publicar una versión nueva y verificarla en `/api/eventos`.
2. Reenviar la misma versión: no crece `events_current` ni `event_versions`.
3. Enviar una versión más reciente y luego una antigua: la más reciente continúa en `events_current`.
4. Enviar magnitud nula, evento oceánico y coordenadas inválidas: nulo y evento oceánico se conservan; coordenadas inválidas quedan en `quarantine`.
5. Reiniciar consumidor; los offsets se confirman después de la transacción y no se duplican efectos.
6. Desconectar ngrok: el panel comunica el fallo y conserva la última lectura; al volver, el refresco se recupera.
7. Usar `--include-cases` y confirmar el aislamiento del modo replay.

## Frontend y despliegue

```powershell
Set-Location frontend
npm ci
npm start
```

En modo demo se ven cuatro eventos ficticios y los filtros de fecha, magnitud y país se aplican localmente a esos ejemplos; el periodo inicial se ajusta a sus fechas. Para conectar la API, edita `frontend/public/config.json`, establece `mode` a `api` y `apiUrl` a la URL pública HTTPS de ngrok sin `/api`. El panel comienza en magnitud mínima 4 para que la comparación anual use un criterio equivalente; puedes borrar el filtro y la ausencia de magnitud seguirá contabilizándose aparte. El `refreshMs` por defecto es 60.000 ms. Reinicia `npm start` o vuelve a compilar después del cambio.

Con ngrok instalado en el equipo:

```powershell
ngrok http 8000
```

La API solo se publica en loopback. Desde otro contenedor de la red Compose el destino es `http://api:8000`; nunca `localhost:8000`. ngrok termina HTTPS y gestiona el certificado. El authtoken se configura en el agente ngrok, nunca en Angular.

Netlify: directorio base `frontend`, comando `npm run build`, directorio de publicación `dist/observatorio/browser`. Actualiza `config.json` con la URL asignada antes del build y usa el dominio exacto de Netlify en `CORS_ORIGINS`. Se mantiene el archivo `_redirects` para recargas SPA.

## Complejidad, rendimiento y costes

Ejecuta la medición reproducible; crea datos sintéticos etiquetados y excluidos de las conclusiones de sismicidad:

```powershell
docker compose run --rm --no-deps api python -m app.benchmark --sizes 1000 5000 10000 --output /data/benchmark.json
```

El informe guarda sistema, hora, tamaños, memoria asignada al índice y tiempo de búsqueda lineal/diccionario, integración con STRtree/recorrido lineal y agregación por hora-país. Registra el equipo, repite la medición y copia resultados reales a la memoria; no inventes cifras de ejecución. Al proyectar 100 millones de filas, discute índices B-tree compuestos, particionado temporal, índices espaciales y agregaciones materializadas; la RAM y el disco pasan a dominar.

Para estimar coste, indica precio local de hardware y electricidad (potencia media × horas × tarifa), almacenamiento (base, copias y originales), transferencia, tiempo de mantenimiento/desarrollo y cuotas o límites de servicios. El modo local no tiene coste cloud cero: documenta tiempo y consumo. No se asigna beneficio económico no medido.

## Modelo y reglas

- `events_current`: grano `(mode, event_id)`, la versión actual más reciente.
- `event_versions`: grano `(mode, event_id, updated_ms)`, historial inmutable de versiones.
- `published_state`: última versión confirmada por Kafka, evita publicar duplicados en el ciclo ordinario.
- `raw_snapshots`: respuesta original GeoJSON JSONB, hash, consulta y caducidad; se purgan tras `RAW_RETENTION_DAYS` (30 por defecto).
- `ingestion_runs`: consulta, estado, latencia, conteo y error.
- `countries`: dimensión con `ADM0_A3`, nombre, continente y subregión.
- `excluded_events` y `quarantine`: filtros por tipo y errores de validación con causa y original.

El índice STRtree acelera la búsqueda de polígonos candidatos. `covers` incluye los puntos frontera; si hay más de una coincidencia, se elige el ADM0_A3 lexicográficamente menor para una decisión estable. Sin coincidencia se guarda country null, sin país más cercano.

Las versiones y el estado actual se escriben en la misma transacción; el offset Kafka se confirma después. Se implementa entrega al menos una vez, no una transacción distribuida. Un reenvío es idempotente por claves primarias y el upsert actual solo acepta `updated_ms` mayor. Los datos de replay usan el mismo consumidor y las mismas garantías.

## Límites operativos

No se incluyen una URL pública de Netlify, una URL ngrok ni resultados observados del equipo: requieren cuentas y ejecución locales. El navegador no es necesario para la ingesta. El feed no informa por sí solo si un evento expiró de la ventana móvil o fue eliminado; no se pretende reconciliar revisiones fuera de esa ventana automáticamente. Natural Earth simplifica costas y no atribuye necesariamente todo evento marítimo.
