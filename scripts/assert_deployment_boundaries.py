"""Assert the browser/proxy deployment boundaries in a resolved Compose document.

Plan P9: the production edge must trust only Caddy for forwarded client IPs,
the API must trust exactly the private edge network, and the browser-facing
storage origin must reach the CSP. This runs against the JSON emitted by
``docker compose config`` in CI, where the real topology is resolved (compose
substitution included), so a misconfiguration fails the build rather than a
production deployment.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any
from urllib.parse import urlsplit

_APPLICATION_SERVICES = {
    "api",
    "worker",
    "coordinator",
    "redis-broker",
    "redis-rate-limit",
}


def _command(service: dict[str, Any]) -> list[str]:
    command = service.get("command", [])
    if isinstance(command, str):
        return command.split()
    return [str(part) for part in command]


def _network_names(service: dict[str, Any]) -> set[str]:
    return set((service.get("networks") or {}).keys())


def _environment(service: dict[str, Any]) -> dict[str, str]:
    environment = service.get("environment") or {}
    return {str(key): str(value) for key, value in environment.items()}


def parse_public_origin(value: str) -> str:
    """Return the bare ``scheme://host[:port]`` origin, or fail the assertion.

    Plan P9 item 1: the CSP ``connect-src`` must carry an exact origin — no
    wildcard, path, query string (a signed URL would otherwise leak into the
    header), fragment or embedded credentials. The value must be an absolute
    ``https://`` URL with a host, so it can be interpolated into the CSP safely.
    """
    assert value, "the storage origin must be configured"
    assert "*" not in value, f"the storage origin must not contain a wildcard: {value!r}"
    parts = urlsplit(value)
    assert parts.scheme == "https", f"the storage origin must use https: {value!r}"
    assert parts.username is None and parts.password is None, (
        f"the storage origin must not embed credentials: {value!r}"
    )
    assert parts.hostname, f"the storage origin must name a host: {value!r}"
    assert parts.path in ("", "/"), f"the storage origin must not contain a path: {value!r}"
    assert not parts.query, f"the storage origin must not contain a query string: {value!r}"
    assert not parts.fragment, f"the storage origin must not contain a fragment: {value!r}"
    return f"{parts.scheme}://{parts.netloc}"


def _assert_production(document: dict[str, Any]) -> None:
    services = document["services"]
    expected = _APPLICATION_SERVICES | {"caddy"}
    assert set(services) == expected, f"unexpected services: {sorted(set(services) ^ expected)}"

    edge = document["networks"]["edge"]
    subnet = edge["ipam"]["config"][0]["subnet"]

    caddy = services["caddy"]
    assert "edge" in _network_names(caddy)
    assert "backend" not in _network_names(caddy)
    caddy_env = _environment(caddy)
    assert caddy_env.get("STORAGE_PUBLIC_ORIGIN"), "caddy must receive STORAGE_PUBLIC_ORIGIN"
    origin = parse_public_origin(caddy_env["STORAGE_PUBLIC_ORIGIN"])
    # The CSP origin and the API's presign endpoint are two views of one
    # deployment input. Plan P9 item 1: a mismatch would either block the
    # browser PUT (CSP) or sign URLs for a different host, so it fails the build.
    assert caddy_env.get(
        "STORAGE_PUBLIC_ENDPOINT_URL"
    ), "caddy must receive STORAGE_PUBLIC_ENDPOINT_URL for the origin match"
    endpoint_origin = parse_public_origin(caddy_env["STORAGE_PUBLIC_ENDPOINT_URL"])
    assert origin == endpoint_origin, (
        "STORAGE_PUBLIC_ORIGIN must be the origin of STORAGE_PUBLIC_ENDPOINT_URL: "
        f"{origin!r} != {endpoint_origin!r}"
    )

    api = services["api"]
    assert {"edge", "backend"} <= _network_names(api)
    command = _command(api)
    assert "--proxy-headers" in command
    assert "--forwarded-allow-ips" in command
    trusted = command[command.index("--forwarded-allow-ips") + 1]
    assert trusted == subnet, (
        f"the API must trust exactly the edge network ({subnet!r}), got {trusted!r}"
    )
    assert trusted != "*", "forwarded-allow-ips must never trust every peer"

    for name in _APPLICATION_SERVICES - {"api"}:
        names = _network_names(services[name])
        assert "backend" in names, f"{name} must join the backend network"
        assert "edge" not in names, f"{name} must not join the browser-facing edge network"


def _assert_local(document: dict[str, Any]) -> None:
    frontend = document["services"]["frontend"]
    environment = _environment(frontend)
    assert environment.get("STORAGE_PUBLIC_ORIGIN"), "frontend must receive STORAGE_PUBLIC_ORIGIN"
    assert environment.get("NGINX_ENVSUBST_FILTER") == "STORAGE_PUBLIC_ORIGIN"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=("local", "production"), required=True)
    arguments = parser.parse_args()
    document = json.load(sys.stdin)
    if arguments.profile == "production":
        _assert_production(document)
    else:
        _assert_local(document)


if __name__ == "__main__":
    main()
