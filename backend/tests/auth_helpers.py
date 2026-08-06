"""Shared helpers for authentication tests (v0.2 Scope §6.2).

Token minting and the stub JWKS client let tests exercise the real
:class:`WorkOSSessionValidator` against a local RSA key with no network
access.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt import PyJWK, PyJWKClient

from app.core.security import WorkOSSessionValidator

CLIENT_ID = "client_test123"
ISSUER = "https://api.workos.com/"


def generate_key_pair() -> tuple[rsa.RSAPrivateKey, str]:
    """Return (private key, public key PEM) for RS256 token minting."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("ascii")
    )
    return private_key, public_pem


def _b64u(value: int) -> str:
    length = (value.bit_length() + 7) // 8
    return base64.urlsafe_b64encode(value.to_bytes(length, "big")).rstrip(b"=").decode("ascii")


class StubJWKSClient(PyJWKClient):
    """A PyJWKClient that serves one fixed RSA public key without a network."""

    def __init__(self, private_key: rsa.RSAPrivateKey) -> None:
        super().__init__("https://jwks.invalid/", cache_jwk_set=False)
        numbers = private_key.public_key().public_numbers()
        self._jwk: dict[str, Any] = {
            "kty": "RSA",
            "use": "sig",
            "alg": "RS256",
            "kid": "test-key",
            "n": _b64u(numbers.n),
            "e": _b64u(numbers.e),
        }

    def get_signing_key_from_jwt(self, token: str | bytes) -> PyJWK:
        return PyJWK(self._jwk, algorithm="RS256")


def build_validator(
    private_key: rsa.RSAPrivateKey,
    *,
    client_id: str = CLIENT_ID,
    issuer: str = ISSUER,
    expected_issuer: str | None = None,
    leeway_seconds: float = 30.0,
) -> WorkOSSessionValidator:
    """Build the real session validator backed by a local signing key."""
    return WorkOSSessionValidator(
        client_id=client_id,
        api_base_url=issuer,
        issuer=expected_issuer or issuer,
        leeway_seconds=leeway_seconds,
        jwks_client=StubJWKSClient(private_key),
    )


def make_token(
    private_key: rsa.RSAPrivateKey,
    *,
    sub: str = "user_test123",
    client_id: str = CLIENT_ID,
    issuer: str = ISSUER,
    sid: str = "session_test123",
    aud: str | list[str] | None = None,
    seconds_valid: int = 3600,
    extra: dict[str, Any] | None = None,
    omit_claims: set[str] | None = None,
) -> str:
    """Mint an RS256 token with WorkOS-style claims; kwargs override defaults."""
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": sub,
        "sid": sid,
        "client_id": client_id,
        "iss": issuer,
        "iat": now,
        "exp": now + timedelta(seconds=seconds_valid),
    }
    if aud is not None:
        payload["aud"] = aud
    if extra:
        payload.update(extra)
    for claim in omit_claims or set():
        payload.pop(claim, None)
    return jwt.encode(payload, private_key, algorithm="RS256")


def webhook_signature_header(payload: bytes, secret: str, timestamp_ms: int) -> str:
    """Build a ``workos-signature`` header value for a payload and timestamp."""
    digest = hmac.new(
        secret.encode("utf-8"),
        f"{timestamp_ms}.{payload.decode('utf-8')}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return f"t={timestamp_ms},v1={digest}"
