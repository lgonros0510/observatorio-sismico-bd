import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

import psycopg
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from psycopg.rows import dict_row

from app.catalog import CountryCatalog
from app.storage import database_url, ensure_schema, upsert_countries


logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("observatorio.api")


@asynccontextmanager
async def lifespan(_: FastAPI):
    catalog = CountryCatalog()
    while True:
        try:
            with psycopg.connect(database_url()) as connection:
                ensure_schema(connection)
                upsert_countries(connection, catalog)
            break
        except psycopg.Error:
            logger.exception("PostgreSQL no está disponible al iniciar la API; reintento en 5 s")
            import asyncio
            await asyncio.sleep(5)
    yield


app = FastAPI(title="API del Observatorio Sísmico", version="1.0.0", lifespan=lifespan)
origins = [origin.strip() for origin in os.getenv("CORS_ORIGINS", "http://localhost:4200").split(",") if origin.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=False,
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["ngrok-skip-browser-warning", "content-type"],
)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _milliseconds(value: Optional[datetime]) -> Optional[int]:
    return int(value.timestamp() * 1000) if value else None


def _base_filters(
    mode: str,
    start: datetime,
    end: datetime,
    min_mag: Optional[float],
    country_id: Optional[str],
    alias: str = "",
    include_test_cases: bool = False,
):
    prefix = f"{alias}." if alias else ""
    conditions = [f"{prefix}mode = %s", f"{prefix}event_time >= %s", f"{prefix}event_time < %s"]
    if mode == "replay" and not include_test_cases:
        conditions.append(f"{prefix}test_case = false")
    params: list[object] = [mode, start, end]
    if min_mag is not None:
        conditions.append(f"{prefix}magnitude >= %s")
        params.append(min_mag)
    if country_id == "__none__":
        conditions.append(f"{prefix}country_id IS NULL")
    elif country_id is not None:
        conditions.append(f"{prefix}country_id = %s")
        params.append(country_id)
    return " AND ".join(conditions), params


def _latest_ingestion(connection: psycopg.Connection) -> Optional[int]:
    row = connection.execute(
        "SELECT completed_at FROM ingestion_runs WHERE source = 'usgs_feed' AND status = 'success' ORDER BY completed_at DESC LIMIT 1"
    ).fetchone()
    return _milliseconds(row[0]) if row else None


def _open_database() -> psycopg.Connection:
    try:
        return psycopg.connect(database_url(), row_factory=dict_row)
    except psycopg.Error as error:
        logger.exception("No se pudo conectar a PostgreSQL")
        raise HTTPException(status_code=503, detail="Almacenamiento temporalmente no disponible") from error


@app.get("/api/salud")
def health():
    with _open_database() as connection:
        try:
            connection.execute("SELECT 1")
            latest = _latest_ingestion(connection)
            return {
                "status": "ok",
                "generated": int(datetime.now(timezone.utc).timestamp() * 1000),
                "last_ingestion": latest,
            }
        except psycopg.Error as error:
            logger.exception("Falló la comprobación de salud")
            raise HTTPException(status_code=503, detail="Base de datos no disponible") from error


@app.get("/api/paises")
def countries():
    with _open_database() as connection:
        try:
            rows = connection.execute(
                """
                SELECT country_id, name_es, admin_name, continent, subregion
                FROM countries ORDER BY COALESCE(name_es, admin_name, country_id)
                """
            ).fetchall()
            return {"countries": rows}
        except psycopg.Error as error:
            logger.exception("No se pudo consultar el catálogo de países")
            raise HTTPException(status_code=503, detail="No se pudo consultar el catálogo") from error


