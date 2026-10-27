"""Error codes of the partner API, and the exception that carries one to the HTTP layer."""

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    UNAUTHORIZED = "UNAUTHORIZED"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    NOT_FOUND = "NOT_FOUND"
    # Idempotency and uniqueness
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    CLIENT_UID_TAKEN = "CLIENT_UID_TAKEN"
    LOAN_ALREADY_APPROVED = "LOAN_ALREADY_APPROVED"
    LOAN_ALREADY_DISBURSED = "LOAN_ALREADY_DISBURSED"
    # Preconditions held locally
    CLIENT_NOT_READY = "CLIENT_NOT_READY"
    LOAN_NOT_READY = "LOAN_NOT_READY"
    NOT_RESUMABLE = "NOT_RESUMABLE"
    # Downstream outcomes
    FINERACT_REJECTED = "FINERACT_REJECTED"
    FINERACT_UNAVAILABLE = "FINERACT_UNAVAILABLE"
    ERP_UNAVAILABLE = "ERP_UNAVAILABLE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class ApiError(Exception):
    def __init__(self, status_code: int, code: ErrorCode, message: str, **details: Any) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details

    def as_stored(self) -> dict[str, Any]:
        """The form kept on an operation, so a replay answers with the same error."""
        return {"status_code": self.status_code, "code": self.code.value, "message": self.message, **self.details}

    @classmethod
    def from_stored(cls, stored: dict[str, Any]) -> "ApiError":
        details = {key: value for key, value in stored.items() if key not in ("status_code", "code", "message")}
        return cls(stored["status_code"], ErrorCode(stored["code"]), stored["message"], **details)
