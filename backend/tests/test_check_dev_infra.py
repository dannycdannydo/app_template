"""Tests for the host-side Redis development-infrastructure preflight."""

from __future__ import annotations

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from scripts.check_dev_infra import DevInfraError, check_redis, redis_endpoint_label


class FakeRedisProbe:
    def __init__(self, *, result: bool = True, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.closed = False

    def ping(self) -> bool:
        if self.error is not None:
            raise self.error
        return self.result

    def close(self) -> None:
        self.closed = True


def test_check_redis_uses_configured_url_and_closes_client() -> None:
    probe = FakeRedisProbe()
    received_urls: list[str] = []

    def factory(url: str) -> FakeRedisProbe:
        received_urls.append(url)
        return probe

    endpoint = check_redis("redis://localhost:6380/0", probe_factory=factory)

    assert endpoint == "localhost:6380"
    assert received_urls == ["redis://localhost:6380/0"]
    assert probe.closed is True


def test_check_redis_failure_is_actionable_and_never_exposes_credentials() -> None:
    probe = FakeRedisProbe(error=RedisConnectionError("connection refused"))

    with pytest.raises(DevInfraError) as caught:
        check_redis(
            "redis://dev-user:secret-password@localhost:6380/0",
            probe_factory=lambda _url: probe,
        )

    message = str(caught.value)
    assert "localhost:6380" in message
    assert "make dev-down" in message
    assert "CONFIRM_RESET=1 make dev-reset" in message
    assert "secret-password" not in message
    assert probe.closed is True


def test_check_redis_rejects_unsuccessful_ping() -> None:
    probe = FakeRedisProbe(result=False)

    with pytest.raises(DevInfraError, match="unsuccessful PING"):
        check_redis("redis://localhost:6379/0", probe_factory=lambda _url: probe)

    assert probe.closed is True


def test_redis_endpoint_label_formats_ipv6_without_credentials() -> None:
    assert redis_endpoint_label("rediss://user:password@[::1]:6380/0") == "[::1]:6380"


def test_check_redis_reports_malformed_port_without_exposing_url() -> None:
    with pytest.raises(DevInfraError) as caught:
        check_redis("redis://user:secret@localhost:not-a-port/0")

    message = str(caught.value)
    assert "REDIS_URL" in message
    assert ".env" in message
    assert "secret" not in message
