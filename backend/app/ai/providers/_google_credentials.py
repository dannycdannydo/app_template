"""Shared Google credential loading and refresh (v0.7 Scope §6.3, ADR-0018, v0.8 Scope §6.4).

Both Google-backed adapters — the Vertex AI ``generateContent`` adapter and the
Vertex GCS staging store — authenticate with the same boundary (ADR-0018): the
configured Google Cloud project, never a Gemini API key, using Application
Default Credentials (workload identity on managed platforms,
``GOOGLE_APPLICATION_CREDENTIALS`` locally) or an approved service-account key
file. Keeping the loader and the Authorization-header refresh in one private
module means the two adapters can never drift about which credentials or which
refresh transport they use. The google-auth SDK is imported only here and both
callers live under ``app/ai/providers/``, so the import-boundary test holds
(BP §33, ADR-0017).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import structlog
import urllib3
from google.auth import default as google_auth_default  # pyright: ignore[reportUnknownVariableType]
from google.auth.transport import urllib3 as google_auth_urllib3
from google.oauth2 import service_account  # pyright: ignore[reportUnknownVariableType]

from app.ai.errors import AIInputValidationError, ProviderUnavailableError

logger = structlog.get_logger()

#: The cloud-platform scope both Google-backed adapters request; it is what
#: service-account keys are loaded with and what ADC refresh honours.
_CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"


def load_google_credentials(credentials_path: str = "") -> Any:
    """Load Google credentials from an approved key file or ADC.

    ``credentials_path`` is the optional service-account key JSON path injected
    through the deployment secret mechanism (``AI_VERTEX_CREDENTIALS_PATH``);
    empty uses Application Default Credentials. A configured but unreadable
    file fails fast with a safe :class:`AIInputValidationError`.
    """
    if credentials_path:
        path = Path(credentials_path).expanduser()
        if not path.is_file():
            raise AIInputValidationError(
                f"google credentials file is not readable: {credentials_path}"
            )
        credentials: Any = cast(
            Any,
            service_account.Credentials.from_service_account_file(  # pyright: ignore[reportUnknownMemberType]
                str(path), scopes=[_CLOUD_PLATFORM_SCOPE]
            ),
        )
        return credentials
    adc_result: Any = google_auth_default(  # pyright: ignore[reportUnknownVariableType]
        scopes=[_CLOUD_PLATFORM_SCOPE]
    )
    return adc_result[0]


def google_authorization_header(credentials: Any) -> str:
    """Return the current ``Authorization: Bearer …`` header, refreshing first.

    The token is refreshed lazily through the urllib3 transport already
    provided by boto3 so no extra HTTP client dependency is pulled in
    (pyproject, v0.7 Scope §6.3). A refresh failure (Google blocked,
    certificate or network issue, malformed key) is translated into the safe
    retryable provider taxonomy so the service retries it like any other
    transient provider failure — never a raw SDK exception that escapes as an
    opaque 502. Only the failure category is logged; the token, the key and
    the credential material are never logged or embedded in an error message
    (BP §28).
    """
    if credentials is None:
        raise ProviderUnavailableError("google credentials are unavailable")
    if credentials.token is None or not credentials.valid:
        try:
            pool: Any = urllib3.PoolManager()
            credentials.refresh(google_auth_urllib3.Request(pool))
        except Exception as exc:
            logger.warning(
                "ai.google.credentials.refresh_failed",
                exception_type=type(exc).__name__,
            )
            raise ProviderUnavailableError("google credential refresh failed") from exc
    return f"Bearer {credentials.token}"


__all__ = ["google_authorization_header", "load_google_credentials"]
