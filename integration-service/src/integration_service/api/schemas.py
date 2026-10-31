"""Response bodies, declared for the OpenAPI documentation. Money is a decimal string, never a float."""

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from integration_service.commands import ExternalReference


class ErrorDetail(BaseModel):
    code: str = Field(examples=["IDEMPOTENCY_CONFLICT"])
    message: str
    correlation_id: str | None
    details: dict[str, Any] | list[Any] | None = None


class ErrorOut(BaseModel):
    error: ErrorDetail


class PendingOut(BaseModel):
    external_reference: str
    operation: str = Field(examples=["DISBURSEMENT"])
    status: str = Field(examples=["ERP_PENDING"])
    detail: str


class ClientOut(BaseModel):
    external_reference: str
    client_uid: str
    first_name: str
    last_name: str


class FineractClientView(BaseModel):
    exists: bool
    active: bool
    status: str
    display_name: str
    name_matches: bool


class ClientView(ClientOut):
    fineract: FineractClientView | None


class LoanApplicationOut(BaseModel):
    external_reference: str
    loan_reference: str = Field(examples=["LN-2026-000001"])
    client_uid: str
    principal: str
    annual_interest_rate: str
    term_months: int
    submitted_on: date
    expected_disbursement_on: date


class ApprovalOut(BaseModel):
    external_reference: str
    loan_reference: str
    approved_on: date


class TransactionOut(BaseModel):
    """A money movement as Fineract recorded it, with the ERP journal entry that mirrors it."""

    external_reference: str
    loan_reference: str
    type: str = Field(examples=["REPAYMENT"])
    transaction_date: date
    amount: str
    principal_portion: str
    interest_portion: str
    fee_portion: str
    penalty_portion: str
    overpayment_portion: str
    outstanding_after: str
    erp_entry_number: str | None = Field(examples=["JE-000002"])


class ReversalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    external_reference: ExternalReference = Field(examples=["REV-0001"])


class ReversalOut(BaseModel):
    """A repayment undone in Fineract, and the ERP entry that reverses its journal entry."""

    external_reference: str
    loan_reference: str
    repayment_reference: str
    reversed_amount: str
    erp_entry_number: str | None = Field(examples=["JE-000003"])


class Outstanding(BaseModel):
    principal: str
    interest: str
    fees: str
    penalties: str
    total: str


class LoanTransactionView(BaseModel):
    external_reference: str | None
    type: str = Field(examples=["REPAYMENT"], description="DISBURSEMENT, REPAYMENT, or OTHER (see fineract_type)")
    fineract_type: str = Field(examples=["Repayment"])
    transaction_date: date
    amount: str
    principal_portion: str
    interest_portion: str
    fee_portion: str
    penalty_portion: str
    overpayment_portion: str
    outstanding_after: str
    reversed: bool


class LoanView(BaseModel):
    loan_reference: str
    external_reference: str
    client_uid: str
    principal: str
    annual_interest_rate: str
    term_months: int
    submitted_on: date
    status: str | None = Field(examples=["ACTIVE"])
    fineract_status: str | None = None
    outstanding: Outstanding | None = None
    transactions: list[LoanTransactionView] = []


class TransactionView(BaseModel):
    """The lifecycle of one money movement, from request to ERP entry."""

    external_reference: str
    type: str
    loan_reference: str | None
    client_uid: str | None
    status: str
    correlation_id: str | None
    attempts: int
    next_attempt_at: datetime | None
    error: dict[str, Any] | None
    requested: dict[str, Any]
    recorded: TransactionOut | None
    created_at: datetime
    updated_at: datetime


class GlAccountSyncRequest(BaseModel):
    external_references: list[ExternalReference] | None = Field(
        default=None, description="Synchronize only these ERP accounts; all of them when omitted."
    )


class GlAccountSyncOut(BaseModel):
    external_reference: str
    code: str
    name: str
    erp_type: str
    status: str = Field(examples=["SYNCED"])
    attempts: int
    last_error: str | None


class ReconciliationRequest(BaseModel):
    from_date: date | None = None
    to_date: date | None = None
    external_references: list[ExternalReference] | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def dates_in_order(self) -> "ReconciliationRequest":
        if self.from_date is not None and self.to_date is not None and self.from_date > self.to_date:
            raise ValueError("from_date cannot be after to_date")
        return self


class ReconciliationItem(BaseModel):
    external_reference: str
    loan_reference: str | None
    outcome: str = Field(examples=["MATCHED"])
    note: str | None
    fineract: dict[str, Any] | None
    erp: dict[str, Any] | None


class ReconciliationReport(BaseModel):
    criteria: dict[str, Any]
    summary: dict[str, int]
    items: list[ReconciliationItem]
