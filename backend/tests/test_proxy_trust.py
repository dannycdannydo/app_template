"""Trusted-proxy client-IP resolution tests (plan P9, BP §30).

The production API runs behind Caddy with uvicorn's proxy-header handling
enabled and ``--forwarded-allow-ips`` set to the private edge subnet only.
These tests drive uvicorn's real ``ProxyHeadersMiddleware`` (the exact code the
deployment runs) with scopes that mimic Caddy and a hostile browser, proving:

- an untrusted direct peer cannot influence the resolved client IP; and
- from a trusted Caddy peer, the *appended* client address is used even when
  the browser supplies a spoofed ``X-Forwarded-For`` chain.

The wildcard case is asserted as a spoofing hazard so the deployment contract
can never quietly switch to ``--forwarded-allow-ips=*``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.core.rate_limit import RedisRateLimiter
from app.main import _rate_limit_middleware  # pyright: ignore[reportPrivateUsage]

#: The private edge network the production compose file configures.
EDGE_SUBNET = "172.30.0.0/24"
TRUSTED_PROXY = "172.30.0.5"
UNTRUSTED_PEER = "203.0.113.9"
REAL_CLIENT = "198.51.100.7"


def _scope(*, peer: str, forwarded_for: str | None) -> dict[str, Any]:
    headers: list[tuple[bytes, bytes]] = []
    if forwarded_for is not None:
        headers.append((b"x-forwarded-for", forwarded_for.encode("latin1")))
    return {
        "type": "http",
        "scheme": "http",
        "client": (peer, 51234),
        "headers": headers,
    }


async def _resolve(trusted: str | list[str], *, peer: str, forwarded_for: str | None) -> Any:
    captured: dict[str, Any] = {}

    async def app(scope: Any, receive: Any, send: Any) -> None:
        captured["client"] = scope.get("client")

    middleware = ProxyHeadersMiddleware(app, trusted_hosts=trusted)
    scope = cast(Any, _scope(peer=peer, forwarded_for=forwarded_for))
    await cast(Any, middleware)(scope, None, None)
    return captured["client"]


async def test_untrusted_peer_cannot_spoof_forwarded_for() -> None:
    client = await _resolve(EDGE_SUBNET, peer=UNTRUSTED_PEER, forwarded_for=REAL_CLIENT)
    assert client == (UNTRUSTED_PEER, 51234)


async def test_trusted_peer_resolves_the_appended_client() -> None:
    client = await _resolve(EDGE_SUBNET, peer=TRUSTED_PROXY, forwarded_for=REAL_CLIENT)
    assert client == (REAL_CLIENT, 0)


async def test_spoofed_chain_is_discarded_in_favour_of_the_real_client() -> None:
    """Caddy appends the real client to any browser-supplied chain."""
    client = await _resolve(
        EDGE_SUBNET,
        peer=TRUSTED_PROXY,
        forwarded_for=f"10.0.0.1, {REAL_CLIENT}",
    )
    assert client == (REAL_CLIENT, 0)


async def test_intermediate_trusted_proxy_is_skipped() -> None:
    client = await _resolve(
        EDGE_SUBNET,
        peer=TRUSTED_PROXY,
        forwarded_for=f"{REAL_CLIENT}, 172.30.0.9",
    )
    assert client == (REAL_CLIENT, 0)


async def test_wildcard_trust_would_accept_the_spoofed_first_value() -> None:
    """Documents why the deployment must never use ``--forwarded-allow-ips=*``.

    With a wildcard, uvicorn trusts the left-most (browser-supplied) value, so
    a client can trivially forge its address and evade per-client limits.
    """
    client = await _resolve("*", peer=TRUSTED_PROXY, forwarded_for=f"10.0.0.1, {REAL_CLIENT}")
    assert client == ("10.0.0.1", 0)


@pytest.mark.parametrize("forwarded_for", [None, ""])
async def test_trusted_peer_without_a_usable_header_keeps_the_peer(
    forwarded_for: str | None,
) -> None:
    client = await _resolve(EDGE_SUBNET, peer=TRUSTED_PROXY, forwarded_for=forwarded_for)
    assert client == (TRUSTED_PROXY, 51234)


# --- Plan P9 item 2: separate real users get separate application quotas ------


class _RecordingLimiter:
    """Captures the quota key each application request is counted against."""

    def __init__(self) -> None:
        self.keys: list[str] = []
        self.blocked: set[str] = set()

    async def enforce(self, *, key: str, limit: int, window_seconds: int) -> None:
        self.keys.append(key)
        if key in self.blocked:
            from app.core.exceptions import RateLimitExceeded

            raise RateLimitExceeded(headers={"Retry-After": str(window_seconds)})


class _FakeRequest:
    def __init__(self, *, path: str, host: str) -> None:
        self.url = SimpleNamespace(path=path)
        self.client = SimpleNamespace(host=host)


async def _next(request: Any) -> Any:
    from starlette.responses import Response

    return Response(status_code=200)


async def test_rate_limiter_keys_each_resolved_client_separately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The application limiter keys on the resolved client IP, so two trusted
    real clients behind Caddy are never collapsed into one quota."""
    limiter = _RecordingLimiter()
    monkeypatch.setattr("app.main.get_rate_limiter", lambda: limiter)

    for peer, forwarded_for in (
        (TRUSTED_PROXY, "198.51.100.7"),
        (TRUSTED_PROXY, "198.51.100.8"),
    ):
        client = await _resolve(EDGE_SUBNET, peer=peer, forwarded_for=forwarded_for)
        request = _FakeRequest(path="/api/v1/users/me", host=client[0])
        await _rate_limit_middleware(cast(Any, request), cast(Any, _next))

    assert limiter.keys == ["api:ip:198.51.100.7", "api:ip:198.51.100.8"]
    assert len(set(limiter.keys)) == 2


