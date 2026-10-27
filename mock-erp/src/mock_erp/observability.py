"""Correlation ids and structured JSON logs."""

import json
import logging
import sys
import uuid
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from datetime import UTC, datetime

from fastapi import FastAPI, Request, Response

CORRELATION_ID_HEADER = "X-Correlation-ID"

CallNext = Callable[[Request], Awaitable[Response]]

correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)

_LOGGED_FIELDS = ("external_reference", "erp_transaction_id", "status")


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str) -> None:
        super().__init__()
        self._service = service

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "service": self._service,
            "event": record.getMessage(),
            "correlation_id": correlation_id.get(),
        }
        entry.update({field: getattr(record, field, None) for field in _LOGGED_FIELDS})
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def configure_logging(service: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)


def install_correlation_middleware(app: FastAPI) -> None:
    """Accepts the caller's correlation id (or mints one) and echoes it on every response."""

    @app.middleware("http")
    async def propagate_correlation_id(request: Request, call_next: CallNext) -> Response:
        token = correlation_id.set(request.headers.get(CORRELATION_ID_HEADER) or str(uuid.uuid4()))
        try:
            response = await call_next(request)
            response.headers[CORRELATION_ID_HEADER] = correlation_id.get() or ""
            return response
        finally:
            correlation_id.reset(token)
