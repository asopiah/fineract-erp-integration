"""Runtime configuration, read from ``INTEGRATION_*`` environment variables."""

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="INTEGRATION_")

    database_url: str = "postgresql+psycopg://integration:integration@localhost:55432/integration"
    api_token: SecretStr = SecretStr("integration-local-token")

    fineract_url: str = "http://localhost:58080/fineract-provider/api/v1"
    fineract_tenant: str = "default"
    fineract_username: str = "integration_api"
    fineract_password: SecretStr = SecretStr("Integration#2026")
    fineract_timeout_seconds: float = 15.0
    # Reference data the bootstrap creates in Fineract, addressed by business keys rather than numeric ids.
    fineract_office_external_id: str = "KGL-MAIN"
    fineract_loan_product_external_id: str = "SME-ASSET-FINANCE"
    fineract_payment_type_name: str = "Bank Transfer"

    erp_url: str = "http://localhost:58001"
    erp_api_token: SecretStr = SecretStr("erp-local-token")
    erp_timeout_seconds: float = 5.0

    # Fineract calls within one request: each attempt looks the operation up before (re)submitting it.
    fineract_attempts: int = 3
    fineract_backoff_seconds: float = 0.5
    # Work left for recovery (an unknown Fineract outcome, an ERP posting): retried with capped exponential backoff,
    # then routed to NEEDS_RECONCILIATION for a human.
    # 20 attempts at 2 s doubling to a 300 s cap: an outage of up to about an hour heals by itself.
    recovery_max_attempts: int = 20
    recovery_backoff_seconds: float = 2.0
    recovery_backoff_cap_seconds: float = 300.0

    # An operation is owned by one executor at a time; a crashed owner's lease expires and recovery takes over.
    lease_seconds: float = 60.0
    recovery_interval_seconds: float = 5.0
    recovery_enabled: bool = True

    # Exposes /api/admin/faults so simulations can make Fineract fail on demand. Never enable outside a test stack.
    fault_injection_enabled: bool = False

    def recovery_delay(self, attempts: int) -> float:
        """Seconds to wait after the given number of failed attempts."""
        return min(self.recovery_backoff_cap_seconds, self.recovery_backoff_seconds * 2.0 ** (attempts - 1))
