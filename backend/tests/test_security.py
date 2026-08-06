"""Unit tests for the centralised security helpers (v0.2 Scope §6.2, BP §8, §30).

The real :class:`WorkOSSessionValidator` is exercised against tokens minted
with a local RSA key served through a stub JWKS client, so no network is
touched.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from tests.auth_helpers import (
    CLIENT_ID,
    ISSUER,
    build_validator,
    generate_key_pair,
    make_token,
    webhook_signature_header,
)

from app.core.security import InvalidSessionError, ValidatedSession, verify_webhook_signature

KeyPair = tuple[rsa.RSAPrivateKey, str]


@pytest.fixture
def key_pair() -> KeyPair:
    """A fresh local RSA key pair for token minting."""
    return generate_key_pair()


async def test_valid_token_yields_validated_session(key_pair: KeyPair) -> None:
    private_key, _ = key_pair
    validator = build_validator(private_key)
    session = await validator.validate_token(make_token(private_key))

    assert isinstance(session, ValidatedSession)
    assert session.workos_user_id == "user_test123"
    assert session.session_id == "session_test123"
    assert session.organisation_id is None
    assert session.claims["client_id"] == CLIENT_ID


async def test_valid_token_with_organisation_claim(key_pair: KeyPair) -> None:
    private_key, _ = key_pair
    validator = build_validator(private_key)
    token = make_token(private_key, extra={"org_id": "org_1"})
    session = await validator.validate_token(token)
    assert session.organisation_id == "org_1"


@pytest.mark.parametrize(
    ("token_kwargs", "reason"),
    [
        ({"issuer": "https://evil.example.com/"}, "invalid_issuer"),
        ({"client_id": "client_other"}, "invalid_audience"),
        ({"aud": "client_other"}, "invalid_audience"),
        ({"aud": ["client_other", "another"]}, "invalid_audience"),
        ({"seconds_valid": -3600}, "expired"),
        ({"extra": {"sub": 12345}}, "invalid_token"),
    ],
)
async def test_invalid_tokens_are_rejected(
    key_pair: KeyPair, token_kwargs: dict[str, Any], reason: str
) -> None:
    private_key, _ = key_pair
    validator = build_validator(private_key)
    token = make_token(private_key, **token_kwargs)

    with pytest.raises(InvalidSessionError) as excinfo:
        await validator.validate_token(token)
    assert excinfo.value.reason == reason


async def test_token_with_aud_list_containing_client_id_is_accepted(key_pair: KeyPair) -> None:
    private_key, _ = key_pair
    validator = build_validator(private_key)
    token = make_token(private_key, aud=[CLIENT_ID, "another-client"])

    session = await validator.validate_token(token)
    assert session.workos_user_id == "user_test123"


async def test_token_with_invalid_signature_is_rejected(key_pair: KeyPair) -> None:
    private_key, _ = key_pair
    other_key, _ = generate_key_pair()
    validator = build_validator(private_key)
    token = make_token(other_key)

    with pytest.raises(InvalidSessionError) as excinfo:
        await validator.validate_token(token)
    assert excinfo.value.reason == "invalid_signature"


async def test_token_with_different_issuer_is_rejected(key_pair: KeyPair) -> None:
    """Issuer matching is exact; configuration must mirror the token claim."""
    private_key, _ = key_pair
    validator = build_validator(private_key, issuer=ISSUER)
    token = make_token(private_key, issuer="https://api.workos.com")

    with pytest.raises(InvalidSessionError) as excinfo:
        await validator.validate_token(token)
    assert excinfo.value.reason == "invalid_issuer"


@pytest.mark.parametrize("missing_claim", ["exp", "iat", "iss", "sub", "sid", "client_id"])
async def test_required_session_claims_cannot_be_omitted(
    key_pair: KeyPair, missing_claim: str
) -> None:
    private_key, _ = key_pair
    validator = build_validator(private_key)
    token = make_token(private_key, omit_claims={missing_claim})

    with pytest.raises(InvalidSessionError) as excinfo:
        await validator.validate_token(token)
    assert excinfo.value.reason == "invalid_token"


async def test_token_can_use_default_application_as_issuer(key_pair: KeyPair) -> None:
    """Multiple WorkOS applications share an issuer but retain their own client_id."""
    private_key, _ = key_pair
    default_application_client_id = "client_environment_default"
    environment_issuer = f"https://api.workos.com/user_management/{default_application_client_id}"
    validator = build_validator(
        private_key,
        client_id=CLIENT_ID,
        expected_issuer=environment_issuer,
    )
    token = make_token(
        private_key,
        client_id=CLIENT_ID,
        issuer=environment_issuer,
    )

    session = await validator.validate_token(token)
    assert session.workos_user_id == "user_test123"


def test_webhook_signature_validates_current_timestamp() -> None:
    secret = "whsec_test"
    payload = b'{"type":"user.created","data":{}}'
    header = webhook_signature_header(payload, secret, int(time.time() * 1000))

    assert verify_webhook_signature(payload, header, secret) is True


def test_webhook_signature_accepts_second_precision_timestamp() -> None:
    """WorkOS documents milliseconds but publishes examples in seconds."""
    secret = "whsec_test"
    payload = b'{"type":"user.created","data":{}}'
    header = webhook_signature_header(payload, secret, int(time.time()))

    assert verify_webhook_signature(payload, header, secret) is True


def test_webhook_signature_rejects_tampered_payload() -> None:
    secret = "whsec_test"
    header = webhook_signature_header(b'{"type":"user.created"}', secret, int(time.time() * 1000))

    assert verify_webhook_signature(b'{"type":"user.deleted"}', header, secret) is False


def test_webhook_signature_rejects_wrong_secret() -> None:
    payload = b'{"type":"user.created"}'
    header = webhook_signature_header(payload, "whsec_right", int(time.time() * 1000))

    assert verify_webhook_signature(payload, header, "whsec_wrong") is False


def test_webhook_signature_rejects_replayed_timestamp() -> None:
    secret = "whsec_test"
    payload = b'{"type":"user.created"}'
    old_ms = int(time.time() * 1000) - 10 * 60 * 1000  # ten minutes ago, beyond tolerance
    header = webhook_signature_header(payload, secret, old_ms)

    assert verify_webhook_signature(payload, header, secret) is False


def test_webhook_signature_rejects_malformed_header() -> None:
    assert verify_webhook_signature(b"{}", "not-a-signature-header", "secret") is False
    assert verify_webhook_signature(b"", "t=1,v1=abc", "secret") is False
