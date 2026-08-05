"""Domain exceptions and the standard API error schema (blueprint §13).

Services raise domain exceptions; central FastAPI handlers in ``app.main``
translate them into HTTP responses using the one structured error format.
"""

from __future__ import annotations

from pydantic import BaseModel

from app.core.logging import current_request_id


class ErrorDetail(BaseModel):
    """One field-level validation problem."""

    field: str | None = None
    message: str


class ErrorResponse(BaseModel):
    """The single structured error format used by the whole API."""

    code: str
    message: str
    details: list[ErrorDetail] | None = None
    request_id: str = ""


class APIError(Exception):
    """Base class for domain errors that are translated to HTTP responses.

    Subclasses fix the HTTP status, default ``code`` and default message;
    callers may override code and message per occurrence.
    """

    status_code: int = 500
    code: str = "internal_error"
    default_message: str = "An unexpected error occurred."

    def __init__(
        self,
        *,
        code: str | None = None,
        message: str | None = None,
        details: list[ErrorDetail] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.code = code or self.code
        self.message = message or self.default_message
        self.details = details
        self.headers = headers
        super().__init__(self.message)

    def as_error_response(self) -> ErrorResponse:
        """Build the standard error response, stamping the current request ID."""
        return ErrorResponse(
            code=self.code,
            message=self.message,
            details=self.details,
            request_id=current_request_id(),
        )


class NotFoundError(APIError):
    status_code = 404
    code = "not_found"
    default_message = "The requested resource could not be found."


class BadRequestError(APIError):
    status_code = 400
    code = "bad_request"
    default_message = "The request is invalid."


class UnauthorizedError(APIError):
    status_code = 401
    code = "unauthorized"
    default_message = "Authentication is required to access this resource."


class PermissionDenied(APIError):
    status_code = 403
    code = "permission_denied"
    default_message = "You do not have permission to perform this action."


class ConflictError(APIError):
    status_code = 409
    code = "conflict"
    default_message = "The request conflicts with the current state of the resource."


class ValidationError(APIError):
    status_code = 422
    code = "validation_error"
    default_message = "The request contains invalid data."


class RateLimitExceeded(APIError):
    status_code = 429
    code = "rate_limit_exceeded"
    default_message = "Too many requests. Please try again later."


class ExternalServiceError(APIError):
    status_code = 502
    code = "external_service_error"
    default_message = "An upstream service failed."


class ServiceUnavailableError(APIError):
    status_code = 503
    code = "service_unavailable"
    default_message = "The service is temporarily unavailable."
