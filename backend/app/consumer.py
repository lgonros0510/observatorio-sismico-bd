import asyncio
import json
import logging
import os

import psycopg
from aiokafka import AIOKafkaConsumer

from app.catalog import CountryCatalog
from app.storage import database_url, ensure_schema, quarantine, store_message, upsert_countries


logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("observatorio.consumer")


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Número no JSON permitido: {value}")


async def run() -> None:
    catalog = CountryCatalog()
    while True:
        try:
            with psycopg.connect(database_url()) as connection:
                ensure_schema(connection)
                upsert_countries(connection, catalog)
            break
        except Exception:
            logger.exception("No se pudo inicializar PostgreSQL; reintento en 5 s")
            await asyncio.sleep(5)

    topics = [
        os.getenv("KAFKA_LIVE_TOPIC", "earthquakes.live"),
        os.getenv("KAFKA_REPLAY_TOPIC", "earthquakes.replay"),
    ]
    consumer = AIOKafkaConsumer(
        *topics,
        bootstrap_servers=os.getenv("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092"),
        group_id=os.getenv("KAFKA_GROUP_ID", "observatorio-consumer"),
        enable_auto_commit=False,
        auto_offset_reset="earliest",
    )
    while True:
        try:
            await consumer.start()
            break
        except Exception:
            logger.exception("Kafka no está disponible; reintento en 5 s")
            await asyncio.sleep(5)

    try:
        async for message in consumer:
            try:
                mode = "live" if message.topic == topics[0] else "replay"
                try:
                    decoded = message.value.decode("utf-8")
                except UnicodeDecodeError:
                    decoded = message.value.decode("utf-8", errors="replace")
                    with psycopg.connect(database_url()) as connection:
                        quarantine(connection, mode, "kafka", {"raw": decoded}, "Mensaje Kafka no es UTF-8 válido")
                    await consumer.commit()
                    logger.warning("Mensaje no UTF-8 enviado a cuarentena topic=%s offset=%d", message.topic, message.offset)
                    continue
                try:
                    envelope = json.loads(decoded, parse_constant=_reject_json_constant)
                except (json.JSONDecodeError, ValueError):
                    with psycopg.connect(database_url()) as connection:
                        quarantine(connection, mode, "kafka", {"raw": decoded}, "JSON de mensaje inválido")
                    await consumer.commit()
                    logger.warning("Mensaje JSON inválido enviado a cuarentena topic=%s offset=%d", message.topic, message.offset)
                    continue
                if not isinstance(envelope, dict):
                    with psycopg.connect(database_url()) as connection:
                        quarantine(connection, mode, "kafka", {"raw": decoded}, "El mensaje JSON no es un objeto")
                    await consumer.commit()
                    logger.warning("Raíz JSON inválida enviada a cuarentena topic=%s offset=%d", message.topic, message.offset)
                    continue
                with psycopg.connect(database_url()) as connection:
                    result = store_message(connection, envelope, catalog)
                await consumer.commit()
                event = envelope.get("event")
                event_id = event.get("id") if isinstance(event, dict) else None
                logger.info("%s: event_id=%s mode=%s", result, event_id, envelope.get("mode"))
            except Exception:
                logger.exception("Error procesando mensaje topic=%s offset=%d; no se confirma el offset", message.topic, message.offset)
                raise
    finally:
        await consumer.stop()


if __name__ == "__main__":
    asyncio.run(run())
