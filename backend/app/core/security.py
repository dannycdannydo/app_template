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
- A validated session is a *bounded* context (plan P1): the raw claim set is
  deliberately not exposed, so request processing can only act on the identity
  fields the application has explicitly chosen to trust. The token's total
  lifetime (``exp - iat``) is capped by ``WORKOS_JWT_MAX_LIFETIME_SECONDS`` and
  a WorkOS ``act`` impersonator claim is captured for the auth boundary to
  reject (see ``get_current_user``); the policy lives in ``app/api/dependencies``
  where the session is consumed.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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

    def __init__(self, reason: str, *, token_issuer: str | None = None) -> None:
        self.reason = reason
        # The issuer is a public JWT claim, retained solely for structured
        # diagnostics. Never attach the token itself or other claims here.
        self.token_issuer = token_issuer
        super().__init__(reason)


@dataclass(frozen=True)
class ValidatedSession:
    """A bounded, validated WorkOS session context (plan P1).

    Only the identity fields the application acts on are retained; the raw
    claim set is deliberately absent so a later code path cannot start trusting
    a claim that was never reviewed. ``authentication_time`` comes from the
    optional ``auth_time`` claim (the default WorkOS access token omits it) and
    ``impersonator`` from the ``act`` impersonator claim; the auth boundary
    rejects an impersonated session.
    """

    workos_user_id: str
    session_id: str | None
    issued_at: datetime
    expires_at: datetime
    organisation_id: str | None
    authentication_time: datetime | None
    impersonator: str | None

    @property
    def is_impersonated(self) -> bool:
        """True when the token carries a WorkOS ``act`` impersonator claim."""
        return self.impersonator is not None


@dataclass(frozen=True)
class UserProfile:
    """Profile data needed to provision the internal user record.

    ``email_verified`` comes from the WorkOS profile and gates the privileged
    platform bootstrap grant (Scope §6.4): a user is only ever granted
    platform_admin when WorkOS reports their email as verified, never on the
    strength of the email alone.
    """

    email: str
    name: str
    email_verified: bool


class SessionValidator(Protocol):
    """Validates a Bearer session token without trusting any client input."""

    async def validate_token(self, token: str) -> ValidatedSession:
        """Validate the token and return its bounded context; raise ``InvalidSessionError``."""
        ...


class UserProfileClient(Protocol):
    """Fetches the profile behind a validated WorkOS user id."""

    async def get_profile(self, workos_user_id: str) -> UserProfile:
        """Return the profile for a WorkOS user; raise on unknown users."""
        ...


