"""``python -m integration_service.bootstrap``: configures an empty Fineract for the integration. Idempotent.

1. Office "Kigali Main Office" and currency RWF.
2. Payment type for bank transfers.
3. A least-privilege role and the API user the integration service runs as.
4. GL accounts: synchronized from the ERP through the integration service (they must exist before the product).
5. Loan product "SME Asset Finance": 18% a year, 12 monthly repayments, single disbursement, cash-based accounting
   mapped to the synchronized GL accounts.

Every step looks for what it would create first, so the script can be run any number of times.
"""

import logging
import time
from typing import Any

import httpx
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from integration_service.observability import configure_logging

logger = logging.getLogger("bootstrap")

DATE_FORMAT = {"dateFormat": "yyyy-MM-dd", "locale": "en"}
HEAD_OFFICE_ID = 1
OFFICE = {"name": "Kigali Main Office", "externalId": "KGL-MAIN", "openingDate": "2026-01-01"}
PAYMENT_TYPE = "Bank Transfer"
CURRENCY = "RWF"
ROLE = "Integration API"

# Everything the integration service does, and nothing else.
INTEGRATION_PERMISSIONS = (
    "READ_GLACCOUNT",
    "CREATE_GLACCOUNT",
    "READ_OFFICE",
    "READ_LOANPRODUCT",
    "READ_PAYMENTTYPE",
    "READ_CLIENT",
    "CREATE_CLIENT",
    "ACTIVATE_CLIENT",  # clients are created active
    "READ_LOAN",
    "CREATE_LOAN",
    "APPROVE_LOAN",
    "DISBURSE_LOAN",
    "REPAYMENT_LOAN",
    "ADJUST_LOAN",  # Fineract's "undo", used to reverse a repayment
)

LOAN_PRODUCT_EXTERNAL_ID = "SME-ASSET-FINANCE"
# Cash-based accounting: Fineract posts journal entries when cash moves (disbursement, repayment), the same events the
# ERP mirrors. No accruals means no entries the ERP would have to learn to ignore.
CASH_BASED = 2
GL_MAPPING = {
    "fundSourceAccountId": "GL-1100",
    "loanPortfolioAccountId": "GL-1200",
    "transfersInSuspenseAccountId": "GL-1300",
    "overpaymentLiabilityAccountId": "GL-2100",
    "interestOnLoanAccountId": "GL-4000",
    "incomeFromFeeAccountId": "GL-4100",
    "incomeFromPenaltyAccountId": "GL-4200",
    "incomeFromRecoveryAccountId": "GL-4300",
    "writeOffAccountId": "GL-5100",
}


class BootstrapSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BOOTSTRAP_")

    fineract_url: str = "http://localhost:58080/fineract-provider/api/v1"
    fineract_tenant: str = "default"
    fineract_admin_username: str = "mifos"
    fineract_admin_password: SecretStr = SecretStr("password")
    api_username: str = "integration_api"
    api_password: SecretStr = SecretStr("Integration#2026")
    integration_url: str = "http://localhost:58090"
    integration_api_token: SecretStr = SecretStr("integration-local-token")


