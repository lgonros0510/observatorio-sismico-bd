# Contrato mínimo de la API

## GET /api/eventos

Devuelve HTTP 200, `Content-Type: application/json` y un GeoJSON `FeatureCollection`. Sin eventos: `features: []`, no un error. Cada evento aparece una sola vez, con su versión actual. Por defecto devuelve terremotos ocurridos en las últimas 24 horas del modo `live`.

```json
{
  "type": "FeatureCollection",
  "metadata": {
    "generated": 1793113200000,
    "last_ingestion": 1793113140000,
    "mode": "live",
    "count": 1,
    "total": 1,
    "limit": 5000,
    "offset": 0,
    "truncated": false,
    "period": {"start": 1793026800000, "end": 1793113200000},
    "filters": {"min_mag": null, "country_id": null}
  },
  "features": [{
    "type": "Feature",
    "id": "ejemplo-ficticio",
    "geometry": {"type": "Point", "coordinates": [-3.7, 40.4, 12]},
    "properties": {
      "mag": 3.2,
      "magType": "ml",
      "place": "Ejemplo ficticio",
      "time": 1793109600000,
      "updated": 1793110000000,
      "country_id": "ESP",
      "country_name": "España",
      "test_case": false
    }
  }]
}
```

`generated`: generación de esta respuesta; no implica que la ingesta siga activa. `last_ingestion`: última consulta correcta a USGS. Fechas en milisegundos Unix UTC. Coordenadas en orden longitud, latitud, profundidad (km); si la profundidad falta, se devuelven longitud/latitud. Usa `null`, nunca NaN/Infinity, para magnitud, profundidad o país desconocidos. Geometría válida obligatoria en este endpoint.

Filtros: `start` (ISO8601, inclusive), `end` (ISO8601, exclusive), `min_mag`, `country_id` y `mode=live|replay`. El identificador especial `country_id=__none__` selecciona eventos sin país. La magnitud nula no cumple un filtro `min_mag` y debe contabilizarse por separado en el análisis de calidad, usando la misma ventana temporal/geográfica pero sin aplicar el filtro de magnitud.

La API limita cada página a 5.000 puntos y ofrece `limit` y `offset`. Si quedan eventos, `truncated` es true; `total` es el conteo completo. El panel permite cargar más puntos y no calcula estadísticas globales sobre una muestra truncada.

## Otros endpoints a implementar

| Ruta | Resultado |
|---|---|
| GET /api/salud | Estado API/base de datos y fecha de última ingesta; no credenciales |
| GET /api/paises | Catálogo de países y regiones disponible para filtros |
| GET /api/estadisticas | Conteos por hora UTC/país, distribuciones, nulos y comparación histórica |

Los filtros de estadísticas equivalen a los del mapa. Cada resultado indica periodo y filtros; los recuentos se calculan sobre el conjunto completo de la base, no sobre la página del mapa. El total sin magnitud se calcula antes del filtro `min_mag`; el resto de métricas se calcula sobre la selección activa. Los registros replay con `test_case=true` pueden mostrarse en el mapa/tabla, pero quedan excluidos de las estadísticas y la comparación. La comparación fija usa el año 2025 y divide por sus 365 días, incluidos los días con cero eventos. Si la base falla, devuelve un error explícito; no cero eventos como si fuera un resultado válido.

## Acceso desde Netlify

Configura CORS en FastAPI para el origen exacto de la web y `http://localhost:4200` durante desarrollo; métodos GET y OPTIONS, y cabecera `ngrok-skip-browser-warning`. Si utilizas `CORSMiddleware`, este middleware atiende el preflight. No necesitas cookies para una API pública de consulta de estos datos.

El encabezado de ngrok evita su página intermedia cuando es aplicable; no autentica al usuario. No incluir secretos en config.json. Añade timeout y manejo de errores en el frontend.

## Transporte y contenedores

La API es un servicio Docker, igual que productor, consumidor, Kafka y PostgreSQL. Uvicorn escucha en `0.0.0.0:8000`. ngrok termina el HTTPS público y gestiona el certificado; la API recibe HTTP local. El frontend utiliza la URL HTTPS del túnel. Desde otro contenedor de la misma red, el agente se conecta a `http://api:8000`; desde el host, al puerto publicado.
