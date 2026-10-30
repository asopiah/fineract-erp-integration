"""Apache Fineract, addressed by external ids wherever Fineract allows it.

Commands go through Fineract's normal command pipeline, so Fineract enforces its own rules (permissions, loan state,
unique ``externalId`` on clients, loans and loan transactions). Fineract's numeric ids stay inside this module and the
mapping tables.
"""

import threading
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from typing import Any

import httpx

from integration_service.domain.erp import ErpAccount
from integration_service.domain.fineract import (
    FineractClient,
    FineractLoan,
    FineractLoanTransaction,
    LoanBalances,
    LoanStatus,
    LoanTransactionType,
)
from integration_service.domain.gl_accounts import fineract_gl_type
from integration_service.domain.money import plain_decimal
from integration_service.gateways.errors import NotAppliedError
from integration_service.gateways.http import JsonApi, json_or_empty, propagate_correlation_id
from integration_service.settings import Settings

DATE_FORMAT = {"dateFormat": "yyyy-MM-dd", "locale": "en"}
GL_USAGE_DETAIL = 1
LEGAL_FORM_PERSON = 1
TERM_IN_MONTHS = 2

_LOAN_STATUSES = {
    "loanStatusType.submitted.and.pending.approval": LoanStatus.SUBMITTED,
    "loanStatusType.approved": LoanStatus.APPROVED,
    "loanStatusType.active": LoanStatus.ACTIVE,
    "loanStatusType.closed.obligations.met": LoanStatus.CLOSED,
    "loanStatusType.overpaid": LoanStatus.OVERPAID,
    "loanStatusType.rejected": LoanStatus.REJECTED,
    "loanStatusType.withdrawn.by.client": LoanStatus.WITHDRAWN,
    "loanStatusType.closed.written.off": LoanStatus.WRITTEN_OFF,
}