class Bootstrap:
    def __init__(self, settings: BootstrapSettings) -> None:
        self._settings = settings
        self._fineract = httpx.Client(
            base_url=settings.fineract_url,
            auth=(settings.fineract_admin_username, settings.fineract_admin_password.get_secret_value()),
            headers={"Fineract-Platform-TenantId": settings.fineract_tenant},
            timeout=60.0,
        )
        self._integration = httpx.Client(
            base_url=settings.integration_url,
            headers={"Authorization": f"Bearer {settings.integration_api_token.get_secret_value()}"},
            timeout=120.0,
        )

    def run(self) -> None:
        self._wait_for(self._fineract, "/offices", "Fineract")
        office_id = self._ensure_office()
        self._ensure_currency()
        self._ensure_payment_type()
        role_id = self._ensure_role()
        self._ensure_api_user(office_id, role_id)
        self._wait_for(self._integration, "/health", "integration service")
        self._sync_gl_accounts()
        self._ensure_loan_product()
        logger.info("Fineract is configured")

    def _ensure_office(self) -> int:
        existing = self._get(f"/offices/external-id/{OFFICE['externalId']}")
        if existing is not None:
            return int(existing["id"])
        created = self._post("/offices", {**OFFICE, "parentId": HEAD_OFFICE_ID, **DATE_FORMAT})
        logger.info("created office %s", OFFICE["name"])
        return int(created["resourceId"])

    def _ensure_currency(self) -> None:
        selected = [currency["code"] for currency in self._get("/currencies")["selectedCurrencyOptions"]]
        if CURRENCY not in selected:
            self._put("/currencies", {"currencies": [*selected, CURRENCY]})
            logger.info("enabled currency %s", CURRENCY)

    def _ensure_payment_type(self) -> None:
        if any(item["name"] == PAYMENT_TYPE for item in self._get("/paymenttypes")):
            return
        payment_type = {
            "name": PAYMENT_TYPE,
            "description": "Partner bank transfer",
            "isCashPayment": False,
            "position": 1,
        }
        self._post("/paymenttypes", payment_type)
        logger.info("created payment type %s", PAYMENT_TYPE)

    def _ensure_role(self) -> int:
        role = next((item for item in self._get("/roles") if item["name"] == ROLE), None)
        if role is None:
            role = {
                "id": self._post("/roles", {"name": ROLE, "description": "Integration service, least privilege"})[
                    "resourceId"
                ]
            }
        role_id = int(role["id"])
        # Re-applied on every run, so the role always holds exactly these permissions.
        self._put(f"/roles/{role_id}/permissions", {"permissions": dict.fromkeys(INTEGRATION_PERMISSIONS, True)})
        return role_id

    def _ensure_api_user(self, office_id: int, role_id: int) -> None:
        username = self._settings.api_username
        if any(user["username"] == username for user in self._get("/users")):
            return
        password = self._settings.api_password.get_secret_value()
        self._post(
            "/users",
            {
                "username": username,
                "firstname": "Integration",
                "lastname": "Service",
                "email": "integration@example.com",
                "officeId": office_id,
                "roles": [role_id],
                "sendPasswordToEmail": False,
                "password": password,
                "repeatPassword": password,
                "passwordNeverExpires": True,
            },
        )
        logger.info("created API user %s", username)

    def _sync_gl_accounts(self) -> None:
        response = self._integration.post("/api/accounts/sync")
        response.raise_for_status()
        unsynced = [account["external_reference"] for account in response.json() if account["status"] != "SYNCED"]
        if unsynced:
            raise RuntimeError(f"GL accounts not synchronized: {unsynced}")
        logger.info("GL accounts synchronized from the ERP")

    def _ensure_loan_product(self) -> None:
        if self._get(f"/loanproducts/external-id/{LOAN_PRODUCT_EXTERNAL_ID}") is not None:
            return
        gl_accounts = {field: self._gl_account_id(reference) for field, reference in GL_MAPPING.items()}
        self._post(
            "/loanproducts",
            {
                "name": "SME Asset Finance",
                "shortName": "SMEA",
                "externalId": LOAN_PRODUCT_EXTERNAL_ID,
                "description": "Asset financing for SMEs: 18% a year, 12 monthly repayments, single disbursement",
                "currencyCode": CURRENCY,
                "digitsAfterDecimal": 0,
                "inMultiplesOf": 1,
                "principal": 5_000_000,
                "minPrincipal": 100_000,
                "maxPrincipal": 50_000_000,
                "numberOfRepayments": 12,
                "minNumberOfRepayments": 1,
                "maxNumberOfRepayments": 60,
                "repaymentEvery": 1,
                "repaymentFrequencyType": 2,  # months
                "interestRatePerPeriod": 18,
                "minInterestRatePerPeriod": 0,
                "maxInterestRatePerPeriod": 60,
                "interestRateFrequencyType": 3,  # per year
                "amortizationType": 1,  # equal installments
                "interestType": 0,  # declining balance
                "interestCalculationPeriodType": 1,  # same as repayment period
                "transactionProcessingStrategyCode": "mifos-standard-strategy",
                "daysInYearType": 1,  # actual
                "daysInMonthType": 1,  # actual
                "isInterestRecalculationEnabled": False,
                "multiDisburseLoan": False,
                "accountingRule": CASH_BASED,
                **gl_accounts,
                **DATE_FORMAT,
            },
        )
        logger.info("created loan product SME Asset Finance")

    def _gl_account_id(self, external_reference: str) -> int:
        account = self._get(f"/glaccounts/external-id/{external_reference}")
        if account is None:
            raise RuntimeError(f"GL account {external_reference} is missing in Fineract")
        return int(account["id"])

    def _get(self, path: str) -> Any:
        response = self._fineract.get(path)
        if response.status_code == 404:
            return None
        _raise_for_status(response)
        return response.json()

    def _post(self, path: str, body: dict[str, Any]) -> Any:
        response = self._fineract.post(path, json=body)
        _raise_for_status(response)
        return response.json()

    def _put(self, path: str, body: dict[str, Any]) -> Any:
        response = self._fineract.put(path, json=body)
        _raise_for_status(response)
        return response.json()

    @staticmethod
    def _wait_for(client: httpx.Client, path: str, name: str, timeout_seconds: float = 300) -> None:
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                if client.get(path).status_code < 500:
                    return
            except httpx.HTTPError:
                pass
            if time.monotonic() > deadline:
                raise RuntimeError(f"{name} did not become available")
            logger.info("waiting for %s", name)
            time.sleep(3)


def _raise_for_status(response: httpx.Response) -> None:
    if response.is_error:
        request = response.request
        raise RuntimeError(f"{request.method} {request.url.path}: {response.status_code} {response.text}")


if __name__ == "__main__":
    configure_logging("bootstrap")
    Bootstrap(BootstrapSettings()).run()
