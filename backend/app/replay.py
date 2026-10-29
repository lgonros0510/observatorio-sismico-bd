import argparse
import asyncio
import json
import logging
import os
from datetime import datetime, timezone

import psycopg
from aiokafka import AIOKafkaProducer

from app.storage import database_url


logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("observatorio.replay")


def _envelope(event: dict, demo: bool = False) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "source": "historical_replay",
        "ingested_at": now,
        "replayed_at": now,
        "mode": "replay",
        "event": event,
        "test_case": demo,
    }


async def replay(limit: int, delay: float, include_cases: bool) -> int:
    with psycopg.connect(database_url()) as connection:
        rows = connection.execute(
            """
            SELECT original FROM events_current
            WHERE mode = 'historical'
            ORDER BY event_time, event_id
            LIMIT %s
            """,
            (limit,),
        ).fetchall()
    if not rows:
        raise RuntimeError("No hay histórico cargado; ejecuta primero app.load_historical")

    producer = AIOKafkaProducer(
        bootstrap_servers=os.getenv("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092"),
        enable_idempotence=True,
        acks="all",
    )
    await producer.start()
    topic = os.getenv("KAFKA_REPLAY_TOPIC", "earthquakes.replay")
    sent = 0
    first_event = rows[0][0]
    try:
        for row in rows:
            event = row[0]
            await producer.send_and_wait(
                topic,
                json.dumps(_envelope(event), separators=(",", ":")).encode(),
                key=event["id"].encode(),
            )
            sent += 1
            if delay:
                await asyncio.sleep(delay)
        if include_cases:
            duplicate = json.loads(json.dumps(first_event))
            await producer.send_and_wait(topic, json.dumps(_envelope(duplicate, True)).encode(), key=duplicate["id"].encode())
            sent += 1

            revised = json.loads(json.dumps(first_event))
            revised["properties"]["updated"] = int(revised["properties"]["updated"]) + 1
            if revised["properties"].get("mag") is not None:
                revised["properties"]["mag"] = float(revised["properties"]["mag"]) + 0.1
            await producer.send_and_wait(topic, json.dumps(_envelope(revised, True)).encode(), key=revised["id"].encode())
            sent += 1

            stale = json.loads(json.dumps(first_event))
            await producer.send_and_wait(topic, json.dumps(_envelope(stale, True)).encode(), key=stale["id"].encode())
            sent += 1
        logger.info("Reproducción completa: %d mensajes publicados en %s", sent, topic)
        return sent
    finally:
        await producer.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="Publica el histórico por Kafka en un espacio replay separado")
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--delay", type=float, default=0.05, help="Pausa entre eventos, en segundos")
    parser.add_argument("--include-cases", action="store_true", help="Añade duplicado, revisión y revisión antigua de prueba")
    args = parser.parse_args()
    if args.limit < 1 or args.delay < 0:
        parser.error("--limit debe ser positivo y --delay no puede ser negativo")
    asyncio.run(replay(args.limit, args.delay, args.include_cases))


if __name__ == "__main__":
    main()
