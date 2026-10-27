"""Operation types and the lifecycle every state-changing request follows.

    RECEIVED ─▶ FINERACT_PENDING ─▶ FINERACT_DONE ─▶ ERP_PENDING ─▶ COMPLETED
                     │   ▲                │                 │
                     ▼   │                └──▶ COMPLETED    ▼
                   FAILED (retryable)        (no journal)  NEEDS_RECONCILIATION ─▶ ERP_PENDING (operator repair)
                     │
                     └─▶ NEEDS_RECONCILIATION (Fineract's record contradicts the request)

FINERACT_PENDING means "Fineract may or may not have it": it is left only by looking the operation up in Fineract.
"""

from enum import StrEnum


class OperationType(StrEnum):
    CLIENT_CREATION = "CLIENT_CREATION"
    LOAN_APPLICATION = "LOAN_APPLICATION"
    LOAN_APPROVAL = "LOAN_APPROVAL"
    DISBURSEMENT = "DISBURSEMENT"
    REPAYMENT = "REPAYMENT"

    @property
    def posts_journal(self) -> bool:
        """Money movements are mirrored into the ERP; lifecycle events are not."""
        return self in (OperationType.DISBURSEMENT, OperationType.REPAYMENT)

    @property
    def once_per_loan(self) -> bool:
        """At most one active operation of this type per loan, whatever its external reference."""
        return self in (OperationType.LOAN_APPROVAL, OperationType.DISBURSEMENT)


class OperationStatus(StrEnum):
    RECEIVED = "RECEIVED"
    FINERACT_PENDING = "FINERACT_PENDING"
    FINERACT_DONE = "FINERACT_DONE"
    ERP_PENDING = "ERP_PENDING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    NEEDS_RECONCILIATION = "NEEDS_RECONCILIATION"

    @property
    def in_flight(self) -> bool:
        """Work remains that the executor (a request or recovery) can do without a human."""
        return self in IN_FLIGHT


IN_FLIGHT = frozenset(
    {
        OperationStatus.RECEIVED,
        OperationStatus.FINERACT_PENDING,
        OperationStatus.FINERACT_DONE,
        OperationStatus.ERP_PENDING,
    }
)

ALLOWED_TRANSITIONS: dict[OperationStatus, frozenset[OperationStatus]] = {
    OperationStatus.RECEIVED: frozenset({OperationStatus.FINERACT_PENDING}),
    OperationStatus.FINERACT_PENDING: frozenset(
        {OperationStatus.FINERACT_DONE, OperationStatus.FAILED, OperationStatus.NEEDS_RECONCILIATION}
    ),
    OperationStatus.FINERACT_DONE: frozenset({OperationStatus.ERP_PENDING, OperationStatus.COMPLETED}),
    OperationStatus.ERP_PENDING: frozenset({OperationStatus.COMPLETED, OperationStatus.NEEDS_RECONCILIATION}),
    # Only a failure that never reached Fineract is retried; the executor checks that before moving back.
    OperationStatus.FAILED: frozenset({OperationStatus.FINERACT_PENDING}),
    OperationStatus.NEEDS_RECONCILIATION: frozenset({OperationStatus.ERP_PENDING}),
    OperationStatus.COMPLETED: frozenset(),
}


class IllegalTransitionError(Exception):
    def __init__(self, current: OperationStatus, target: OperationStatus) -> None:
        super().__init__(f"illegal operation transition {current} -> {target}")
        self.current = current
        self.target = target


def ensure_transition(current: OperationStatus, target: OperationStatus) -> None:
    if target not in ALLOWED_TRANSITIONS[current]:
        raise IllegalTransitionError(current, target)
