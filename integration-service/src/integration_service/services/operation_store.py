"""Operations in the database: claiming an external reference, leasing work, finding operations.

The database decides every race here. Two requests can never both own a reference: ``(type, external_reference)`` is
unique (money references are unique across types), and the business keys an operation reserves (a client uid, a
loan's single approval or disbursement) are unique too.

A lease makes one executor at a time responsible for an operation. It is taken with an atomic conditional update and
identified by an owner token; the row's ``version`` (optimistic locking) makes any write by a former owner fail.
Times come from ``clock_timestamp()``, the database's wall clock, not the start of a transaction.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from pydantic import BaseModel
from sqlalchemy import ColumnElement, DateTime, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from integration_service.domain.fingerprint import fingerprint
from integration_service.domain.operations import IN_FLIGHT, OperationStatus, OperationType
from integration_service.models import Operation
from integration_service.observability import current_correlation_id

Reserve = Callable[[Session, Operation], None]


@dataclass(frozen=True)
class Claim:
    operation_id: int
    is_new: bool
    # The lease token of a new operation (its claimer executes it); None for an existing one.
    lease: str | None = None


class ReferenceReusedError(Exception):
    """The external reference already names an operation with a different payload."""

    def __init__(self, operation_type: OperationType, external_reference: str) -> None:
        super().__init__(f"{operation_type} {external_reference} was already used with a different payload")
        self.operation_type = operation_type
        self.external_reference = external_reference


class BusinessKeyTakenError(Exception):
    """A unique business key the operation needs is already taken; ``constraint`` names which one."""

    def __init__(self, error: IntegrityError) -> None:
        super().__init__(str(error.orig))
        diagnostics = getattr(error.orig, "diag", None)
        self.constraint: str | None = getattr(diagnostics, "constraint_name", None)


def lease_expiry(seconds: float) -> ColumnElement[datetime]:
    """``seconds`` from now, by the database's wall clock (``now()`` would be the transaction's start)."""
    return func.clock_timestamp(type_=DateTime(timezone=True)) + timedelta(seconds=seconds)


def claim(
    session: Session,
    operation_type: OperationType,
    external_reference: str,
    request: BaseModel,
    *,
    lease_seconds: float,
    client_uid: str | None = None,
    loan_reference: str | None = None,
    reserve: Reserve | None = None,
) -> Claim:
    """Registers a new operation, or identifies the existing one for this reference.

    The fingerprint covers what the partner asked for: the body and the loan it targets. A new operation is created
    leased to the caller, with whatever rows ``reserve`` adds, in one transaction.
    """
    request_fingerprint = fingerprint(request, loan_reference=loan_reference)
    existing = find(session, operation_type, external_reference)
    if existing is not None:
        return _existing(existing, request_fingerprint)

    lease = str(uuid.uuid4())
    operation = Operation(
        type=operation_type,
        external_reference=external_reference,
        request_fingerprint=request_fingerprint,
        request=request.model_dump(mode="json"),
        client_uid=client_uid,
        loan_reference=loan_reference,
        correlation_id=current_correlation_id(),
        lease_owner=lease,
        lease_expires_at=lease_expiry(lease_seconds),
    )
    session.add(operation)
    try:
        if reserve is not None:
            reserve(session, operation)
        session.commit()
    except IntegrityError as error:
        session.rollback()
        winner = find(session, operation_type, external_reference)
        if winner is not None:
            return _existing(winner, request_fingerprint)
        raise BusinessKeyTakenError(error) from error
    return Claim(operation.id, is_new=True, lease=lease)


def find(session: Session, operation_type: OperationType, external_reference: str) -> Operation | None:
    return session.scalar(
        select(Operation).where(Operation.type == operation_type, Operation.external_reference == external_reference)
    )


def active_for_loan(session: Session, operation_type: OperationType, loan_reference: str) -> Operation | None:
    """The operation holding a loan's once-only slot (approval or disbursement), if any."""
    return session.scalar(
        select(Operation).where(
            Operation.type == operation_type,
            Operation.loan_reference == loan_reference,
            Operation.status != OperationStatus.FAILED,
        )
    )


def try_lease(
    session: Session, operation_id: int, lease_seconds: float, *, only_retryable_failure: bool = False
) -> str | None:
    """Takes the operation if nobody holds it (or the holder's lease expired). Returns the lease token, or None.

    With ``only_retryable_failure`` the operation must also still be a retryable FAILED one: checked in the same
    statement, so a replay that waited for another executor cannot take an operation that has meanwhile finished.
    """
    lease = str(uuid.uuid4())
    conditions = [
        Operation.id == operation_id,
        or_(Operation.lease_expires_at.is_(None), Operation.lease_expires_at < func.clock_timestamp()),
    ]
    if only_retryable_failure:
        conditions += [Operation.status == OperationStatus.FAILED, Operation.retryable.is_(True)]
    taken = session.execute(
        update(Operation)
        .where(*conditions)
        .values(lease_owner=lease, lease_expires_at=lease_expiry(lease_seconds), version=Operation.version + 1)
        .execution_options(synchronize_session=False)
    )
    session.commit()
    return lease if taken.rowcount else None


def release(session: Session, operation_id: int, lease: str) -> None:
    """Gives the operation up, if this token still holds it."""
    session.execute(
        update(Operation)
        .where(Operation.id == operation_id, Operation.lease_owner == lease)
        .values(lease_owner=None, lease_expires_at=None, version=Operation.version + 1)
        .execution_options(synchronize_session=False)
    )
    session.commit()


def due_for_recovery(session: Session, limit: int = 100) -> list[int]:
    """In-flight operations nobody is working on whose next attempt is due (or that never got one: a crash)."""
    return list(
        session.scalars(
            select(Operation.id)
            .where(
                Operation.status.in_(IN_FLIGHT),
                or_(Operation.next_attempt_at.is_(None), Operation.next_attempt_at <= func.clock_timestamp()),
                or_(Operation.lease_expires_at.is_(None), Operation.lease_expires_at < func.clock_timestamp()),
            )
            .order_by(Operation.id)
            .limit(limit)
        )
    )


def _existing(operation: Operation, request_fingerprint: str) -> Claim:
    if operation.request_fingerprint != request_fingerprint:
        raise ReferenceReusedError(operation.type, operation.external_reference)
    return Claim(operation.id, is_new=False)