class FineractGateway:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None) -> None:
        self._settings = settings
        client = httpx.Client(
            base_url=settings.fineract_url,
            auth=(settings.fineract_username, settings.fineract_password.get_secret_value()),
            headers={"Fineract-Platform-TenantId": settings.fineract_tenant},
            timeout=httpx.Timeout(settings.fineract_timeout_seconds, connect=3.0),
            transport=transport,
            event_hooks={"request": [propagate_correlation_id]},
        )
        self._api = JsonApi("fineract", client, _error_detail, _is_transient_conflict, _is_permission_denied)
        self._reference_ids: dict[str, int] = {}
        self._reference_lock = threading.Lock()

    # --- GL accounts --------------------------------------------------------------------------------------------

    def create_gl_account(self, account: ErpAccount) -> int:
        """Idempotent in Fineract: an identical replay returns the existing account (see the Fineract patch)."""
        body = {
            "name": account.name,
            "glCode": account.code,
            "type": int(fineract_gl_type(account.type)),
            "usage": GL_USAGE_DETAIL,
            # The ERP owns the ledger: Fineract posts to these accounts from loan transactions only.
            "manualEntriesAllowed": False,
            "description": f"Synchronized from the ERP chart of accounts ({account.external_reference})",
            "externalId": account.external_reference,
        }
        return int(self._api.post("/glaccounts", body)["resourceId"])

    def find_gl_account_id(self, external_reference: str) -> int | None:
        account = self._api.get(f"/glaccounts/external-id/{external_reference}")
        return None if account is None else int(account["id"])

    # --- Clients ------------------------------------------------------------------------------------------------

    def find_client(self, client_uid: str) -> FineractClient | None:
        client = self._api.get(f"/clients/external-id/{client_uid}")
        if client is None:
            return None
        return FineractClient(
            internal_id=int(client["id"]),
            client_uid=client_uid,
            display_name=str(client.get("displayName", "")),
            active=bool(client.get("active", False)),
            status=str(client.get("status", {}).get("value", "")),
        )

    def create_client(self, client_uid: str, first_name: str, last_name: str, activation_date: date) -> None:
        self._api.post(
            "/clients",
            {
                "officeId": self._office_id(),
                "legalFormId": LEGAL_FORM_PERSON,
                "firstname": first_name,
                "lastname": last_name,
                "externalId": client_uid,
                "active": True,
                "activationDate": activation_date.isoformat(),
                "submittedOnDate": activation_date.isoformat(),
                **DATE_FORMAT,
            },
        )

    # --- Loans --------------------------------------------------------------------------------------------------

    def find_loan(self, loan_reference: str) -> FineractLoan | None:
        loan = self._api.get(f"/loans/external-id/{loan_reference}", params={"associations": "transactions"})
        return None if loan is None else _loan(loan_reference, loan)

    def submit_loan_application(
        self,
        loan_reference: str,
        fineract_client_id: int,
        principal: Decimal,
        annual_interest_rate: Decimal,
        term_months: int,
        submitted_on: date,
        expected_disbursement_on: date,
    ) -> None:
        self._api.post(
            "/loans",
            {
                "externalId": loan_reference,
                "loanType": "individual",
                "clientId": fineract_client_id,
                "productId": self._loan_product_id(),
                "principal": plain_decimal(principal),
                "loanTermFrequency": term_months,
                "loanTermFrequencyType": TERM_IN_MONTHS,
                "numberOfRepayments": term_months,
                "repaymentEvery": 1,
                "repaymentFrequencyType": TERM_IN_MONTHS,
                "interestRatePerPeriod": plain_decimal(annual_interest_rate),
                "amortizationType": 1,  # equal installments
                "interestType": 0,  # declining balance
                "interestCalculationPeriodType": 1,  # same as repayment period
                "transactionProcessingStrategyCode": "mifos-standard-strategy",
                "submittedOnDate": submitted_on.isoformat(),
                "expectedDisbursementDate": expected_disbursement_on.isoformat(),
                **DATE_FORMAT,
            },
        )

    def approve_loan(self, loan_reference: str, approved_on: date, expected_disbursement_on: date) -> None:
        self._api.post(
            f"/loans/external-id/{loan_reference}",
            {
                "approvedOnDate": approved_on.isoformat(),
                "expectedDisbursementDate": expected_disbursement_on.isoformat(),
                **DATE_FORMAT,
            },
            params={"command": "approve"},
        )

    def disburse_loan(self, loan_reference: str, external_reference: str, amount: Decimal, on: date) -> None:
        self._api.post(
            f"/loans/external-id/{loan_reference}",
            {
                "externalId": external_reference,
                "actualDisbursementDate": on.isoformat(),
                "transactionAmount": plain_decimal(amount),
                "paymentTypeId": self._payment_type_id(),
                **DATE_FORMAT,
            },
            params={"command": "disburse"},
        )

    def make_repayment(self, loan_reference: str, external_reference: str, amount: Decimal, on: date) -> None:
        self._api.post(
            f"/loans/external-id/{loan_reference}/transactions",
            {
                "externalId": external_reference,
                "transactionDate": on.isoformat(),
                "transactionAmount": plain_decimal(amount),
                "paymentTypeId": self._payment_type_id(),
                **DATE_FORMAT,
            },
            params={"command": "repayment"},
        )

    def find_loan_transaction(self, loan_reference: str, external_reference: str) -> FineractLoanTransaction | None:
        transaction = self._api.get(
            f"/loans/external-id/{loan_reference}/transactions/external-id/{external_reference}"
        )
        return None if transaction is None else _loan_transaction(transaction)

    # --- Reference data, created by the bootstrap and resolved by business key ------------------------------------

    def _office_id(self) -> int:
        return self._reference_id(
            "office", lambda: self._api.get(f"/offices/external-id/{self._settings.fineract_office_external_id}")
        )

    def _loan_product_id(self) -> int:
        return self._reference_id(
            "loan_product",
            lambda: self._api.get(f"/loanproducts/external-id/{self._settings.fineract_loan_product_external_id}"),
        )

    def _payment_type_id(self) -> int:
        def find() -> dict[str, Any] | None:
            payment_types = self._api.get("/paymenttypes") or []
            name = self._settings.fineract_payment_type_name
            return next((item for item in payment_types if item.get("name") == name), None)

        return self._reference_id("payment_type", find)

    def _reference_id(self, name: str, lookup: Callable[[], dict[str, Any] | None]) -> int:
        with self._reference_lock:
            if name not in self._reference_ids:
                found = lookup()
                if found is None:
                    # Raised before anything is sent, so nothing was applied.
                    raise NotAppliedError("fineract", f"{name} is not configured in Fineract; run the bootstrap")
                self._reference_ids[name] = int(found["id"])
            return self._reference_ids[name]


