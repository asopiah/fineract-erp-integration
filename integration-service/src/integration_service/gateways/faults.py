"""Failures on demand for the reliability simulations. Off unless fault injection is enabled.

Faults act on the HTTP transport, below the gateway, so the simulations exercise exactly the error handling that a
real outage would.
"""

import threading
from enum import StrEnum

import httpx
from pydantic import BaseModel, Field


class FineractFault(StrEnum):
    NONE = "none"
    # Nothing reaches Fineract (connection refused): every call fails before sending.
    UNAVAILABLE = "unavailable"
    # Commands reach Fineract and are processed; the response is lost (read timeout). Reads are unaffected.
    TIMEOUT_AFTER_SEND = "timeout_after_send"


class FaultConfig(BaseModel):
    fineract: FineractFault = FineractFault.NONE
    remaining: int | None = Field(default=None, ge=1, description="Affect only the next N calls (null: until cleared)")
    # Stop the process right after the next operation reaches FINERACT_DONE, before the ERP is called.
    crash_after_fineract: bool = False


class FaultInjector:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._config = FaultConfig()

    @property
    def config(self) -> FaultConfig:
        with self._lock:
            return self._config

    def configure(self, config: FaultConfig) -> None:
        with self._lock:
            self._config = config

    def wrap(self, transport: httpx.BaseTransport) -> httpx.BaseTransport:
        return _FaultInjectingTransport(transport, self)

    def take_crash(self) -> bool:
        with self._lock:
            if not self._config.crash_after_fineract:
                return False
            self._config = self._config.model_copy(update={"crash_after_fineract": False})
            return True

    def take_fineract_fault(self, request: httpx.Request) -> FineractFault:
        with self._lock:
            fault = self._config.fineract
            if fault is FineractFault.NONE or (fault is FineractFault.TIMEOUT_AFTER_SEND and request.method == "GET"):
                return FineractFault.NONE
            if self._config.remaining is not None:
                left = self._config.remaining - 1
                cleared = FaultConfig(crash_after_fineract=self._config.crash_after_fineract)
                self._config = cleared if left == 0 else self._config.model_copy(update={"remaining": left})
            return fault


class _FaultInjectingTransport(httpx.BaseTransport):
    def __init__(self, inner: httpx.BaseTransport, faults: FaultInjector) -> None:
        self._inner = inner
        self._faults = faults

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        match self._faults.take_fineract_fault(request):
            case FineractFault.UNAVAILABLE:
                raise httpx.ConnectError("injected fault: Fineract unavailable", request=request)
            case FineractFault.TIMEOUT_AFTER_SEND:
                response = self._inner.handle_request(request)
                response.read()
                response.close()
                raise httpx.ReadTimeout("injected fault: response lost after Fineract processed it", request=request)
            case _:
                return self._inner.handle_request(request)

    def close(self) -> None:
        self._inner.close()
