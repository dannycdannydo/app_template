"""Real-Redis contract tests for payload-blind Dramatiq queue metrics."""

from __future__ import annotations

import os
import uuid
from collections.abc import Generator
from typing import Any, cast

import pytest
from dramatiq import actor
from dramatiq.brokers.redis import RedisBroker
from redis import Redis
from redis.exceptions import RedisError

from app.observability.dramatiq_redis import DramatiqRedisMetrics

_REDIS_URL = os.environ.get(
    "BROKER_REDIS_URL", os.environ.get("REDIS_URL", "redis://localhost:6379/0")
)


@pytest.fixture
def real_redis_broker() -> Generator[RedisBroker]:
    namespace = f"metrics-test-{uuid.uuid4().hex}"
    client = cast(Any, Redis).from_url(_REDIS_URL)
    try:
        client.ping()
    except RedisError:
        pytest.skip("no reachable Redis at BROKER_REDIS_URL/REDIS_URL")
    broker = RedisBroker(url=_REDIS_URL, namespace=namespace)
    try:
        yield broker
    finally:
        for key in client.scan_iter(match=f"{namespace}:*"):
            client.delete(key)
        client.close()


def test_locked_dramatiq_redis_layout_reports_all_closed_states(
    real_redis_broker: RedisBroker,
) -> None:
    client: Any = vars(real_redis_broker)["client"]
    namespace = real_redis_broker.namespace

    def do_nothing() -> None:
        return None

    test_actor: Any = cast(Any, actor)(
        do_nothing,
        actor_name=f"metrics_actor_{uuid.uuid4().hex}",
        queue_name="ai",
        broker=real_redis_broker,
    )
    test_actor.send()
    test_actor.send_with_options(delay=60_000)

    # In-flight and dead states require a consumer to create them. Seed only
    # those states directly; ready and delayed must exercise Dramatiq's real
    # publication path so a future key-layout change fails this contract test.
    client.sadd(f"{namespace}:__acks__.worker-1.ai", "owned-id")
    client.zadd(f"{namespace}:ai.XQ", {"dead-id": 1})

    counts = DramatiqRedisMetrics(real_redis_broker).queue_counts(("ai",))["ai"]

    assert counts.ready == 1
    assert counts.delayed == 1
    assert counts.in_flight == 1
    assert counts.dead == 1


def test_locked_dramatiq_redis_adapter_reads_capacity_without_payloads(
    real_redis_broker: RedisBroker,
) -> None:
    resources = DramatiqRedisMetrics(real_redis_broker).resource_metrics()

    assert resources.used_memory_bytes > 0
    assert resources.maxmemory_bytes >= 0
    assert resources.oom_error_replies_total >= 0