def _loan(loan_reference: str, loan: dict[str, Any]) -> FineractLoan:
    status = loan.get("status", {})
    summary = loan.get("summary") or {}
    return FineractLoan(
        internal_id=int(loan["id"]),
        loan_reference=loan_reference,
        status=_LOAN_STATUSES.get(str(status.get("code")), LoanStatus.OTHER),
        fineract_status=str(status.get("value", "")),
        principal=_money(loan.get("principal")),
        balances=LoanBalances(
            principal_outstanding=_money(summary.get("principalOutstanding")),
            interest_outstanding=_money(summary.get("interestOutstanding")),
            fees_outstanding=_money(summary.get("feeChargesOutstanding")),
            penalties_outstanding=_money(summary.get("penaltyChargesOutstanding")),
            total_outstanding=_money(summary.get("totalOutstanding")),
        ),
        transactions=tuple(_loan_transaction(item) for item in loan.get("transactions") or ()),
    )


def _loan_transaction(transaction: dict[str, Any]) -> FineractLoanTransaction:
    kind = transaction.get("type", {})
    if kind.get("disbursement"):
        transaction_type = LoanTransactionType.DISBURSEMENT
    elif kind.get("repayment"):
        transaction_type = LoanTransactionType.REPAYMENT
    else:
        transaction_type = LoanTransactionType.OTHER
    return FineractLoanTransaction(
        internal_id=int(transaction["id"]),
        external_reference=transaction.get("externalId"),
        type=transaction_type,
        fineract_type=str(kind.get("value", "")),
        transaction_date=_date(transaction["date"]),
        amount=_money(transaction.get("amount")),
        principal=_money(transaction.get("principalPortion")),
        interest=_money(transaction.get("interestPortion")),
        fees=_money(transaction.get("feeChargesPortion")),
        penalties=_money(transaction.get("penaltyChargesPortion")),
        overpayment=_money(transaction.get("overpaymentPortion")),
        outstanding_after=_money(transaction.get("outstandingLoanBalance")),
        reversed=bool(transaction.get("manuallyReversed", False)),
    )


def _money(value: Any) -> Decimal:
    return Decimal(str(value)) if value is not None else Decimal(0)


def _date(value: Any) -> date:
    """Fineract serializes dates either as ``[yyyy, mm, dd]`` or as ISO strings, depending on the endpoint."""
    if isinstance(value, list):
        return date(*value)
    return date.fromisoformat(str(value))


def _error_detail(response: httpx.Response) -> tuple[str | None, str]:
    body = json_or_empty(response)
    errors = body.get("errors") or [{}]
    first = errors[0] if isinstance(errors[0], dict) else {}
    code = first.get("userMessageGlobalisationCode") or body.get("userMessageGlobalisationCode")
    message = first.get("defaultUserMessage") or body.get("defaultUserMessage") or response.text[:300]
    return code, str(message)


def _is_transient_conflict(response: httpx.Response) -> bool:
    """Fineract answers 409 when a concurrent update won (the transaction rolled back) or the loan is locked by COB."""
    code, _ = _error_detail(response)
    return code is not None and (code.startswith("error.msg.platform.service.") or code == "error.msg.loan.locked")


def _is_permission_denied(response: httpx.Response) -> bool:
    """Fineract answers 403 for broken business rules too; only this code means our user lacks a permission."""
    code, _ = _error_detail(response)
    return response.status_code == 403 and code == "error.msg.not.authorized"
