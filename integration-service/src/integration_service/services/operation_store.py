"""Operations in the database: claiming an external reference, leasing work, finding operations.

The database decides every race here. Two requests can never both own a reference: ``(type, external_reference)`` is
unique, and the business keys an operation reserves (a client uid, a loan's single approval or disbursement) are
unique too. A lease makes one executor at a time responsible for advancing an operation.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

from pydantic import BaseModel
from sqlalchemy import func, or_, select, update
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


class ReferenceReusedError(Exception):
    """The external reference already names an operation with a different payload."""

    def __init__(self, operation_type: OperationType, external_reference: str) -> None:
        super().__init__(f"{operation_type} {external_reference} was already used with a different payload")
        self.operation_type = operation_type
        self.external_reference = external_reference


class BusinessKeyTakenError(Exception):
    """A unique business key the operation needs (client uid, a loan's approval or disbursement) is already taken."""


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

    A new operation is created leased to the caller, together with whatever rows ``reserve`` adds in the same
    transaction. Reusing a reference with a different payload raises ``ReferenceReusedError``.
    """
    request_fingerprint = fingerprint(request)
    existing = find(session, operation_type, external_reference)
    if existing is not None:
        return _existing(existing, request_fingerprint)

    operation = Operation(
        type=operation_type,
        external_reference=external_reference,
        request_fingerprint=request_fingerprint,
        request=request.model_dump(mode="json"),
        client_uid=client_uid,
        loan_reference=loan_reference,
        correlation_id=current_correlation_id(),
        lease_expires_at=func.now() + timedelta(seconds=lease_seconds),
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
        raise BusinessKeyTakenError(str(error.orig)) from error
    return Claim(operation.id, is_new=True)


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


def try_lease(session: Session, operation_id: int, lease_seconds: float) -> bool:
    """Takes the operation if nobody holds it (or the holder's lease expired). Atomic: one caller wins."""
    taken = session.execute(
        update(Operation)
        .where(
            Operation.id == operation_id,
            or_(Operation.lease_expires_at.is_(None), Operation.lease_expires_at < func.now()),
        )
        .values(lease_expires_at=func.now() + timedelta(seconds=lease_seconds))
    )
    session.commit()
    return bool(taken.rowcount)


def due_for_recovery(session: Session, limit: int = 100) -> list[int]:
    """In-flight operations nobody is working on whose next attempt is due (or that never got one: a crash)."""
    return list(
        session.scalars(
            select(Operation.id)
            .where(
                Operation.status.in_(IN_FLIGHT),
                or_(Operation.next_attempt_at.is_(None), Operation.next_attempt_at <= func.now()),
                or_(Operation.lease_expires_at.is_(None), Operation.lease_expires_at < func.now()),
            )
            .order_by(Operation.id)
            .limit(limit)
        )
    )


def _existing(operation: Operation, request_fingerprint: str) -> Claim:
    if operation.request_fingerprint != request_fingerprint:
        raise ReferenceReusedError(operation.type, operation.external_reference)
    return Claim(operation.id, is_new=False)
