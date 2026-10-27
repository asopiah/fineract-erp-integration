"""Application factory. Run with ``uvicorn mock_erp.main:create_app --factory``."""

from fastapi import FastAPI

from mock_erp.api import admin, api
from mock_erp.chart import seed_chart
from mock_erp.database import create_session_factory
from mock_erp.errors import install_error_handlers
from mock_erp.faults import FaultInjector
from mock_erp.observability import configure_logging, install_correlation_middleware
from mock_erp.settings import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    configure_logging("mock-erp")

    session_factory = create_session_factory(settings.database_url)
    with session_factory() as session:
        seed_chart(session)

    app = FastAPI(
        title="Mock ERP",
        version="1.0.0",
        description="Accounting master: owns the chart of accounts and records balanced journal entries.",
    )
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.faults = FaultInjector()

    install_correlation_middleware(app)
    install_error_handlers(app)

    @app.get("/health", tags=["health"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(api)
    if settings.fault_injection_enabled:
        app.include_router(admin)
    return app
