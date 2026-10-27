"""Every error has the same shape: ``{"error": {"code", "message", "correlation_id", "details"}}``."""

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from integration_service.errors import ApiError, ErrorCode
from integration_service.gateways.errors import GatewayError
from integration_service.observability import current_correlation_id

logger = logging.getLogger(__name__)


def error_response(status_code: int, code: ErrorCode, message: str, details: Any = None) -> JSONResponse:
    error: dict[str, Any] = {"code": code.value, "message": message, "correlation_id": current_correlation_id()}
    if details:
        error["details"] = details
    return JSONResponse({"error": error}, status_code=status_code)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def handle_api_error(_: Request, exc: ApiError) -> JSONResponse:
        return error_response(exc.status_code, exc.code, exc.message, exc.details or None)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        details = [{"field": ".".join(map(str, e["loc"][1:])), "message": e["msg"]} for e in exc.errors()]
        return error_response(422, ErrorCode.VALIDATION_ERROR, "Request validation failed", details)

    @app.exception_handler(GatewayError)
    async def handle_gateway_error(_: Request, exc: GatewayError) -> JSONResponse:
        code = ErrorCode.FINERACT_UNAVAILABLE if exc.system == "fineract" else ErrorCode.ERP_UNAVAILABLE
        return error_response(503, code, exc.message)

    @app.exception_handler(Exception)
    async def handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error")
        return error_response(
            500, ErrorCode.INTERNAL_ERROR, "Internal error; see the service logs for this correlation id"
        )
