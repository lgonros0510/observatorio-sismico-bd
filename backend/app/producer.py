import asyncio
import hashlib
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import asyncpg
import httpx
from aiokafka import AIOKafkaProducer


logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("observatorio.producer")


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Número no JSON permitido: {value}")


def _updated(value: Any) -> Optional[int]:
    properties = value.get("properties") if isinstance(value, dict) else None
    result = properties.get("updated") if isinstance(properties, dict) else None
    return result if isinstance(result, int) and not isinstance(result, bool) else None


async def _database() -> asyncpg.Pool:
    database_url = os.environ["DATABASE_URL"]
    while True:
        try:
            return await asyncpg.create_pool(database_url, min_size=1, max_size=4)
        except Exception:
            logger.exception("PostgreSQL no está disponible; reintento en 5 s")
            await asyncio.sleep(5)


async def _fetch_feed(client: httpx.AsyncClient, url: str, timeout_seconds: float) -> tuple[dict[str, Any], int, int]:
    last_error: Optional[Exception] = None
    for attempt in range(5):
        started = time.monotonic()
        try:
            response = await client.get(url, timeout=timeout_seconds)
            response.raise_for_status()
            payload = json.loads(response.content, parse_constant=_reject_json_constant)
            if not isinstance(payload, dict) or payload.get("type") != "FeatureCollection" or not isinstance(payload.get("features"), list):
                raise ValueError("Respuesta USGS no es un GeoJSON FeatureCollection")
            return payload, response.status_code, int((time.monotonic() - started) * 1000)
        except (httpx.HTTPError, ValueError) as error:
            last_error = error
            delay = min(60, 2 ** attempt)
            logger.warning("USGS falló (intento %d/5): %s; reintento en %d s", attempt + 1, error, delay)
            if attempt < 4:
                await asyncio.sleep(delay)
    raise RuntimeError(f"No se pudo consultar USGS tras cinco intentos: {last_error}") from last_error


async def run() -> None:
    topic = os.getenv("KAFKA_LIVE_TOPIC", "earthquakes.live")
    feed_url = os.getenv("USGS_FEED_URL", "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_day.geojson")
    poll_seconds = max(10, int(os.getenv("POLL_SECONDS", "60")))
    timeout_seconds = float(os.getenv("HTTP_TIMEOUT_SECONDS", "20"))
    pool = await _database()
    producer = AIOKafkaProducer(
        bootstrap_servers=os.getenv("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092"),
        enable_idempotence=True,
        acks="all",
    )
    while True:
        try:
            await producer.start()
            break
        except Exception:
            logger.exception("Kafka no está disponible; reintento en 5 s")
            await asyncio.sleep(5)

    try:
        async with httpx.AsyncClient(headers={"User-Agent": "observatorio-sismico/1.0"}) as client:
            while True:
                requested_at = datetime.now(timezone.utc)
                try:
                    payload, status_code, response_ms = await _fetch_feed(client, feed_url, timeout_seconds)
                    features = payload["features"]
                    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                    digest = hashlib.sha256(b"usgs_feed\0" + serialized.encode("utf-8")).hexdigest()
                    fetched_at = datetime.now(timezone.utc)
                    async with pool.acquire() as connection:
                        async with connection.transaction():
                            retention = int(os.getenv("RAW_RETENTION_DAYS", "30"))
                            await connection.execute(
                                """
                                INSERT INTO raw_snapshots (sha256, source, fetched_at, expires_at, query, payload)
                                VALUES ($1, 'usgs_feed', $2, $3, $4::jsonb, $5::jsonb)
                                ON CONFLICT (sha256) DO UPDATE SET expires_at = GREATEST(raw_snapshots.expires_at, EXCLUDED.expires_at)
                                """,
                                digest, fetched_at, fetched_at + timedelta(days=retention),
                                json.dumps({"url": feed_url}), serialized,
                            )
                            await connection.execute("DELETE FROM raw_snapshots WHERE expires_at < $1", fetched_at)
                            await connection.execute(
                                """
                                INSERT INTO ingestion_runs (source, mode, requested_at, completed_at, status, record_count,
                                                           http_status, response_ms, query)
                                VALUES ('usgs_feed', 'live', $1, $2, 'success', $3, $4, $5, $6::jsonb)
                                """,
                                requested_at, fetched_at, len(features), status_code, response_ms,
                                json.dumps({"url": feed_url, "sha256": digest}),
                            )

                    ids = [
                        feature.get("id")
                        for feature in features
                        if isinstance(feature, dict) and isinstance(feature.get("id"), str)
                    ]
                    async with pool.acquire() as connection:
                        known = await connection.fetch(
                            "SELECT event_id, updated_ms FROM published_state WHERE mode = 'live' AND event_id = ANY($1::text[])",
                            list(set(ids)),
                        )
                    published = {record["event_id"]: record["updated_ms"] for record in known}
                    sent = 0
                    for feature in features:
                        event_id = feature.get("id") if isinstance(feature, dict) else None
                        updated_ms = _updated(feature)
                        if isinstance(event_id, str) and updated_ms is not None and updated_ms <= published.get(event_id, -1):
                            continue
                        message = {
                            "source": "usgs_feed",
                            "ingested_at": datetime.now(timezone.utc).isoformat(),
                            "mode": "live",
                            "event": feature,
                        }
                        key = event_id.encode("utf-8") if isinstance(event_id, str) else f"invalid-{sent}".encode("utf-8")
                        await producer.send_and_wait(topic, json.dumps(message, separators=(",", ":")).encode("utf-8"), key=key)
                        sent += 1
                        if isinstance(event_id, str) and updated_ms is not None:
                            async with pool.acquire() as connection:
                                async with connection.transaction():
                                    await connection.execute(
                                        """
                                        INSERT INTO published_state (mode, event_id, updated_ms)
                                        VALUES ('live', $1, $2)
                                        ON CONFLICT (mode, event_id) DO UPDATE SET updated_ms = EXCLUDED.updated_ms
                                        WHERE published_state.updated_ms < EXCLUDED.updated_ms
                                        """,
                                        event_id, updated_ms,
                                    )
                            published[event_id] = updated_ms
                    logger.info("Consulta correcta: %d eventos en feed, %d novedades/versiones publicadas", len(features), sent)
                except Exception:
                    logger.exception("Falló el ciclo de extracción; se conserva el estado anterior")
                    try:
                        async with pool.acquire() as connection:
                            await connection.execute(
                                """
                                INSERT INTO ingestion_runs (source, mode, requested_at, completed_at, status, error, query)
                                VALUES ('usgs_feed', 'live', $1, $2, 'error', $3, $4::jsonb)
                                """,
                                requested_at, datetime.now(timezone.utc), "Error durante extracción/publicación; consultar logs", json.dumps({"url": feed_url}),
                            )
                    except Exception:
                        logger.exception("No se pudo registrar el fallo de ingesta")
                await asyncio.sleep(poll_seconds)
    finally:
        await producer.stop()
        await pool.close()


if __name__ == "__main__":
    asyncio.run(run())
