"""Structured JSON logs that carry the identifiers needed to trace one business operation end to end.

Every log line has the same fields. Values bound with ``log_context`` apply to every line logged inside the block
(including from other modules); values passed in ``extra`` apply to one line.
"""

import json
import logging
import sys
import uuid
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from types import MappingProxyType

from fastapi import FastAPI, Request, Response

CORRELATION_ID_HEADER = "X-Correlation-ID"

LOG_FIELDS = (
    "correlation_id",
    "external_reference",
    "client_uid",
    "loan_reference",
    "fineract_transaction_id",
    "erp_transaction_id",
    "status",
)

_context: ContextVar[Mapping[str, object]] = ContextVar("log_context", default=MappingProxyType({}))

CallNext = Callable[[Request], Awaitable[Response]]


@contextmanager
def log_context(**fields: object) -> Iterator[None]:
    """Binds fields to every log line written inside the block. ``None`` values leave existing bindings alone."""
    bound = {**_context.get(), **{name: value for name, value in fields.items() if value is not None}}
    token = _context.set(MappingProxyType(bound))
    try:
        yield
    finally:
        _context.reset(token)


def current_correlation_id() -> str | None:
    value = _context.get().get("correlation_id")
    return str(value) if value is not None else None


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str) -> None:
        super().__init__()
        self._service = service

    def format(self, record: logging.LogRecord) -> str:
        bound = _context.get()
        entry: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "service": self._service,
            "logger": record.name,
            "event": record.getMessage(),
        }
        entry.update({field: getattr(record, field, bound.get(field)) for field in LOG_FIELDS})
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def configure_logging(service: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    # httpx logs every request at INFO without our fields; the gateways log what matters.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def install_correlation_middleware(app: FastAPI) -> None:
    """Accepts the caller's correlation id (or mints one), binds it to the request's logs and echoes it back."""

    @app.middleware("http")
    async def propagate_correlation_id(request: Request, call_next: CallNext) -> Response:
        correlation_id = request.headers.get(CORRELATION_ID_HEADER) or str(uuid.uuid4())
        with log_context(correlation_id=correlation_id):
            response = await call_next(request)
        response.headers[CORRELATION_ID_HEADER] = correlation_id
        return response
