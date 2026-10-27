"""Part J: every failure leaves a state the executor (or recovery) can finish without double processing."""

import time
from dataclasses import replace

import pytest

from integration_service.domain.operations import OperationStatus
from integration_service.errors import ApiError, ErrorCode
from integration_service.gateways.faults import FaultConfig
from tests.component.conftest import LEASE_SECONDS, SimulatedCrash, System
from tests.component.fakes import Fault


def test_fineract_down_fails_without_side_effects_and_is_safe_to_retry(system: System) -> None:
    loan = system.open_loan()
    system.fineract.fail("find_loan_transaction", Fault.NOT_APPLIED, Fault.NOT_APPLIED)

    with pytest.raises(ApiError) as down:
        system.disburse(loan)
    failed = system.operation("DISB-1")
    retried = system.disburse(loan)

    assert (down.value.status_code, down.value.code) == (503, ErrorCode.FINERACT_UNAVAILABLE)
    assert (failed.status, failed.retryable) == (OperationStatus.FAILED, True)
    assert retried.status_code == 201
    assert system.fineract.commands("disburse_loan") == 1


def test_a_lost_answer_is_resolved_by_looking_the_operation_up(system: System) -> None:
    loan = system.open_loan()
    system.fineract.fail("disburse_loan", Fault.UNKNOWN_AFTER)

    outcome = system.disburse(loan)

    assert outcome.status_code == 201
    assert system.fineract.commands("disburse_loan") == 1
    assert len(system.erp.entries) == 1


def test_an_unconfirmed_outcome_is_never_marked_failed(system: System) -> None:
    loan = system.open_loan()
    system.fineract.fail("disburse_loan", Fault.UNKNOWN_AFTER)
    # The lookup before submitting works; after the lost answer Fineract is unreachable for the rest of the request
    # and for recovery's first attempt.
    system.fineract.fail("find_loan_transaction", Fault.PASS, Fault.NOT_APPLIED, Fault.NOT_APPLIED)

    pending = system.disburse(loan)
    in_doubt = system.operation("DISB-1").status
    system.recovery.run_once()

    assert (pending.status_code, pending.body["status"]) == (202, "FINERACT_PENDING")
    assert in_doubt is OperationStatus.FINERACT_PENDING
    assert system.operation("DISB-1").status is OperationStatus.COMPLETED
    assert system.fineract.commands("disburse_loan") == 1


def test_a_rejection_caused_by_our_own_earlier_attempt_counts_as_success(system: System) -> None:
    loan = system.open_loan()
    # The first answer is lost, the next lookup does not see the transaction yet, so the command is sent again and
    # Fineract's unique external id refuses it. The executor looks again instead of reporting a failure.
    system.fineract.fail("disburse_loan", Fault.UNKNOWN_AFTER)
    system.fineract.fail("find_loan_transaction", Fault.MISSING, Fault.MISSING)

    outcome = system.disburse(loan)

    assert outcome.status_code == 201
    assert system.fineract.commands("disburse_loan") == 1


def test_erp_down_after_fineract_success_is_posted_later_by_recovery(system: System) -> None:
    loan = system.open_loan()
    system.erp.fail("post_journal_entry", Fault.NOT_APPLIED)

    accepted = system.disburse(loan)
    waiting = system.operation("DISB-1")
    system.recovery.run_once()
    replay = system.disburse(loan)

    assert (accepted.status_code, accepted.body["status"]) == (202, "ERP_PENDING")
    assert (waiting.status, waiting.attempts) == (OperationStatus.ERP_PENDING, 1)
    assert system.operation("DISB-1").status is OperationStatus.COMPLETED
    assert replay.status_code == 200
    assert system.fineract.commands("disburse_loan") == 1
    assert len(system.erp.entries) == 1


def test_erp_timeout_after_write_does_not_duplicate_the_journal_entry(system: System) -> None:
    loan = system.open_loan()
    system.erp.fail("post_journal_entry", Fault.UNKNOWN_AFTER)

    accepted = system.disburse(loan)
    system.recovery.run_once()

    assert accepted.status_code == 202
    assert system.operation("DISB-1").status is OperationStatus.COMPLETED
    assert system.erp.calls.count("post_journal_entry") == 2  # posted twice ...
    assert len(system.erp.entries) == 1  # ... recorded once


def test_a_crash_between_fineract_and_the_erp_is_finished_after_restart(system: System) -> None:
    loan = system.open_loan()
    system.faults.configure(FaultConfig(crash_after_fineract=True))

    with pytest.raises(SimulatedCrash):
        system.disburse(loan)
    after_crash = system.operation("DISB-1").status
    resumed_while_owner_presumed_alive = system.recovery.run_once().operations_resumed
    time.sleep(LEASE_SECONDS + 0.2)  # the dead owner's lease expires
    resumed_after_lease_expiry = system.recovery.run_once().operations_resumed

    assert after_crash is OperationStatus.FINERACT_DONE
    assert (resumed_while_owner_presumed_alive, resumed_after_lease_expiry) == (0, 1)
    assert system.operation("DISB-1").status is OperationStatus.COMPLETED
    assert system.fineract.commands("disburse_loan") == 1
    assert len(system.erp.entries) == 1


def test_fineract_contradicting_the_request_needs_a_human(system: System) -> None:
    loan = system.open_loan()
    system.disburse(loan, external_reference="DISB-1", amount=5_000_000)
    system.repay(loan, "REP-1", amount=500_000)
    # Someone reused REP-1 directly in Fineract with another amount: our request and Fineract's record disagree.
    system.fineract.transactions["REP-2"] = replace(system.fineract.transactions["REP-1"], external_reference="REP-2")

    outcome = system.repay(loan, "REP-2", amount=400_000)

    assert (outcome.status_code, outcome.body["status"]) == (202, "NEEDS_RECONCILIATION")
    assert [item["external_reference"] for item in system.queries.operations(OperationStatus.NEEDS_RECONCILIATION)] == [
        "REP-2"
    ]
