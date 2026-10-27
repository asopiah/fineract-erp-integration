"""One error shape for every failure: ``{"error": {"code", "message", "correlation_id", "details"}}``."""

from enum import StrEnum
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from mock_erp.observability import correlation_id


class ErrorCode(StrEnum):
    UNAUTHORIZED = "UNAUTHORIZED"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    NOT_FOUND = "NOT_FOUND"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    ACCOUNT_CODE_TAKEN = "ACCOUNT_CODE_TAKEN"
    UNKNOWN_ACCOUNT = "UNKNOWN_ACCOUNT"
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
    UNBALANCED_ENTRY = "UNBALANCED_ENTRY"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    RATE_LIMITED = "RATE_LIMITED"


class ApiError(Exception):
    def __init__(
        self, status_code: int, code: ErrorCode, message: str, *, headers: dict[str, str] | None = None
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = headers


def error_body(code: ErrorCode, message: str, details: list[Any] | None = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message, "correlation_id": correlation_id.get()}
    if details:
        error["details"] = details
    return {"error": error}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def handle_api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(error_body(exc.code, exc.message), status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        details = [{"field": ".".join(map(str, e["loc"][1:])), "message": e["msg"]} for e in exc.errors()]
        return JSONResponse(
            error_body(ErrorCode.VALIDATION_ERROR, "Request validation failed", details), status_code=422
        )
