"""Part I at component level: one approval and one disbursement per loan, whatever the references."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date

import pytest

from integration_service.commands import ApproveLoan
from integration_service.errors import ApiError, ErrorCode
from integration_service.services.partner import Outcome
from tests.component.conftest import System
from tests.component.fakes import Fault


def outcome_or_error(call: object) -> int | ErrorCode:
    try:
        result = call()  # type: ignore[operator]
    except ApiError as error:
        return error.code
    assert isinstance(result, Outcome)
    return result.status_code


def test_parallel_disbursements_with_one_reference_execute_once(system: System) -> None:
    loan = system.open_loan()

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(lambda _: outcome_or_error(lambda: system.disburse(loan)), range(10)))

    assert results.count(201) == 1
    assert set(results) <= {200, 201, 202}
    assert system.fineract.commands("disburse_loan") == 1
    assert len(system.erp.entries) == 1


def test_parallel_disbursements_with_different_references_execute_once(system: System) -> None:
    loan = system.open_loan()

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(lambda i: outcome_or_error(lambda: system.disburse(loan, f"DISB-{i}")), range(10)))

    assert results.count(201) == 1
    assert results.count(ErrorCode.LOAN_ALREADY_DISBURSED) == 9
    assert system.fineract.commands("disburse_loan") == 1


def test_a_disbursement_that_never_reached_fineract_does_not_hold_the_loan(system: System) -> None:
    loan = system.open_loan()
    system.fineract.fail("find_loan_transaction", Fault.NOT_APPLIED, Fault.NOT_APPLIED)
    with pytest.raises(ApiError):
        system.disburse(loan, "DISB-1")

    assert system.disburse(loan, "DISB-2").status_code == 201


def test_a_retry_cannot_take_a_slot_another_reference_holds(system: System) -> None:
    loan = system.open_loan()
    system.fineract.fail("find_loan_transaction", Fault.NOT_APPLIED, Fault.NOT_APPLIED)
    with pytest.raises(ApiError):
        system.disburse(loan, "DISB-1")
    system.disburse(loan, "DISB-2")

    with pytest.raises(ApiError) as retry:
        system.disburse(loan, "DISB-1")

    assert retry.value.code is ErrorCode.LOAN_ALREADY_DISBURSED
    assert system.fineract.commands("disburse_loan") == 1


def test_a_loan_is_approved_once(system: System) -> None:
    loan = system.open_loan()

    with pytest.raises(ApiError) as second:
        system.partner.approve_loan(loan, ApproveLoan(external_reference="APR-2", approved_on=date(2026, 1, 15)))

    assert second.value.code is ErrorCode.LOAN_ALREADY_APPROVED


def test_repayments_with_different_references_all_apply(system: System) -> None:
    loan = system.open_loan()
    system.disburse(loan)

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(lambda i: outcome_or_error(lambda: system.repay(loan, f"REP-{i}")), range(10)))

    assert results == [201] * 10
    assert system.fineract.commands("make_repayment") == 10
    assert len(system.erp.entries) == 11
