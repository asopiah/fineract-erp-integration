"""Failure modes found in an independent review, each pinned by a test."""

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date
from decimal import Decimal
from typing import Any, cast

import httpx
import pytest
from sqlalchemy import select, update

from integration_service.commands import CreateClient
from integration_service.database import create_session_factory
from integration_service.domain.accounting import LedgerAccounts
from integration_service.domain.operations import OperationStatus, OperationType
from integration_service.errors import ApiError, ErrorCode
from integration_service.gateways.fineract import FineractGateway
from integration_service.models import Client, Operation
from integration_service.services import operation_store
from integration_service.services.executor import OperationExecutor
from integration_service.services.handlers import build_handlers
from integration_service.services.partner import PartnerService
from integration_service.settings import Settings
from tests.component.conftest import LEASE_SECONDS, System
from tests.component.fakes import FakeErp, Fault


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

    # The first executor renews its lease before submitting, notices the takeover and stops, without submitting,
    # recording anything or releasing the new owner's lease.
    assert takeover["lease"] is not None
    assert first.status_code == 202
    assert (stalled.status, stalled.lease_owner) == (OperationStatus.FINERACT_PENDING, takeover["lease"])
    assert system.fineract.commands("disburse_loan") == 0
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


def test_an_unexpected_erp_failure_is_deferred_then_handed_to_a_human(system: System) -> None:
    loan = system.open_loan()
    system.erp.fail("post_journal_entry", *[Fault.GARBAGE] * 30)

    outcome = system.disburse(loan)
    deferred = system.operation("DISB-1")
    for _ in range(30):
        if system.operation("DISB-1").status is not OperationStatus.ERP_PENDING:
            break
        system.recovery.run_once()

    assert (outcome.status_code, outcome.body["status"]) == (202, "ERP_PENDING")
    assert (deferred.attempts, deferred.next_attempt_at is not None) == (1, True)
    assert system.operation("DISB-1").status is OperationStatus.NEEDS_RECONCILIATION


@pytest.mark.parametrize("change", [{"reversed": True}, {"principal": Decimal(1)}], ids=["reversed", "changed"])
def test_repair_is_refused_when_fineract_no_longer_matches_the_stored_record(
    system: System, change: dict[str, Any]
) -> None:
    loan = system.open_loan()
    system.erp.fail("post_journal_entry", Fault.REJECTED)
    system.disburse(loan)
    system.fineract.transactions["DISB-1"] = replace(system.fineract.transactions["DISB-1"], **change)

    with pytest.raises(ApiError) as refused:
        system.recovery.resume_erp_posting(OperationType.DISBURSEMENT, "DISB-1")

    assert refused.value.code is ErrorCode.NOT_RESUMABLE
    assert system.operation("DISB-1").status is OperationStatus.NEEDS_RECONCILIATION
    assert system.erp.entries == {}


def test_refused_fineract_credentials_keep_the_reservation_and_stay_retryable(clean_database: str) -> None:
    def fineract(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(401, json={"defaultUserMessage": "Unauthenticated"})
        if "/offices/" in request.url.path:
            return httpx.Response(200, json={"id": 1})
        return httpx.Response(404, json={})

    settings = Settings(database_url=clean_database, fineract_attempts=1)
    session_factory = create_session_factory(clean_database)
    gateway = FineractGateway(settings, transport=httpx.MockTransport(fineract))
    handlers = build_handlers(gateway, LedgerAccounts())
    partner = PartnerService(
        session_factory, OperationExecutor(session_factory, handlers, cast(Any, FakeErp()), settings), settings
    )
    command = CreateClient(
        external_reference="CLI-1",
        client_uid="SME-000001",
        first_name="Amina",
        last_name="Uwase",
        activation_date=date(2026, 1, 10),
    )

    with pytest.raises(ApiError) as raised:
        partner.create_client(command)

    assert (raised.value.status_code, raised.value.code) == (503, ErrorCode.FINERACT_UNAVAILABLE)
    with session_factory() as session:
        assert session.scalar(select(Client.client_uid)) == "SME-000001"
        assert session.scalar(select(Operation.retryable)) is True
    session_factory.kw["bind"].dispose()
