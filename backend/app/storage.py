import hashlib
import json
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import psycopg
from psycopg.types.json import Jsonb

from app.catalog import CountryCatalog


class InvalidEvent(ValueError):
    pass


def database_url() -> str:
    value = os.getenv("DATABASE_URL")
    if not value:
        raise RuntimeError("DATABASE_URL no está configurada")
    return value


def ensure_schema(connection: psycopg.Connection) -> None:
    schema = Path(__file__).parent.parent / "sql" / "init.sql"
    connection.execute(schema.read_text(encoding="utf-8"))


def upsert_countries(connection: psycopg.Connection, catalog: CountryCatalog) -> None:
    connection.executemany(
        """
        INSERT INTO countries (country_id, name_es, admin_name, continent, subregion)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (country_id) DO UPDATE SET
            name_es = EXCLUDED.name_es,
            admin_name = EXCLUDED.admin_name,
            continent = EXCLUDED.continent,
            subregion = EXCLUDED.subregion
        """,
        [
            (c["country_id"], c["name_es"], c["admin_name"], c["continent"], c["subregion"])
            for c in catalog.features
        ],
    )


def save_raw_snapshot(
    connection: psycopg.Connection,
    source: str,
    payload: Any,
    query: dict[str, Any],
    retention_days: Optional[int] = None,
) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    digest = hashlib.sha256(source.encode("utf-8") + b"\0" + encoded).hexdigest()
    fetched_at = datetime.now(timezone.utc)
    retention = retention_days if retention_days is not None else int(os.getenv("RAW_RETENTION_DAYS", "30"))
    connection.execute(
        """
        INSERT INTO raw_snapshots (sha256, source, fetched_at, expires_at, query, payload)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (sha256) DO UPDATE SET expires_at = GREATEST(raw_snapshots.expires_at, EXCLUDED.expires_at)
        """,
        (digest, source, fetched_at, fetched_at + timedelta(days=retention), Jsonb(query), Jsonb(payload)),
    )
    connection.execute("DELETE FROM raw_snapshots WHERE expires_at < %s", (fetched_at,))
    return digest


def _number(value: Any, label: str, nullable: bool = False) -> Optional[float]:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise InvalidEvent(f"{label} debe ser un número finito" + (" o null" if nullable else ""))
    return float(value)


def normalize_event(envelope: dict[str, Any], catalog: CountryCatalog) -> dict[str, Any]:
    event = envelope.get("event")
    if not isinstance(event, dict):
        raise InvalidEvent("Falta el objeto event")
    event_id = event.get("id")
    if not isinstance(event_id, str) or not event_id.strip():
        raise InvalidEvent("id ausente o inválido")

    properties = event.get("properties")
    geometry = event.get("geometry")
    if not isinstance(properties, dict):
        raise InvalidEvent("properties ausente o inválido")
    if not isinstance(geometry, dict) or geometry.get("type") != "Point":
        raise InvalidEvent("geometry debe ser Point")

    time_ms = properties.get("time")
    updated_ms = properties.get("updated")
    if isinstance(time_ms, bool) or not isinstance(time_ms, int):
        raise InvalidEvent("properties.time debe ser un timestamp Unix en milisegundos")
    if isinstance(updated_ms, bool) or not isinstance(updated_ms, int):
        raise InvalidEvent("properties.updated debe ser un timestamp Unix en milisegundos")
    try:
        event_time = datetime.fromtimestamp(time_ms / 1000, tz=timezone.utc)
        datetime.fromtimestamp(updated_ms / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError) as error:
        raise InvalidEvent("time o updated fuera del rango de fechas") from error

    coordinates = geometry.get("coordinates")
    if not isinstance(coordinates, list) or len(coordinates) < 2:
        raise InvalidEvent("coordinates debe incluir longitud y latitud")
    longitude = _number(coordinates[0], "longitud")
    latitude = _number(coordinates[1], "latitud")
    depth = _number(coordinates[2], "profundidad", nullable=True) if len(coordinates) > 2 else None
    if not -180 <= longitude <= 180 or not -90 <= latitude <= 90:
        raise InvalidEvent("coordenadas fuera de rango WGS84")
    magnitude = _number(properties.get("mag"), "mag", nullable=True)
    event_type = properties.get("type")
    if not isinstance(event_type, str):
        raise InvalidEvent("properties.type ausente o inválido")
    magnitude_type = properties.get("magType")
    if magnitude_type is not None and not isinstance(magnitude_type, str):
        raise InvalidEvent("properties.magType debe ser texto o null")
    place = properties.get("place")
    if place is not None and not isinstance(place, str):
        raise InvalidEvent("properties.place debe ser texto o null")

    mode = envelope.get("mode", "live")
    if not isinstance(mode, str) or mode not in {"live", "replay", "historical"}:
        raise InvalidEvent("mode debe ser live, replay o historical")
    source = envelope.get("source", "unknown")
    if not isinstance(source, str):
        raise InvalidEvent("source debe ser texto")
    ingested_at = _parse_iso(envelope.get("ingested_at")) or datetime.now(timezone.utc)
    replayed_at = _parse_iso(envelope.get("replayed_at"))
    test_case = envelope.get("test_case", False)
    if not isinstance(test_case, bool):
        raise InvalidEvent("test_case debe ser booleano")
    if test_case and mode != "replay":
        raise InvalidEvent("test_case solo se permite en modo replay")

    return {
        "mode": mode,
        "event_id": event_id,
        "updated_ms": updated_ms,
        "source": source,
        "ingested_at": ingested_at,
        "replayed_at": replayed_at,
        "test_case": test_case,
        "event_time": event_time,
        "magnitude": magnitude,
        "magnitude_type": magnitude_type,
        "event_type": event_type,
        "place": place,
        "longitude": longitude,
        "latitude": latitude,
        "depth_km": depth,
        "country_id": catalog.country_for(longitude, latitude),
        "original": event,
    }


