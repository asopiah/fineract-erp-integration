"""Repayment reversal: undone once in Fineract, mirrored once in the ERP, whatever fails in between."""

import pytest

from integration_service.domain.operations import OperationStatus
from integration_service.errors import ApiError, ErrorCode
from tests.component.conftest import System
from tests.component.fakes import Fault


def repaid_loan(system: System) -> str:
    loan = system.open_loan()
    system.disburse(loan)
    system.repay(loan, "REP-1", amount=500_000)
    return loan


def test_a_reversal_undoes_the_repayment_and_posts_the_swapped_entry(system: System) -> None:
    loan = repaid_loan(system)

    outcome = system.reverse(loan, "REP-1")

    assert outcome.status_code == 201
    assert outcome.body["repayment_reference"] == "REP-1"
    assert system.fineract.transactions["REP-1"].reversed
    assert system.fineract.transactions["REP-1"].reversal_external_reference == "REV-1"
    repayment, reversal = system.erp.entries["REP-1"], system.erp.entries["REP-1:REVERSAL"]
    assert [(line.account_code, line.debit, line.credit) for line in reversal.lines] == [
        (line.account_code, line.credit, line.debit) for line in repayment.lines
    ]


def test_a_replayed_reversal_changes_nothing(system: System) -> None:
    loan = repaid_loan(system)
    first = system.reverse(loan, "REP-1")

    replay = system.reverse(loan, "REP-1")

    assert (replay.status_code, replay.body) == (200, first.body)
    assert system.fineract.commands("reverse_repayment") == 1
    assert list(system.erp.entries).count("REP-1:REVERSAL") == 1


def test_a_lost_answer_is_found_by_the_reversal_reference_not_resent(system: System) -> None:
    loan = repaid_loan(system)
    system.fineract.fail("reverse_repayment", Fault.UNKNOWN_AFTER)

    outcome = system.reverse(loan, "REP-1")

    assert outcome.status_code == 201
    assert system.fineract.commands("reverse_repayment") == 1


def test_an_erp_outage_leaves_the_reversing_entry_to_recovery(system: System) -> None:
    loan = repaid_loan(system)
    system.erp.fail("post_journal_entry", Fault.NOT_APPLIED)

    pending = system.reverse(loan, "REP-1")
    system.recovery.run_once()

    assert pending.status_code == 202
    assert system.operation("REV-1").status is OperationStatus.COMPLETED
    assert "REP-1:REVERSAL" in system.erp.entries


def test_a_second_reversal_of_the_same_repayment_is_refused(system: System) -> None:
    loan = repaid_loan(system)
    system.reverse(loan, "REP-1")

    with pytest.raises(ApiError) as refused:
        system.reverse(loan, "REP-1", external_reference="REV-2")

    assert (refused.value.status_code, refused.value.code) == (409, ErrorCode.REPAYMENT_ALREADY_REVERSED)
    assert system.fineract.commands("reverse_repayment") == 1


def test_only_a_known_repayment_of_this_loan_can_be_reversed(system: System) -> None:
    loan = repaid_loan(system)

    with pytest.raises(ApiError) as unknown:
        system.reverse(loan, "DISB-1")

    assert (unknown.value.status_code, unknown.value.code) == (404, ErrorCode.NOT_FOUND)
