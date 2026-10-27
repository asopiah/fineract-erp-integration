"""Application factory. Run with ``uvicorn integration_service.main:create_app --factory``."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from integration_service.api.admin import admin, faults
from integration_service.api.errors import install_error_handlers
from integration_service.api.routes import router
from integration_service.container import Services, build_services
from integration_service.observability import configure_logging, install_correlation_middleware
from integration_service.settings import Settings

DESCRIPTION = """
Partner API in front of Apache Fineract (system of record for clients and loans) and the ERP (accounting master).

* Authenticate with `Authorization: Bearer <token>`.
* Send `X-Correlation-ID` to trace a request through every system; one is minted otherwise and always echoed back.
* Every state-changing request carries an `external_reference`. Repeating it with the same payload replays the
  original result (200), or answers 202 while the first request is still in progress; a different payload is a 409.
* Fineract's numeric ids never appear: clients are addressed by `client_uid`, loans by `loan_reference`.
"""


def create_app(settings: Settings | None = None, services: Services | None = None) -> FastAPI:
    settings = settings or Settings()
    configure_logging("integration-service")
    services = services or build_services(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if settings.recovery_enabled:
            services.recovery.start()
        yield
        services.recovery.stop()

    app = FastAPI(title="Integration Service", version="1.0.0", description=DESCRIPTION, lifespan=lifespan)
    app.state.services = services
    install_error_handlers(app)
    install_correlation_middleware(app)  # added last, so outermost: it wraps the error rendering

    @app.get("/health", tags=["health"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(router)
    app.include_router(admin)
    if settings.fault_injection_enabled:
        app.include_router(faults)
    return app