def _parse_iso(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidEvent("Las fechas de ingesta deben ser ISO8601")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise InvalidEvent("Fecha de ingesta ISO8601 inválida") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def quarantine(
    connection: psycopg.Connection,
    mode: str,
    source: str,
    original: Any,
    reason: str,
    event_id: Optional[str] = None,
) -> None:
    connection.execute(
        """
        INSERT INTO quarantine (mode, event_id, reason, source, original)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (mode, event_id, reason, source, Jsonb(original)),
    )


def store_message(connection: psycopg.Connection, envelope: dict[str, Any], catalog: CountryCatalog) -> str:
    mode = envelope.get("mode", "live")
    source = envelope.get("source", "unknown")
    event = envelope.get("event")
    event_id = event.get("id") if isinstance(event, dict) and isinstance(event.get("id"), str) else None
    properties = event.get("properties") if isinstance(event, dict) else None
    event_type = properties.get("type") if isinstance(properties, dict) else None
    if isinstance(event_type, str) and event_type != "earthquake":
        updated_ms = properties.get("updated")
        if (
            isinstance(mode, str)
            and mode in {"live", "replay", "historical"}
            and isinstance(source, str)
            and event_id
            and isinstance(updated_ms, int)
            and not isinstance(updated_ms, bool)
            and -(2**63) <= updated_ms <= 2**63 - 1
        ):
            connection.execute(
                """
                INSERT INTO excluded_events (mode, event_id, updated_ms, reason, source, original)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (mode, event_id, updated_ms) DO NOTHING
                """,
                (mode, event_id, updated_ms, f"event_type={event_type}", source, Jsonb(event)),
            )
            return "excluded"
        quarantine(
            connection,
            mode if isinstance(mode, str) and mode in {"live", "replay", "historical"} else "unknown",
            source if isinstance(source, str) else "unknown",
            envelope,
            "Evento no terremoto sin modo, fuente, id o updated válidos",
            event_id,
        )
        return "quarantined"
    try:
        row = normalize_event(envelope, catalog)
    except InvalidEvent as error:
        quarantine(
            connection,
            mode if isinstance(mode, str) and mode in {"live", "replay", "historical"} else "unknown",
            source if isinstance(source, str) else "unknown",
            envelope,
            str(error),
            event_id,
        )
        return "quarantined"

    if row["event_type"] != "earthquake":
        connection.execute(
            """
            INSERT INTO excluded_events (mode, event_id, updated_ms, reason, source, original)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (mode, event_id, updated_ms) DO NOTHING
            """,
            (mode, row["event_id"], row["updated_ms"], f"event_type={row['event_type']}", source, Jsonb(row["original"])),
        )
        return "excluded"

    values = (
        row["mode"], row["test_case"], row["event_id"], row["updated_ms"], row["source"], row["ingested_at"], row["replayed_at"],
        row["event_time"], row["magnitude"], row["magnitude_type"], row["event_type"], row["place"],
        row["longitude"], row["latitude"], row["depth_km"], row["country_id"], Jsonb(row["original"]),
    )
    connection.execute(
        """
        INSERT INTO event_versions (
            mode, test_case, event_id, updated_ms, source, ingested_at, replayed_at, event_time, magnitude,
            magnitude_type, event_type, place, longitude, latitude, depth_km, country_id, original
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (mode, event_id, updated_ms) DO NOTHING
        """,
        values,
    )
    connection.execute(
        """
        INSERT INTO events_current (
            mode, test_case, event_id, updated_ms, source, ingested_at, replayed_at, event_time, magnitude,
            magnitude_type, event_type, place, longitude, latitude, depth_km, country_id, original
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (mode, event_id) DO UPDATE SET
            test_case = EXCLUDED.test_case,
            updated_ms = EXCLUDED.updated_ms,
            source = EXCLUDED.source,
            ingested_at = EXCLUDED.ingested_at,
            replayed_at = EXCLUDED.replayed_at,
            event_time = EXCLUDED.event_time,
            magnitude = EXCLUDED.magnitude,
            magnitude_type = EXCLUDED.magnitude_type,
            event_type = EXCLUDED.event_type,
            place = EXCLUDED.place,
            longitude = EXCLUDED.longitude,
            latitude = EXCLUDED.latitude,
            depth_km = EXCLUDED.depth_km,
            country_id = EXCLUDED.country_id,
            original = EXCLUDED.original
        WHERE events_current.updated_ms < EXCLUDED.updated_ms
        """,
        values,
    )
    return "stored"


def record_run(
    connection: psycopg.Connection,
    source: str,
    mode: str,
    requested_at: datetime,
    completed_at: datetime,
    status: str,
    record_count: int,
    http_status: Optional[int],
    response_ms: int,
    query: dict[str, Any],
    error: Optional[str] = None,
) -> None:
    connection.execute(
        """
        INSERT INTO ingestion_runs (
            source, mode, requested_at, completed_at, status, record_count,
            http_status, response_ms, query, error
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (source, mode, requested_at, completed_at, status, record_count, http_status, response_ms, Jsonb(query), error),
    )
