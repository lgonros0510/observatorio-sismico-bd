-- Cambia 'live' por 'historical' o 'replay' y ajusta el periodo para el análisis.
\set mode 'live'
\set start_time '2026-10-28T00:00:00Z'
\set end_time '2026-10-29T00:00:00Z'

-- Foto de entrada más reciente: evidencia "antes" de filtrar/validar el feed.
WITH latest AS (
    SELECT payload
    FROM raw_snapshots
    WHERE source = 'usgs_feed'
    ORDER BY fetched_at DESC
    LIMIT 1
),
raw_features AS (
    SELECT feature,
           CASE WHEN jsonb_typeof(feature->'geometry'->'coordinates') = 'array'
                     AND jsonb_typeof(feature->'geometry'->'coordinates'->0) = 'number'
                THEN (feature->'geometry'->'coordinates'->>0)::double precision END AS longitude,
           CASE WHEN jsonb_typeof(feature->'geometry'->'coordinates') = 'array'
                     AND jsonb_typeof(feature->'geometry'->'coordinates'->1) = 'number'
                THEN (feature->'geometry'->'coordinates'->>1)::double precision END AS latitude
    FROM latest
    CROSS JOIN LATERAL jsonb_array_elements(latest.payload->'features') AS item(feature)
)
SELECT
    count(*) AS raw_features,
    count(*) FILTER (
        WHERE feature->'properties'->>'mag' IS NULL
           OR feature->'properties'->'mag' = 'null'::jsonb
    ) AS missing_magnitude,
    count(*) FILTER (
        WHERE feature->'properties'->>'place' IS NULL
           OR feature->'properties'->'place' = 'null'::jsonb
    ) AS missing_place,
    count(*) FILTER (
        WHERE feature->'properties'->>'type' IS DISTINCT FROM 'earthquake'
    ) AS non_earthquake_events,
    count(*) FILTER (
        WHERE feature->'geometry'->>'type' IS DISTINCT FROM 'Point'
           OR jsonb_typeof(feature->'geometry'->'coordinates') IS DISTINCT FROM 'array'
    ) AS invalid_point_geometry,
    count(*) FILTER (
        WHERE longitude IS NULL OR latitude IS NULL
           OR longitude NOT BETWEEN -180 AND 180
           OR latitude NOT BETWEEN -90 AND 90
    ) AS invalid_coordinates
FROM raw_features;

-- Completitud y cardinalidad de la tabla de estado actual.
SELECT
    count(*) AS rows,
    count(*) FILTER (WHERE magnitude IS NOT NULL) AS with_magnitude,
    round(100.0 * count(*) FILTER (WHERE magnitude IS NOT NULL) / NULLIF(count(*), 0), 2) AS magnitude_pct,
    count(*) FILTER (WHERE depth_km IS NOT NULL) AS with_depth,
    round(100.0 * count(*) FILTER (WHERE depth_km IS NOT NULL) / NULLIF(count(*), 0), 2) AS depth_pct,
    count(*) FILTER (WHERE place IS NOT NULL) AS with_place,
    round(100.0 * count(*) FILTER (WHERE place IS NOT NULL) / NULLIF(count(*), 0), 2) AS place_pct,
    count(*) FILTER (WHERE country_id IS NOT NULL) AS with_country,
    round(100.0 * count(*) FILTER (WHERE country_id IS NOT NULL) / NULLIF(count(*), 0), 2) AS country_pct
FROM events_current
WHERE mode = :'mode' AND test_case = false
  AND event_time >= :'start_time'::timestamptz AND event_time < :'end_time'::timestamptz;

-- Unicidad lógica (debe devolver cero filas) y versiones repetidas deduplicadas por PK.
SELECT mode, event_id, count(*) AS current_rows
FROM events_current
WHERE mode = :'mode' AND test_case = false
GROUP BY mode, event_id HAVING count(*) > 1;
SELECT mode, event_id, updated_ms, count(*) AS copies
FROM event_versions
WHERE mode = :'mode' AND test_case = false
GROUP BY mode, event_id, updated_ms HAVING count(*) > 1;

-- Validez y valores que requieren análisis, no sustitución automática.
SELECT
    count(*) FILTER (WHERE event_time IS NULL) AS missing_event_time,
    count(*) FILTER (WHERE longitude NOT BETWEEN -180 AND 180 OR latitude NOT BETWEEN -90 AND 90) AS out_of_range_coordinates,
    count(*) FILTER (WHERE magnitude < -2 OR magnitude > 10) AS unusual_magnitudes,
    count(*) FILTER (WHERE depth_km < 0) AS negative_depths,
    count(*) FILTER (WHERE e.country_id IS NOT NULL AND c.country_id IS NULL) AS orphan_country_keys
FROM events_current e LEFT JOIN countries c USING (country_id)
WHERE e.mode = :'mode' AND e.test_case = false
  AND e.event_time >= :'start_time'::timestamptz AND e.event_time < :'end_time'::timestamptz;

-- Actualidad: retrasos observados entre ocurrencia, actualización del catálogo e ingesta.
SELECT
    percentile_cont(0.5) WITHIN GROUP (ORDER BY ingested_at - event_time) AS median_event_to_ingestion,
    percentile_cont(0.95) WITHIN GROUP (ORDER BY ingested_at - event_time) AS p95_event_to_ingestion,
    percentile_cont(0.5) WITHIN GROUP (
        ORDER BY ingested_at - to_timestamp(updated_ms / 1000.0)
    ) AS median_update_to_ingestion
FROM events_current
WHERE mode = :'mode' AND test_case = false
  AND event_time >= :'start_time'::timestamptz AND event_time < :'end_time'::timestamptz;

-- Rechazos y exclusiones documentados por motivo.
SELECT 'quarantine' AS category, reason, count(*) AS rows
FROM quarantine WHERE mode = :'mode' GROUP BY reason
UNION ALL
SELECT 'excluded', reason, count(*) FROM excluded_events WHERE mode = :'mode' GROUP BY reason
ORDER BY category, rows DESC;

-- Exactitud: USGS no proporciona verdad-terreno independiente; revisar una muestra frente
-- al GeoJSON original y documentar límites de cobertura/costa del catálogo Natural Earth.
