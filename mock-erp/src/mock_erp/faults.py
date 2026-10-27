"""Failures on demand, for the reliability simulations. Mounted only when fault injection is enabled."""

import threading
import time
from enum import StrEnum

from pydantic import BaseModel, Field

from mock_erp.errors import ApiError, ErrorCode


class FaultMode(StrEnum):
    NONE = "none"
    # Every API call answers 503 before reading or writing anything.
    UNAVAILABLE = "unavailable"
    # Journal posts commit, then hold the response past the caller's timeout: the write happened, the caller can't tell.
    TIMEOUT_AFTER_COMMIT = "timeout_after_commit"
    # Every API call answers 429 with Retry-After.
    RATE_LIMITED = "rate_limited"


class FaultConfig(BaseModel):
    mode: FaultMode = FaultMode.NONE
    delay_seconds: float = Field(default=35.0, gt=0, le=300)
    remaining: int | None = Field(
        default=None, ge=1, description="Affect only the next N requests (null: until cleared)"
    )


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

    def before_request(self) -> None:
        config = self._consume(FaultMode.UNAVAILABLE, FaultMode.RATE_LIMITED)
        if config is None:
            return
        if config.mode is FaultMode.UNAVAILABLE:
            raise ApiError(503, ErrorCode.SERVICE_UNAVAILABLE, "ERP is unavailable (injected fault)")
        raise ApiError(
            429, ErrorCode.RATE_LIMITED, "ERP rate limit exceeded (injected fault)", headers={"Retry-After": "1"}
        )

    def after_commit(self) -> None:
        config = self._consume(FaultMode.TIMEOUT_AFTER_COMMIT)
        if config is not None:
            time.sleep(config.delay_seconds)

    def _consume(self, *modes: FaultMode) -> FaultConfig | None:
        """Returns the active fault if it is one of ``modes``, counting it against ``remaining``."""
        with self._lock:
            config = self._config
            if config.mode not in modes:
                return None
            if config.remaining is not None:
                left = config.remaining - 1
                self._config = FaultConfig() if left == 0 else config.model_copy(update={"remaining": left})
            return config