def _claim_datetime(claims: dict[str, Any], name: str) -> datetime:
    """Convert a required numeric JWT timestamp claim to an aware datetime.

    A missing, non-numeric or malformed value is an invalid token; the claim is
    never surfaced to the caller.
    """
    value = claims.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidSessionError("invalid_token")
    try:
        return datetime.fromtimestamp(value, tz=UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise InvalidSessionError("invalid_token") from exc


def _optional_claim_datetime(claims: dict[str, Any], name: str) -> datetime | None:
    """Convert an optional numeric JWT timestamp claim, or None when absent.

    Present-but-malformed is rejected (fail closed) rather than ignored.
    """
    if name not in claims:
        return None
    return _claim_datetime(claims, name)


def _optional_claim_string(claims: dict[str, Any], name: str) -> str | None:
    """Convert an optional non-empty string claim, or None when absent.

    Present-but-malformed is rejected (fail closed): the bounded authenticated
    session context must never carry an arbitrary object copied straight from
    the token, so a wrong-typed or empty value is an invalid token.
    """
    if name not in claims:
        return None
    value = claims.get(name)
    if not isinstance(value, str) or not value:
        raise InvalidSessionError("invalid_token")
    return value


def _impersonator(claims: dict[str, Any]) -> str | None:
    """Return the WorkOS impersonator identity from the ``act`` claim, if any.

    WorkOS marks an impersonated session with an ``act`` claim carrying the
    dashboard user's ``sub``. A malformed ``act`` still marks the session as
    impersonated so the auth boundary rejects it.
    """
    act = claims.get("act")
    if act is None:
        return None
    if isinstance(act, dict):
        act_claims = cast("dict[str, Any]", act)
        sub = act_claims.get("sub")
        return sub if isinstance(sub, str) and sub else "unknown"
    if isinstance(act, str) and act:
        return act
    return "unknown"


class WorkOSSessionValidator:
    """Validates WorkOS session tokens against the WorkOS JWKS endpoint.

    The signature (RS256 key from the JWKS set for the client), issuer,
    audience and expiry are all verified. The JWKS client is injectable so
    tests can substitute a local signing key; the default client fetches and
    caches the JWKS set from ``<api_base_url>sso/jwks/<client_id>``. The
    issuer is supplied independently: in WorkOS environments with multiple
    applications, access tokens use the environment default application's
    client ID as ``iss`` while ``client_id`` identifies the active application.
    """

    _ALGORITHMS: ClassVar[list[str]] = ["RS256"]

    def __init__(
        self,
        *,
        client_id: str,
        api_base_url: str,
        issuer: str,
        leeway_seconds: float,
        max_lifetime_seconds: float = 3600.0,
        jwks_client: PyJWKClient | None = None,
    ) -> None:
        self._client_id = client_id
        self._expected_issuer = issuer
        self._leeway = leeway_seconds
        self._max_lifetime = max_lifetime_seconds
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
                # PyJWT only verifies ``exp`` when it exists. Requiring the
                # claims below prevents a token with an omitted expiry,
                # issuer, session ID, or client binding from being accepted.
                # Issuer and audience are compared explicitly below because
                # WorkOS uses ``client_id`` as the primary audience binding.
                options={
                    "verify_aud": False,
                    "verify_iss": False,
                    "require": ["exp", "iat", "iss", "sub", "sid", "client_id"],
                },
                leeway=self._leeway,
            )
        except jwt.ExpiredSignatureError as exc:
            raise InvalidSessionError("expired") from exc
        except jwt.exceptions.InvalidSubjectError as exc:
            raise InvalidSessionError("invalid_token") from exc
        except jwt.InvalidSignatureError as exc:
            raise InvalidSessionError("invalid_signature") from exc
        except PyJWTError as exc:
            # Covers bad signatures, malformed tokens and missing/invalid
            # standard claims. All failures are rejected without exposing JWT
            # details to the caller.
            raise InvalidSessionError("invalid_token") from exc

        issuer = claims.get("iss")
        if not isinstance(issuer, str) or issuer != self._expected_issuer:
            raise InvalidSessionError(
                "invalid_issuer",
                token_issuer=issuer if isinstance(issuer, str) else None,
            )

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
        session_id = claims.get("sid")
        if not isinstance(session_id, str) or not session_id:
            raise InvalidSessionError("invalid_token")

        issued_at = _claim_datetime(claims, "iat")
        expires_at = _claim_datetime(claims, "exp")
        # A token whose total lifetime exceeds the reviewed maximum is rejected
        # even though ``exp`` is still in the future (plan P1 decision 2).
        # This bounds the offline revocation window to at most the maximum.
        lifetime = expires_at - issued_at
        if lifetime <= timedelta(0) or lifetime > timedelta(seconds=self._max_lifetime):
            raise InvalidSessionError("excessive_lifetime")

        return ValidatedSession(
            workos_user_id=sub,
            session_id=session_id,
            issued_at=issued_at,
            expires_at=expires_at,
            organisation_id=_optional_claim_string(claims, "org_id"),
            authentication_time=_optional_claim_datetime(claims, "auth_time"),
            impersonator=_impersonator(claims),
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
        return UserProfile(
            email=user.email,
            name=name or user.email,
            email_verified=bool(user.email_verified),
        )


_WEBHOOK_TOLERANCE_SECONDS = 300


class CachingUserProfileClient:
    """Request-scoped memoising wrapper around a ``UserProfileClient`` (plan P7).

    One successful authentication resolves the same validated identity several
    times: provisioning, the one-time bootstrap check and login-time invitation
    linking each read the WorkOS profile. The profile cannot change within a
    single request, so the first fetch is reused and the later callers share it.
    Every caller still performs its own fail-closed ``email_verified`` and
    membership checks; only the redundant provider round trips are removed.

    The wrapper is created per request (never cached process-wide) so a stale
    profile can never leak between requests.
    """

    def __init__(self, inner: UserProfileClient) -> None:
        self._inner = inner
        self._profiles: dict[str, UserProfile] = {}

    async def get_profile(self, workos_user_id: str) -> UserProfile:
        cached = self._profiles.get(workos_user_id)
        if cached is not None:
            return cached
        profile = await self._inner.get_profile(workos_user_id)
        self._profiles[workos_user_id] = profile
        return profile


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
        issuer=settings.workos_jwt_issuer,
        leeway_seconds=settings.workos_jwt_leeway,
        max_lifetime_seconds=settings.workos_jwt_max_lifetime_seconds,
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
