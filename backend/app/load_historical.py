import argparse
import hashlib
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import psycopg

from app.catalog import CountryCatalog
from app.storage import database_url, ensure_schema, record_run, save_raw_snapshot, store_message, upsert_countries


logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("observatorio.historical")
BASE_URL = "https://earthquake.usgs.gov/fdsnws/event/1/query"
MAX_RESULTS = 20_000


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Número no JSON permitido: {value}")


def _query(start: datetime, end: datetime) -> dict[str, str]:
    return {
        "format": "geojson",
        "starttime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "endtime": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "minmagnitude": "4",
        "eventtype": "earthquake",
        "orderby": "time-asc",
        "limit": str(MAX_RESULTS),
    }


def _updated_ms(feature: dict[str, Any]) -> int:
    properties = feature.get("properties")
    value = properties.get("updated") if isinstance(properties, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else -1


def _fetch_interval(
    client: httpx.Client,
    start: datetime,
    end: datetime,
    output: Path,
    refresh: bool = False,
) -> list[dict[str, Any]]:
    params = _query(start, end)
    requested_at = datetime.now(timezone.utc)
    started = time.monotonic()
    status_code = None
    try:
        output.mkdir(parents=True, exist_ok=True)
        prefix = f"{start.strftime('%Y%m%dT%H%M%S')}_{end.strftime('%Y%m%dT%H%M%S')}_"
        cached_files = sorted(output.glob(f"{prefix}*.geojson"), key=lambda path: path.stat().st_mtime)
        if cached_files and not refresh:
            payload = json.loads(cached_files[-1].read_bytes(), parse_constant=_reject_json_constant)
            logger.info("Reutilizando respuesta local %s", cached_files[-1].name)
        else:
            for attempt in range(5):
                try:
                    response = client.get(BASE_URL, params=params, timeout=60)
                    status_code = response.status_code
                    response.raise_for_status()
                    break
                except httpx.HTTPStatusError as error:
                    if error.response.status_code != 429 and error.response.status_code < 500:
                        raise
                    if attempt == 4:
                        raise
                    time.sleep(min(60, 2 ** attempt))
                except httpx.TransportError:
                    if attempt == 4:
                        raise
                    time.sleep(min(60, 2 ** attempt))
            payload = json.loads(response.content, parse_constant=_reject_json_constant)
            if not isinstance(payload, dict) or payload.get("type") != "FeatureCollection" or not isinstance(payload.get("features"), list):
                raise ValueError("USGS respondió con un GeoJSON inválido")

            fetched_at = datetime.now(timezone.utc)
            digest = hashlib.sha256(response.content).hexdigest()
            snapshot_file = output / f"{prefix}{digest[:12]}.geojson"
            if not snapshot_file.exists():
                snapshot_file.write_bytes(response.content)
            with psycopg.connect(database_url()) as connection:
                save_raw_snapshot(connection, "usgs_catalog", payload, params)
                record_run(
                    connection, "usgs_catalog", "historical", requested_at, fetched_at, "success",
                    len(payload["features"]), status_code, int((time.monotonic() - started) * 1000), params,
                )

        if not isinstance(payload, dict) or payload.get("type") != "FeatureCollection" or not isinstance(payload.get("features"), list):
            raise ValueError("La copia histórica local no es un GeoJSON FeatureCollection")
        features = payload["features"]
        metadata = payload.get("metadata")
        result_count = metadata.get("count", len(features)) if isinstance(metadata, dict) else len(features)
        if result_count >= MAX_RESULTS or len(features) >= MAX_RESULTS:
            duration = end - start
            if duration <= timedelta(minutes=1):
                raise RuntimeError(f"El intervalo sigue alcanzando {MAX_RESULTS} registros al reducirlo a un minuto")
            midpoint = start + duration / 2
            logger.info("Intervalo lleno (%d): subdividiendo %s - %s", result_count, start, end)
            left = _fetch_interval(client, start, midpoint, output, refresh)
            right = _fetch_interval(client, midpoint, end, output, refresh)
            return left + right
        return features
    except Exception as error:
        completed_at = datetime.now(timezone.utc)
        with psycopg.connect(database_url()) as connection:
            record_run(
                connection, "usgs_catalog", "historical", requested_at, completed_at, "error",
                0, status_code, int((time.monotonic() - started) * 1000), params, str(error),
            )
        raise


def load(year: int, output: Path, refresh: bool = False) -> int:
    catalog = CountryCatalog()
    with psycopg.connect(database_url()) as connection:
        ensure_schema(connection)
        upsert_countries(connection, catalog)

    start_year = datetime(year, 1, 1, tzinfo=timezone.utc)
    end_year = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    all_features: dict[str, dict[str, Any]] = {}
    unkeyed_features: list[Any] = []
    with httpx.Client(headers={"User-Agent": "observatorio-sismico/1.0"}) as client:
        cursor = start_year
        while cursor < end_year:
            next_month = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
            interval_end = min(next_month, end_year)
            records = _fetch_interval(client, cursor, interval_end, output, refresh)
            for feature in records:
                event_id = feature.get("id") if isinstance(feature, dict) else None
                if isinstance(event_id, str):
                    current = all_features.get(event_id)
                    candidate_updated = _updated_ms(feature)
                    current_updated = _updated_ms(current) if current else -1
                    if candidate_updated >= current_updated:
                        all_features[event_id] = feature
                else:
                    unkeyed_features.append(feature)
            logger.info("Consulta %s - %s: %d registros; %d eventos únicos acumulados", cursor, interval_end, len(records), len(all_features))
            cursor = interval_end

    ingested_at = datetime.now(timezone.utc).isoformat()
    stored = 0
    with psycopg.connect(database_url()) as connection:
        for event in [*all_features.values(), *unkeyed_features]:
            outcome = store_message(
                connection,
                {"source": "usgs_catalog", "ingested_at": ingested_at, "mode": "historical", "event": event},
                catalog,
            )
            stored += outcome == "stored"
    logger.info("Carga histórica %d terminada: %d eventos únicos enviados a persistencia", year, len(all_features))
    return stored


def main() -> None:
    parser = argparse.ArgumentParser(description="Descarga e importa el histórico USGS por intervalos mensuales")
    parser.add_argument("--year", type=int, default=2025)
    parser.add_argument("--output", type=Path, default=Path("/data/historical"))
    parser.add_argument("--refresh", action="store_true", help="Vuelve a consultar USGS aunque haya respuestas locales")
    args = parser.parse_args()
    if args.year != 2025:
        parser.error("La comparación implementada utiliza el año histórico 2025")
    count = load(args.year, args.output, args.refresh)
    print(f"Carga completada: {count} eventos nuevos/actualizados de {args.year}.")


if __name__ == "__main__":
    main()
