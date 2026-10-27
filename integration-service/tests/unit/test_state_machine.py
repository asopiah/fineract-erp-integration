import pytest

from integration_service.domain.operations import (
    ALLOWED_TRANSITIONS,
    IllegalTransitionError,
    OperationStatus,
    OperationType,
    ensure_transition,
)

S = OperationStatus


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (S.RECEIVED, S.FINERACT_PENDING),
        (S.FINERACT_PENDING, S.FINERACT_DONE),
        (S.FINERACT_PENDING, S.FAILED),
        (S.FINERACT_PENDING, S.NEEDS_RECONCILIATION),
        (S.FINERACT_DONE, S.ERP_PENDING),
        (S.FINERACT_DONE, S.COMPLETED),
        (S.ERP_PENDING, S.COMPLETED),
        (S.ERP_PENDING, S.NEEDS_RECONCILIATION),
        (S.FAILED, S.FINERACT_PENDING),
        (S.NEEDS_RECONCILIATION, S.ERP_PENDING),
    ],
)
def test_allowed_transitions(current: OperationStatus, target: OperationStatus) -> None:
    ensure_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (S.RECEIVED, S.FINERACT_DONE),  # Fineract cannot be skipped
        (S.FINERACT_PENDING, S.ERP_PENDING),  # nor its confirmation
        (S.FINERACT_DONE, S.FAILED),  # money moved: never "failed"
        (S.ERP_PENDING, S.FAILED),
        (S.COMPLETED, S.FINERACT_PENDING),  # completed is final
        (S.NEEDS_RECONCILIATION, S.COMPLETED),  # only via an explicit ERP re-post
    ],
)
def test_illegal_transitions_are_rejected(current: OperationStatus, target: OperationStatus) -> None:
    with pytest.raises(IllegalTransitionError):
        ensure_transition(current, target)


def test_every_status_has_a_transition_entry() -> None:
    assert set(ALLOWED_TRANSITIONS) == set(OperationStatus)


def test_only_money_movements_post_journals() -> None:
    assert {t for t in OperationType if t.posts_journal} == {OperationType.DISBURSEMENT, OperationType.REPAYMENT}


def test_approval_and_disbursement_are_once_per_loan() -> None:
    assert {t for t in OperationType if t.once_per_loan} == {OperationType.LOAN_APPROVAL, OperationType.DISBURSEMENT}
