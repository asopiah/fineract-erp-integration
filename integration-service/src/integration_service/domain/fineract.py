"""What Fineract recorded, as typed values. The gateway builds these from Fineract's JSON.

Fineract's numeric ids appear here only as ``internal_id``: they are kept for logs and never leave the service.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum


class LoanStatus(StrEnum):
    SUBMITTED = "SUBMITTED"
    APPROVED = "APPROVED"
    ACTIVE = "ACTIVE"
    CLOSED = "CLOSED"
    OVERPAID = "OVERPAID"
    REJECTED = "REJECTED"
    WITHDRAWN = "WITHDRAWN"
    WRITTEN_OFF = "WRITTEN_OFF"
    OTHER = "OTHER"

    @property
    def is_approved_or_later(self) -> bool:
        return self in (LoanStatus.APPROVED, LoanStatus.ACTIVE, LoanStatus.CLOSED, LoanStatus.OVERPAID)


class LoanTransactionType(StrEnum):
    DISBURSEMENT = "DISBURSEMENT"
    REPAYMENT = "REPAYMENT"
    OTHER = "OTHER"


@dataclass(frozen=True)
class FineractClient:
    internal_id: int
    client_uid: str
    display_name: str
    active: bool
    status: str


@dataclass(frozen=True)
class FineractLoanTransaction:
    internal_id: int
    external_reference: str | None
    type: LoanTransactionType
    fineract_type: str  # Fineract's own name for it, e.g. "Repayment", "Accrual"
    transaction_date: date
    amount: Decimal
    principal: Decimal
    interest: Decimal
    fees: Decimal
    penalties: Decimal
    overpayment: Decimal
    outstanding_after: Decimal
    reversed: bool
    reversal_external_reference: str | None = None  # the reference the reversal was submitted with


@dataclass(frozen=True)
class LoanBalances:
    principal_outstanding: Decimal
    interest_outstanding: Decimal
    fees_outstanding: Decimal
    penalties_outstanding: Decimal
    total_outstanding: Decimal


@dataclass(frozen=True)
class FineractLoan:
    internal_id: int
    loan_reference: str
    status: LoanStatus
    fineract_status: str
    principal: Decimal
    balances: LoanBalances
    transactions: tuple[FineractLoanTransaction, ...]
