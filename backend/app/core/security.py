"""Centralised authentication and webhook security helpers (blueprint §8, §30).

Session validation and webhook signature verification are centralised here per
the blueprint's backend rules; every other component consumes these helpers and
nothing else talks to WorkOS tokens. The WorkOS integration stays behind
adapters (ADR-0001) so the auth surface is testable and swappable: the session
validator verifies a Bearer access token against the WorkOS JWKS endpoint, and
the profile client maps a validated identity to the profile data needed to
provision the internal user.

Design notes:

- A session token carries only the WorkOS user id in ``sub``; email and name
  come from the WorkOS user record so identity fields are never taken from the
  frontend.
- The default WorkOS token has no ``aud`` claim; the ``client_id`` claim plays
  that role, and an ``aud`` claim, when present (JWT templates), must also
  contain the client id.
- The webhook helper accepts timestamps in seconds or milliseconds because the
  WorkOS documentation is inconsistent about the unit.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, ClassVar, Protocol, cast

import jwt
from jwt import PyJWKClient, PyJWTError
from workos import WorkOSClient, WorkOSError

from app.core.config import get_settings
from app.core.exceptions import ExternalServiceError


class InvalidSessionError(Exception):
    """Raised when a session token fails validation.

    ``reason`` is a short stable identifier (e.g. ``"expired"``) useful for
    logging; it is never surfaced to the client as-is.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class ValidatedSession:
    """The claims of a validated WorkOS session token."""

    workos_user_id: str
    session_id: str | None
    organisation_id: str | None
    claims: dict[str, Any]


@dataclass(frozen=True)
class UserProfile:
    """Profile data needed to provision the internal user record."""

    email: str
    name: str


class SessionValidator(Protocol):
    """Validates a Bearer session token without trusting any client input."""

    async def validate_token(self, token: str) -> ValidatedSession:
        """Validate the token and return its claims; raise ``InvalidSessionError``."""
        ...


class UserProfileClient(Protocol):
    """Fetches the profile behind a validated WorkOS user id."""

    async def get_profile(self, workos_user_id: str) -> UserProfile:
        """Return the profile for a WorkOS user; raise on unknown users."""
        ...


class WorkOSSessionValidator:
    """Validates WorkOS session tokens against the WorkOS JWKS endpoint.

    The signature (RS256 key from the JWKS set for the client), issuer,
    audience and expiry are all verified. The JWKS client is injectable so
    tests can substitute a local signing key; the default client fetches and
    caches the JWKS set from ``<api_base_url>sso/jwks/<client_id>``.
    """

    _ALGORITHMS: ClassVar[list[str]] = ["RS256"]

    def __init__(
        self,
        *,
        client_id: str,
        api_base_url: str,
        leeway_seconds: float,
        jwks_client: PyJWKClient | None = None,
    ) -> None:
        self._client_id = client_id
        self._expected_issuer = api_base_url.rstrip("/")
        self._leeway = leeway_seconds
        self._jwks = jwks_client or PyJWKClient(f"{api_base_url}sso/jwks/{client_id}")

    async def validate_token(self, token: str) -> ValidatedSession:
        """Validate a session token, running the blocking JWT work off the loop."""
        return await asyncio.to_thread(self._decode, token)

    def _decode(self, token: str) -> ValidatedSession:
        try:
            signing_key = self._jwks.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=self._ALGORITHMS,
                options={"verify_aud": False, "verify_iss": False},
                leeway=self._leeway,
            )
        except jwt.ExpiredSignatureError as exc:
            raise InvalidSessionError("expired") from exc
        except jwt.exceptions.InvalidSubjectError as exc:
            raise InvalidSessionError("invalid_token") from exc
        except PyJWTError as exc:
            # Covers bad signatures, malformed tokens and JWKS fetch failures;
            # failing closed on any of these is the safe behaviour.
            raise InvalidSessionError("invalid_signature") from exc

        issuer = claims.get("iss")
        if not isinstance(issuer, str) or issuer.rstrip("/") != self._expected_issuer:
            raise InvalidSessionError("invalid_issuer")

        if claims.get("client_id") != self._client_id:
            raise InvalidSessionError("invalid_audience")
        aud = claims.get("aud")
        if aud is not None:
            audiences = cast("list[Any]", aud) if isinstance(aud, list) else [aud]
            if self._client_id not in audiences:
                raise InvalidSessionError("invalid_audience")

        sub = claims.get("sub")
        if not isinstance(sub, str) or not sub:
            raise InvalidSessionError("invalid_token")

        return ValidatedSession(
            workos_user_id=sub,
            session_id=claims.get("sid"),
            organisation_id=claims.get("org_id"),
            claims=claims,
        )


