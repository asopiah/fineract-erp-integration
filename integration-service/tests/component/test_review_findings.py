"""Failure modes found in an independent review, each pinned by a test."""

import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import update

from integration_service.domain.operations import OperationStatus, OperationType
from integration_service.errors import ApiError, ErrorCode
from integration_service.models import Operation
from integration_service.services import operation_store
from tests.component.conftest import LEASE_SECONDS, System
from tests.component.fakes import Fault


def test_a_submit_that_cannot_be_confirmed_is_never_reported_as_not_processed(system: System) -> None:
    loan = system.open_loan()
    # The lookup before submitting works and the command is applied; Fineract then stops answering lookups.
    system.fineract.fail("find_loan_transaction", Fault.PASS, Fault.NOT_APPLIED, Fault.NOT_APPLIED)

    pending = system.disburse(loan)
    in_doubt = system.operation("DISB-1").status
    system.recovery.run_once()

    assert (pending.status_code, pending.body["status"]) == (202, "FINERACT_PENDING")
    assert in_doubt is OperationStatus.FINERACT_PENDING
    assert system.operation("DISB-1").status is OperationStatus.COMPLETED
    assert system.fineract.commands("disburse_loan") == 1


def test_the_same_reference_on_another_loan_is_a_conflict(system: System) -> None:
    first, second = system.open_loan(1), system.open_loan(2)
    system.disburse(first)
    system.disburse(second, external_reference="DISB-2")
    system.repay(first, "REP-1")

    with pytest.raises(ApiError) as raised:
        system.repay(second, "REP-1")

    assert (raised.value.status_code, raised.value.code) == (409, ErrorCode.IDEMPOTENCY_CONFLICT)


def test_a_money_reference_cannot_name_both_a_disbursement_and_a_repayment(system: System) -> None:
    loan = system.open_loan()
    system.disburse(loan, external_reference="MONEY-1")

    with pytest.raises(ApiError) as raised:
        system.repay(loan, "MONEY-1")

    assert (raised.value.status_code, raised.value.code) == (409, ErrorCode.IDEMPOTENCY_CONFLICT)
    assert system.fineract.commands("make_repayment") == 0


def test_an_executor_that_lost_its_lease_stops_without_overwriting(system: System) -> None:
    loan = system.open_loan()
    takeover: dict[str, str | None] = {}

    def recovery_takes_over() -> None:  # the first executor's lease expires while it is waiting on Fineract
        with system.session_factory() as session:
            session.execute(
                update(Operation)
                .where(Operation.external_reference == "DISB-1")
                .values(lease_expires_at=None)
                .execution_options(synchronize_session=False)
            )
            session.commit()
            operation_id = system.operation("DISB-1").id
            takeover["lease"] = operation_store.try_lease(session, operation_id, LEASE_SECONDS)

    system.fineract.during_next("find_loan_transaction", recovery_takes_over)
    first = system.disburse(loan)
    stalled = system.operation("DISB-1")

    # The first executor's command did reach Fineract; it was stopped at its next write, without recording anything
    # or releasing the new owner's lease.
    assert takeover["lease"] is not None
    assert first.status_code == 202
    assert (stalled.status, stalled.lease_owner) == (OperationStatus.FINERACT_PENDING, takeover["lease"])
    time.sleep(LEASE_SECONDS + 0.2)
    system.recovery.run_once()
    # The new owner finds the transaction by lookup instead of submitting it again.
    assert system.operation("DISB-1").status is OperationStatus.COMPLETED
    assert system.fineract.commands("disburse_loan") == 1


def test_an_unparseable_answer_is_deferred_not_retried_forever(system: System) -> None:
    loan = system.open_loan()
    system.fineract.fail("find_loan_transaction", Fault.GARBAGE, Fault.GARBAGE)

    outcome = system.disburse(loan)
    operation = system.operation("DISB-1")

    assert outcome.status_code == 202
    assert (operation.status, operation.attempts) == (OperationStatus.FINERACT_PENDING, 1)
    assert operation.next_attempt_at is not None


def test_concurrent_replays_of_a_retryable_failure_execute_once(system: System) -> None:
    loan = system.open_loan()
    system.fineract.fail("find_loan_transaction", Fault.NOT_APPLIED, Fault.NOT_APPLIED)
    with pytest.raises(ApiError):
        system.disburse(loan)

    with ThreadPoolExecutor(max_workers=5) as pool:
        statuses = sorted(pool.map(lambda _: system.disburse(loan).status_code, range(5)))

    assert statuses.count(201) == 1
    assert set(statuses) <= {200, 201, 202}
    assert system.fineract.commands("disburse_loan") == 1


def test_an_erp_rejection_can_be_repaired_explicitly(system: System) -> None:
    loan = system.open_loan()
    system.erp.fail("post_journal_entry", Fault.REJECTED)

    stranded = system.disburse(loan)
    repaired = system.recovery.resume_erp_posting(OperationType.DISBURSEMENT, "DISB-1")

    assert (stranded.status_code, stranded.body["status"]) == (202, "NEEDS_RECONCILIATION")
    assert "ERP rejected" in stranded.body["detail"]
    assert repaired is OperationStatus.COMPLETED
    assert len(system.erp.entries) == 1
    with pytest.raises(ApiError) as again:  # idempotent: nothing left to repair
        system.recovery.resume_erp_posting(OperationType.DISBURSEMENT, "DISB-1")
    assert again.value.code is ErrorCode.NOT_RESUMABLE


def test_repair_is_refused_while_fineracts_side_is_unsettled(system: System) -> None:
    loan = system.open_loan()
    system.disburse(loan, external_reference="DISB-1")
    system.repay(loan, "REP-1", amount=500_000)
    system.fineract.transactions["REP-2"] = system.fineract.transactions["REP-1"]  # contradicts the request below
    system.repay(loan, "REP-2", amount=400_000)

    with pytest.raises(ApiError) as refused:
        system.recovery.resume_erp_posting(OperationType.REPAYMENT, "REP-2")

    assert refused.value.code is ErrorCode.NOT_RESUMABLE
