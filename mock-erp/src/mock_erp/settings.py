"""Runtime configuration, read from ``ERP_*`` environment variables."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ERP_")

    database_url: str = "postgresql+psycopg://mock_erp:mock_erp@localhost:55432/mock_erp"
    api_token: str = "erp-local-token"
    # Exposes PUT /_faults so simulations can make the ERP fail on demand. Never enable outside a test stack.
    fault_injection_enabled: bool = False