class WorkOSUserProfileClient:
    """Fetches WorkOS user profiles through the WorkOS SDK (ADR-0001).

    The WorkOS API key is held here, inside the adapter, so it never leaks
    into request handlers or response schemas.
    """

    def __init__(
        self,
        *,
        api_key: str,
        client_id: str,
        api_base_url: str,
    ) -> None:
        self._client = WorkOSClient(
            api_key=api_key,
            client_id=client_id,
            base_url=api_base_url,
        )

    async def get_profile(self, workos_user_id: str) -> UserProfile:
        """Fetch the WorkOS user record for a validated identity."""
        try:
            user = await asyncio.to_thread(self._client.user_management.get_user, workos_user_id)
        except WorkOSError as exc:
            raise ExternalServiceError(
                code="workos_profile_unavailable",
                message="Authentication could not be completed. Please try again.",
            ) from exc
        name = user.name or " ".join(p for p in (user.first_name, user.last_name) if p).strip()
        return UserProfile(email=user.email, name=name or user.email)


_WEBHOOK_TOLERANCE_SECONDS = 300


def _normalise_timestamp_ms(timestamp: int) -> int:
    """Return a millisecond timestamp, accepting seconds or milliseconds.

    WorkOS documents the timestamp as milliseconds but publishes examples in
    seconds; a timestamp below ``10**12`` (1973) must be seconds.
    """
    return timestamp * 1000 if timestamp < 10**12 else timestamp


def verify_webhook_signature(
    payload: bytes,
    signature_header: str,
    secret: str,
    *,
    tolerance_seconds: int = _WEBHOOK_TOLERANCE_SECONDS,
) -> bool:
    """Verify a WorkOS webhook signature without raising (BP §30).

    WorkOS signs deliveries with HMAC-SHA256 of ``<t>.<body>`` using the
    endpoint secret, sent as ``workos-signature: t=<ts>,v1=<hex>``. The
    timestamp is checked against a tolerance to prevent replay attacks.
    """
    if not payload or not secret:
        return False

    fields: dict[str, str] = {}
    for item in signature_header.split(","):
        key, separator, value = item.partition("=")
        if separator:
            fields[key.strip()] = value.strip()
    issued_at = fields.get("t")
    provided_digest = fields.get("v1")
    if not issued_at or not provided_digest:
        return False

    try:
        timestamp = int(issued_at)
        expected = hmac.new(
            secret.encode("utf-8"),
            f"{timestamp}.{payload.decode('utf-8')}".encode(),
            hashlib.sha256,
        ).hexdigest()
    except (ValueError, UnicodeDecodeError):
        return False

    if not hmac.compare_digest(expected, provided_digest):
        return False

    age_ms = abs(time.time() * 1000 - _normalise_timestamp_ms(timestamp))
    return age_ms <= tolerance_seconds * 1000


@lru_cache
def get_session_validator() -> SessionValidator:
    """Dependency factory for the process-wide session validator."""
    settings = get_settings()
    return WorkOSSessionValidator(
        client_id=settings.workos_client_id,
        api_base_url=settings.workos_api_base_url,
        leeway_seconds=settings.workos_jwt_leeway,
    )


@lru_cache
def get_user_profile_client() -> UserProfileClient:
    """Dependency factory for the process-wide WorkOS profile client."""
    settings = get_settings()
    return WorkOSUserProfileClient(
        api_key=settings.workos_api_key,
        client_id=settings.workos_client_id,
        api_base_url=settings.workos_api_base_url,
    )
