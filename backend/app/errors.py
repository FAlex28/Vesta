from __future__ import annotations

from typing import Any


class AppError(Exception):
    """A safe, structured error that may be returned to an API client."""

    def __init__(
        self,
        *,
        code: str,
        message: str,
        status_code: int,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}


class NotFoundError(AppError):
    def __init__(self, message: str = "Resource not found") -> None:
        super().__init__(code="NOT_FOUND", message=message, status_code=404)


class ConflictError(AppError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code=code, message=message, status_code=409)


class AuthenticationError(AppError):
    def __init__(self, message: str = "Invalid or missing authorization") -> None:
        super().__init__(code="UNAUTHORIZED", message=message, status_code=401)


class ServiceUnavailableError(AppError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code=code, message=message, status_code=503)
