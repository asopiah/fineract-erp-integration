"""Persistent state of the integration service.

Fineract's numeric ids are stored only in ``fineract_*_id`` columns: they let the service talk to Fineract and appear
in logs, and are never part of the partner API.
"""

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Sequence,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from integration_service.database import Base
from integration_service.domain.fineract import LoanTransactionType
from integration_service.domain.gl_accounts import SyncStatus
from integration_service.domain.operations import OperationStatus, OperationType

MONEY = Numeric(19, 6)

LOAN_REFERENCE_SEQUENCE = Sequence("loan_reference_seq", metadata=Base.metadata)
MONEY_REFERENCE_INDEX = "uq_operations_money_reference"


def _enum(enum: type[StrEnum]) -> Enum:
    """Stored as the enum's string value (a VARCHAR), so the database stays readable without a type registry."""
    return Enum(
        enum, native_enum=False, length=32, validate_strings=True, values_callable=lambda e: [m.value for m in e]
    )


class Operation(Base):
    """One state-changing partner request, keyed by (type, external_reference): the idempotency record and the unit
    of work the executor drives through the state machine."""

    __tablename__ = "operations"
    __table_args__ = (
        UniqueConstraint("type", "external_reference", name="uq_operations_type_external_reference"),
        # A loan is approved once and disbursed once, whatever references callers use. FAILED operations never reached
        # Fineract, so they do not hold the slot.
        Index(
            "uq_operations_one_active_per_loan",
            "type",
            "loan_reference",
            unique=True,
            postgresql_where=text("type IN ('LOAN_APPROVAL', 'DISBURSEMENT') AND status <> 'FAILED'"),
        ),
        # Fineract (m_loan_transaction.external_id) and the ERP (journal external_reference) each have one namespace
        # for money references, so a disbursement and a repayment cannot share one either.
        Index(
            MONEY_REFERENCE_INDEX,
            "external_reference",
            unique=True,
            postgresql_where=text("type IN ('DISBURSEMENT', 'REPAYMENT')"),
        ),
        Index("ix_operations_status_next_attempt_at", "status", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[OperationType] = mapped_column(_enum(OperationType))
    external_reference: Mapped[str] = mapped_column(String(100))
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    request: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[OperationStatus] = mapped_column(_enum(OperationStatus), default=OperationStatus.RECEIVED)
    client_uid: Mapped[str | None] = mapped_column(String(64))
    loan_reference: Mapped[str | None] = mapped_column(String(32), index=True)
    correlation_id: Mapped[str | None] = mapped_column(String(100))

    # The body a replay returns once the operation has finished (success or definitive rejection).
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # FAILED and nothing reached Fineract: the same request may be sent again.
    retryable: Mapped[bool] = mapped_column(Boolean, default=False)

    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The executor responsible for the operation, and until when. Taken and released only by its token.
    lease_owner: Mapped[str | None] = mapped_column(String(36))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, server_default="1")
    # Optimistic locking: every ORM update checks and bumps ``version``, so a writer holding stale state fails
    # instead of overwriting a newer status.
    __mapper_args__ = {"version_id_col": version}  # noqa: RUF012

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Client(Base):
    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_uid: Mapped[str] = mapped_column(String(64), unique=True)
    external_reference: Mapped[str] = mapped_column(String(100), unique=True)
    first_name: Mapped[str] = mapped_column(String(100))
    last_name: Mapped[str] = mapped_column(String(100))
    fineract_client_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Loan(Base):
    __tablename__ = "loans"

    id: Mapped[int] = mapped_column(primary_key=True)
    loan_reference: Mapped[str] = mapped_column(String(32), unique=True)
    external_reference: Mapped[str] = mapped_column(String(100), unique=True)
    client_uid: Mapped[str] = mapped_column(ForeignKey("clients.client_uid"), index=True)
    principal: Mapped[Decimal] = mapped_column(MONEY)
    annual_interest_rate: Mapped[Decimal] = mapped_column(Numeric(9, 4))
    term_months: Mapped[int] = mapped_column(Integer)
    submitted_on: Mapped[date] = mapped_column(Date)
    expected_disbursement_on: Mapped[date] = mapped_column(Date)
    fineract_loan_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LoanTransaction(Base):
    """A money movement as Fineract recorded it, and the ERP journal entry that mirrors it."""

    __tablename__ = "loan_transactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    external_reference: Mapped[str] = mapped_column(String(100), unique=True)
    loan_reference: Mapped[str] = mapped_column(ForeignKey("loans.loan_reference"), index=True)
    type: Mapped[LoanTransactionType] = mapped_column(_enum(LoanTransactionType))
    transaction_date: Mapped[date] = mapped_column(Date)
    amount: Mapped[Decimal] = mapped_column(MONEY)
    principal_portion: Mapped[Decimal] = mapped_column(MONEY)
    interest_portion: Mapped[Decimal] = mapped_column(MONEY)
    fee_portion: Mapped[Decimal] = mapped_column(MONEY)
    penalty_portion: Mapped[Decimal] = mapped_column(MONEY)
    overpayment_portion: Mapped[Decimal] = mapped_column(MONEY)
    outstanding_after: Mapped[Decimal] = mapped_column(MONEY)
    fineract_transaction_id: Mapped[int] = mapped_column(Integer)
    erp_entry_number: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GlAccountSync(Base):
    """Mapping of one ERP account to its Fineract GL account. Create-only: SYNC_PENDING → SYNCED (or FAILED)."""

    __tablename__ = "gl_account_syncs"

    id: Mapped[int] = mapped_column(primary_key=True)
    external_reference: Mapped[str] = mapped_column(String(100), unique=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    erp_type: Mapped[str] = mapped_column(String(16))
    status: Mapped[SyncStatus] = mapped_column(_enum(SyncStatus), default=SyncStatus.SYNC_PENDING)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(String(500))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fineract_gl_account_id: Mapped[int | None] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