async def test_rate_limiter_does_not_share_quota_between_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A throttled client does not consume or exhaust another client's quota:
    the two resolved addresses map to independent keys."""
    limiter = _RecordingLimiter()
    limiter.blocked.add("api:ip:198.51.100.7")
    monkeypatch.setattr("app.main.get_rate_limiter", lambda: limiter)

    blocked = await _rate_limit_middleware(
        cast(Any, _FakeRequest(path="/api/v1/users/me", host="198.51.100.7")),
        cast(Any, _next),
    )
    assert blocked.status_code == 429
    allowed = await _rate_limit_middleware(
        cast(Any, _FakeRequest(path="/api/v1/users/me", host="198.51.100.8")),
        cast(Any, _next),
    )
    assert allowed.status_code == 200


class _IncrementFakeRedis:
    """Minimal Redis stand-in: INCR per key with the fixed-window EXPIRE."""

    def __init__(self) -> None:
        self.counters: dict[str, int] = {}

    async def eval(self, script: str, num_keys: int, key: str, window: str) -> int:
        self.counters[key] = self.counters.get(key, 0) + 1
        return self.counters[key]


async def test_redis_rate_limiter_counts_each_key_independently() -> None:
    """The Redis limiter (the production control) increments a separate counter
    per key, so distinct client IPs have distinct quotas."""
    limiter = RedisRateLimiter("redis://unused")
    fake = _IncrementFakeRedis()
    limiter._redis = fake  # type: ignore[assignment]  # pyright: ignore[reportPrivateUsage]

    await limiter.enforce(key="api:ip:198.51.100.7", limit=2, window_seconds=60)
    await limiter.enforce(key="api:ip:198.51.100.7", limit=2, window_seconds=60)
    # The second client's first request is counted against its own key.
    await limiter.enforce(key="api:ip:198.51.100.8", limit=1, window_seconds=60)

    assert fake.counters == {
        "api:ip:198.51.100.7": 2,
        "api:ip:198.51.100.8": 1,
    }