@app.get("/api/eventos")
def events(
    start: Optional[datetime] = Query(default=None, description="Inicio ISO8601 inclusivo; por defecto, últimas 24 horas"),
    end: Optional[datetime] = Query(default=None, description="Fin ISO8601 exclusivo; por defecto, ahora"),
    min_mag: Optional[float] = Query(default=None, ge=-10, le=10),
    country_id: Optional[str] = Query(default=None, min_length=1, max_length=16),
    mode: Literal["live", "replay"] = "live",
    limit: int = Query(default=5000, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
):
    end_at = _utc(end) if end else datetime.now(timezone.utc)
    start_at = _utc(start) if start else end_at - timedelta(hours=24)
    if start_at >= end_at:
        raise HTTPException(status_code=422, detail="start debe ser anterior a end")
    where, params = _base_filters(mode, start_at, end_at, min_mag, country_id, include_test_cases=True)
    row_where, row_params = _base_filters(mode, start_at, end_at, min_mag, country_id, "e", include_test_cases=True)
    with _open_database() as connection:
        try:
            total = connection.execute(f"SELECT count(*) FROM events_current WHERE {where}", params).fetchone()["count"]
            rows = connection.execute(
                f"""
                SELECT e.event_id, e.event_time, e.updated_ms, e.magnitude, e.magnitude_type, e.place, e.test_case,
                       e.longitude, e.latitude, e.depth_km, e.country_id, c.name_es, c.admin_name
                FROM events_current e
                LEFT JOIN countries c ON c.country_id = e.country_id
                WHERE {row_where}
                ORDER BY e.event_time DESC, e.event_id
                LIMIT %s OFFSET %s
                """,
                [*row_params, limit, offset],
            ).fetchall()
            features = []
            for row in rows:
                coordinates = [row["longitude"], row["latitude"]]
                if row["depth_km"] is not None:
                    coordinates.append(row["depth_km"])
                features.append({
                    "type": "Feature",
                    "id": row["event_id"],
                    "geometry": {"type": "Point", "coordinates": coordinates},
                    "properties": {
                        "mag": row["magnitude"],
                        "magType": row["magnitude_type"],
                        "place": row["place"],
                        "time": _milliseconds(row["event_time"]),
                        "updated": row["updated_ms"],
                        "country_id": row["country_id"],
                        "country_name": row["name_es"] or row["admin_name"],
                        "test_case": row["test_case"],
                    },
                })
            generated = datetime.now(timezone.utc)
            return {
                "type": "FeatureCollection",
                "metadata": {
                    "generated": _milliseconds(generated),
                    "last_ingestion": _latest_ingestion(connection),
                    "mode": mode,
                    "count": len(features),
                    "total": total,
                    "limit": limit,
                    "offset": offset,
                    "truncated": offset + len(features) < total,
                    "period": {"start": _milliseconds(start_at), "end": _milliseconds(end_at)},
                    "filters": {"min_mag": min_mag, "country_id": country_id},
                },
                "features": features,
            }
        except psycopg.Error as error:
            logger.exception("Falló la consulta de eventos")
            raise HTTPException(status_code=503, detail="No se pudieron consultar los eventos") from error


@app.get("/api/estadisticas")
def statistics(
    start: Optional[datetime] = Query(default=None),
    end: Optional[datetime] = Query(default=None),
    min_mag: Optional[float] = Query(default=None, ge=-10, le=10),
    country_id: Optional[str] = Query(default=None, min_length=1, max_length=16),
    mode: Literal["live", "replay"] = "live",
):
    end_at = _utc(end) if end else datetime.now(timezone.utc)
    start_at = _utc(start) if start else end_at - timedelta(hours=24)
    if start_at >= end_at:
        raise HTTPException(status_code=422, detail="start debe ser anterior a end")
    where, params = _base_filters(mode, start_at, end_at, min_mag, country_id)
    row_where, row_params = _base_filters(mode, start_at, end_at, min_mag, country_id, "e")
    quality_where, quality_params = _base_filters(mode, start_at, end_at, None, country_id)
    with _open_database() as connection:
        try:
            hourly_rows = connection.execute(
                f"""
                SELECT date_trunc('hour', event_time AT TIME ZONE 'UTC') AS hour_utc, count(*) AS count
                FROM events_current WHERE {where}
                GROUP BY 1 ORDER BY 1
                """,
                params,
            ).fetchall()
            country_rows = connection.execute(
                f"""
                SELECT e.country_id, COALESCE(c.name_es, c.admin_name, 'Sin país asignado') AS country_name,
                       count(*) AS count
                FROM events_current e LEFT JOIN countries c ON c.country_id = e.country_id
                WHERE {row_where}
                GROUP BY e.country_id, c.name_es, c.admin_name ORDER BY count DESC, country_name
                """,
                row_params,
            ).fetchall()
            magnitude_rows = connection.execute(
                f"""
                SELECT CASE
                    WHEN magnitude IS NULL THEN 'Sin valor'
                    WHEN magnitude < 2 THEN '<2'
                    WHEN magnitude < 3 THEN '2–2.99'
                    WHEN magnitude < 4 THEN '3–3.99'
                    WHEN magnitude < 5 THEN '4–4.99'
                    WHEN magnitude < 6 THEN '5–5.99'
                    WHEN magnitude < 7 THEN '6–6.99'
                    ELSE '7+'
                END AS bucket, count(*) AS count
                FROM events_current WHERE {where} GROUP BY 1
                """,
                params,
            ).fetchall()
            depth_rows = connection.execute(
                f"""
                SELECT CASE
                    WHEN depth_km IS NULL THEN 'Sin valor'
                    WHEN depth_km < 0 THEN '<0'
                    WHEN depth_km < 70 THEN '0–69.99 km'
                    WHEN depth_km < 300 THEN '70–299.99 km'
                    ELSE '300+ km'
                END AS bucket, count(*) AS count
                FROM events_current WHERE {where} GROUP BY 1
                """,
                params,
            ).fetchall()
            recent = connection.execute(
                f"SELECT count(*) FROM events_current WHERE {where}", params
            ).fetchone()["count"]
            quality_counts = connection.execute(
                f"""
                SELECT count(*) AS population,
                       count(*) FILTER (WHERE magnitude IS NULL) AS without_magnitude
                FROM events_current WHERE {quality_where}
                """,
                quality_params,
            ).fetchone()
            historical_conditions = [
                "mode = 'historical'", "test_case = false", "event_time >= %s", "event_time < %s"
            ]
            historical_params: list[object] = [
                datetime(2025, 1, 1, tzinfo=timezone.utc),
                datetime(2026, 1, 1, tzinfo=timezone.utc),
            ]
            if min_mag is not None:
                historical_conditions.append("magnitude >= %s")
                historical_params.append(min_mag)
            if country_id == "__none__":
                historical_conditions.append("country_id IS NULL")
            elif country_id is not None:
                historical_conditions.append("country_id = %s")
                historical_params.append(country_id)
            historical = connection.execute(
                f"SELECT count(*) FROM events_current WHERE {' AND '.join(historical_conditions)}",
                historical_params,
            ).fetchone()["count"]
            avg_daily = historical / 365
            hourly_counts = {
                row["hour_utc"].replace(tzinfo=timezone.utc): row["count"]
                for row in hourly_rows
            }
            hour_cursor = start_at.replace(minute=0, second=0, microsecond=0)
            hourly_windows = []
            while hour_cursor < end_at:
                hourly_windows.append({
                    "hour": hour_cursor.isoformat(),
                    "count": hourly_counts.get(hour_cursor, 0),
                })
                hour_cursor += timedelta(hours=1)

            generated = datetime.now(timezone.utc)
            return {
                "generated": _milliseconds(generated),
                "mode": mode,
                "period": {"start": _milliseconds(start_at), "end": _milliseconds(end_at)},
                "filters": {"min_mag": min_mag, "country_id": country_id},
                "last_ingestion": _latest_ingestion(connection),
                "counts": {
                    "events": recent,
                    "population_before_min_mag": quality_counts["population"],
                    "without_magnitude": quality_counts["without_magnitude"],
                    "without_depth": sum(1 for row in depth_rows if row["bucket"] == "Sin valor"),
                    "without_country": sum(1 for row in country_rows if row["country_id"] is None),
                },
                "hourly_utc": hourly_windows,
                "by_country": country_rows,
                "magnitude_distribution": magnitude_rows,
                "depth_distribution": depth_rows,
                "historical_comparison": {
                    "historical_year": 2025,
                    "historical_total": historical,
                    "historical_days": 365,
                    "historical_average_per_day": avg_daily,
                    "recent_total": recent,
                    "recent_period_hours": (end_at - start_at).total_seconds() / 3600,
                    "recent_per_day": recent / ((end_at - start_at).total_seconds() / 86400),
                    "relative_to_historical_daily_average": (recent / ((end_at - start_at).total_seconds() / 86400) / avg_daily) if avg_daily else None,
                    "note": "La media incluye los 365 días de 2025, también los días con cero eventos; el periodo reciente puede estar incompleto por retrasos de publicación.",
                },
            }
        except psycopg.Error as error:
            logger.exception("Falló la consulta de estadísticas")
            raise HTTPException(status_code=503, detail="No se pudieron calcular las estadísticas") from error
