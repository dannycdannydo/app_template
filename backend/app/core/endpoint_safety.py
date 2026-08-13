"""Endpoint safety rules for the local OpenAI-compatible provider.

v0.7 Scope §6.3: ``LocalOpenAICompatibleProvider`` targets a privately
reachable OpenAI-compatible server (Ollama, vLLM or SGLang) and must never be
exposed to browsers or reach a public HTTP endpoint. The rule is enforced
twice: the typed settings validator (BP §27) rejects unsafe ``AI_LOCAL_*``
configuration, and the adapter constructor re-checks the same rule so a
directly constructed adapter cannot bypass it (defense in depth, ADR-0017).
The same plain-HTTP predicate gates the runtime's dev managed-URL staging
decision (a local storage seam cannot produce a provider-reachable HTTPS
signed URL, so the AI layer stages through GCS instead).
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

# Host suffixes accepted over plain HTTP in addition to literal loopback.
# ``*.local`` is mDNS for local servers; single-label hostnames (no dot) are
# the private docker-compose network names the local stack uses (e.g.
# ``ollama``, ``vllm``). Everything else over HTTP — any dotted public-looking
# host — is rejected; HTTPS is always allowed.
_ALLOWED_HTTP_SUFFIXES = (".local",)


def is_private_http_host(host: str) -> bool:
    """Whether plain-HTTP to ``host`` is safe: loopback, private-range
    addresses, ``*.local`` mDNS names or single-label private-network hosts.

    The single safety predicate behind every plain-HTTP allowance in the
    template (the local provider, provider base-url overrides and the managed
    signed-URL minter): a public plain-HTTP endpoint would ship credentials or
    bearer URLs over an unauthenticated link. HTTPS is always allowed
    regardless of the host.
    """
    host = (host or "").strip().lower()
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # A hostname over plain HTTP: only mDNS and single-label private-
        # network names are allowed; a dotted name is a public reachability
        # risk.
        return host.endswith(_ALLOWED_HTTP_SUFFIXES) or "." not in host
    return address.is_loopback or address.is_private


def validate_local_endpoint(endpoint: str) -> str:
    """Validate an OpenAI-compatible local endpoint URL; return it trimmed.

    Allows ``https`` to any host and ``http`` only to loopback, private-range
    addresses, ``*.local`` hostnames or single-label private-network hosts.
    Raises :class:`ValueError` with an actionable message otherwise. Empty
    input is invalid: a local provider that is enabled must name an endpoint.
    """
    value = (endpoint or "").strip().rstrip("/")
    if not value:
        raise ValueError("ai_local_base_url is required when the local provider is enabled")
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("ai_local_base_url must be an http(s) URL")
    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError("ai_local_base_url must include a host")
    if parsed.scheme == "https" or is_private_http_host(host):
        return value
    if parsed.scheme == "http":
        raise ValueError(
            "ai_local_base_url over plain HTTP must be a loopback, private-range, "
            "*.local or single-label host; use https for any other host"
        )
    return value
