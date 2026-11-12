"""Kafka producer for the network-config-jobs topic."""
import json
import logging
import threading

from .config import settings

log = logging.getLogger("netauto.kafka")


class KafkaDispatchError(Exception):
    pass


class JobProducer:
    def __init__(self) -> None:
        self._producer = None
        self._lock = threading.Lock()

    def _get(self):
        if self._producer is None:
            from confluent_kafka import Producer
            self._producer = Producer({
                "bootstrap.servers": settings.kafka_bootstrap,
                "client.id": "netauto-backend",
                "acks": "all",
                "enable.idempotence": True,
                "message.timeout.ms": 15000,
            })
        return self._producer

    def send(self, message: dict) -> dict:
        """Publish synchronously and return {topic, partition, offset}."""
        result: dict = {}

        def on_delivery(err, msg):
            if err is not None:
                result["error"] = str(err)
            else:
                result.update(topic=msg.topic(), partition=msg.partition(), offset=msg.offset())

        with self._lock:
            p = self._get()
            p.produce(settings.kafka_topic, key=message["device_id"].encode(),
                      value=json.dumps(message).encode(), on_delivery=on_delivery,
                      headers={"job_type": message["job_type"], "schema": message["schema"]})
            remaining = p.flush(20)
        if remaining or "error" in result or "offset" not in result:
            raise KafkaDispatchError(result.get("error") or "Kafka delivery timed out")
        log.info("dispatched %s to %s", message["job_id"], result)
        return result


class InMemoryProducer:
    """Used by tests (KAFKA_ENABLED=false): keeps messages in a list."""

    def __init__(self) -> None:
        self.messages: list = []

    def send(self, message: dict) -> dict:
        self.messages.append(message)
        return {"topic": settings.kafka_topic, "partition": 0, "offset": len(self.messages) - 1}


producer = JobProducer() if settings.kafka_enabled else InMemoryProducer()


def set_producer(p) -> None:
    global producer
    producer = p
