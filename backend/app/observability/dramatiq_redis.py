"""Payload-blind queue metrics for Dramatiq's reviewed Redis contract.

Dramatiq does not publish a queue-observability API.  Version 2.2 stores
message identifiers in lists, worker ownership in acknowledgement sets and
dead letters in sorted sets.  This adapter deliberately reads only cardinality
commands; it never fetches message ids or message bodies.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import version
from typing import Any

from dramatiq.brokers.redis import RedisBroker

SUPPORTED_DRAMATIQ_SERIES = (2, 2)


class QueueMetricsCompatibilityError(RuntimeError):
    """The installed broker does not match the reviewed metrics contract."""


@dataclass(frozen=True, slots=True)
class QueueStateCounts:
    ready: int
    delayed: int
    in_flight: int
    dead: int


@dataclass(frozen=True, slots=True)
class BrokerResourceMetrics:
    used_memory_bytes: int
    maxmemory_bytes: int
    oom_error_replies_total: int


class DramatiqRedisMetrics:
    """Read closed queue states from a compatible production Redis broker."""

    def __init__(self, broker: RedisBroker) -> None:
        installed = version("dramatiq")
        try:
            series = tuple(int(part) for part in installed.split(".")[:2])
        except ValueError as exc:  # pragma: no cover - defensive package metadata guard
            raise QueueMetricsCompatibilityError(
                f"unparseable Dramatiq version {installed!r}"
            ) from exc
        if series != SUPPORTED_DRAMATIQ_SERIES:
            raise QueueMetricsCompatibilityError(
                f"Dramatiq {installed} is unsupported; expected the 2.2.x Redis contract"
            )
        client: Any = vars(broker).get("client")
        if not broker.namespace or not hasattr(client, "pipeline"):
            raise QueueMetricsCompatibilityError(
                "RedisBroker is missing the reviewed namespace/client contract"
            )
        self._client = client
        self._namespace = broker.namespace

    def _ack_keys(self, queue: str) -> list[bytes | str]:
        pattern = f"{self._namespace}:__acks__.*.{queue}"
        return list(self._client.scan_iter(match=pattern, count=100))

    def queue_counts(self, queues: tuple[str, ...]) -> dict[str, QueueStateCounts]:
        """Count ready, delayed, owned and dead messages without reading payloads."""
        ack_keys = {queue: self._ack_keys(queue) for queue in queues}
        delayed_ack_keys = {queue: self._ack_keys(f"{queue}.DQ") for queue in queues}
        pipeline = self._client.pipeline(transaction=False)
        for queue in queues:
            prefix = f"{self._namespace}:{queue}"
            pipeline.llen(prefix)
            pipeline.llen(f"{prefix}.DQ")
            for key in ack_keys[queue]:
                pipeline.scard(key)
            for key in delayed_ack_keys[queue]:
                pipeline.scard(key)
            pipeline.zcard(f"{prefix}.XQ")
        values = iter(pipeline.execute())
        result: dict[str, QueueStateCounts] = {}
        for queue in queues:
            ready = int(next(values))
            delayed = int(next(values))
            in_flight = sum(int(next(values)) for _ in ack_keys[queue])
            # DelayQueue consumers own future-ETA messages in .DQ ack sets;
            # they remain delayed rather than business-work in flight.
            delayed += sum(int(next(values)) for _ in delayed_ack_keys[queue])
            dead = int(next(values))
            result[queue] = QueueStateCounts(
                ready=ready,
                delayed=delayed,
                in_flight=in_flight,
                dead=dead,
            )
        return result

    def resource_metrics(self) -> BrokerResourceMetrics:
        """Return capacity and OOM rejection signals from Redis INFO only."""
        memory = self._client.info("memory")
        errorstats = self._client.info("errorstats")
        oom = errorstats.get("errorstat_OOM", {})
        return BrokerResourceMetrics(
            used_memory_bytes=int(memory.get("used_memory", 0)),
            maxmemory_bytes=int(memory.get("maxmemory", 0)),
            oom_error_replies_total=int(oom.get("count", 0)),
        )


def metrics_adapter_for_broker(broker: object) -> DramatiqRedisMetrics:
    """Validate and adapt the process broker, failing closed on other backends."""
    if not isinstance(broker, RedisBroker):
        raise QueueMetricsCompatibilityError("queue metrics require RedisBroker")
    return DramatiqRedisMetrics(broker)
