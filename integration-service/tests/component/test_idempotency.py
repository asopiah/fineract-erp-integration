"""Part F: one business operation per external reference, decided by the database."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date

import pytest

from integration_service.commands import CreateClient
from integration_service.domain.operations import OperationType
from integration_service.errors import ApiError, ErrorCode
from integration_service.services import operation_store
from tests.component.conftest import System
from tests.component.fakes import Fault


def client(reference: str = "CLI-1", uid: str = "SME-000001", first_name: str = "Amina") -> CreateClient:
    return CreateClient(
        external_reference=reference,
        client_uid=uid,
        first_name=first_name,
        last_name="Uwase",
        activation_date=date(2026, 1, 10),
    )


def test_a_finished_operation_replays_its_original_result(system: System) -> None:
    first = system.partner.create_client(client())
    replay = system.partner.create_client(client())

    assert (first.status_code, replay.status_code) == (201, 200)
    assert replay.body == first.body
    assert system.fineract.commands("create_client") == 1


def test_same_reference_with_a_different_payload_is_a_conflict(system: System) -> None:
    system.partner.create_client(client())

    with pytest.raises(ApiError) as raised:
        system.partner.create_client(client(first_name="Aminata"))

    assert (raised.value.status_code, raised.value.code) == (409, ErrorCode.IDEMPOTENCY_CONFLICT)


def test_a_client_uid_cannot_be_reused_under_another_reference(system: System) -> None:
    system.partner.create_client(client())

    with pytest.raises(ApiError) as raised:
        system.partner.create_client(client(reference="CLI-2"))

    assert raised.value.code is ErrorCode.CLIENT_UID_TAKEN
    assert system.fineract.commands("create_client") == 1


def test_an_operation_in_progress_answers_202_and_is_not_executed_again(system: System) -> None:
    with system.session_factory() as session:  # another request holds the reference and is still working on it
        operation_store.claim(
            session, OperationType.CLIENT_CREATION, "CLI-1", client(), lease_seconds=60, client_uid="SME-000001"
        )

    outcome = system.partner.create_client(client())

    assert outcome.status_code == 202
    assert outcome.body["status"] == "RECEIVED"
    assert system.fineract.commands("create_client") == 0


def test_concurrent_identical_requests_execute_once(system: System) -> None:
    with ThreadPoolExecutor(max_workers=10) as pool:
        outcomes = list(pool.map(lambda _: system.partner.create_client(client()), range(10)))

    statuses = sorted(outcome.status_code for outcome in outcomes)
    assert statuses.count(201) == 1
    assert set(statuses) <= {200, 201, 202}
    assert system.fineract.commands("create_client") == 1


def test_a_rejected_creation_releases_the_client_uid(system: System) -> None:
    system.fineract.fail("create_client", Fault.REJECTED)

    with pytest.raises(ApiError) as rejected:
        system.partner.create_client(client())
    corrected = system.partner.create_client(client(reference="CLI-2"))

    assert (rejected.value.status_code, rejected.value.code) == (422, ErrorCode.FINERACT_REJECTED)
    assert corrected.status_code == 201


def test_a_rejection_is_replayed_for_its_reference(system: System) -> None:
    system.fineract.fail("create_client", Fault.REJECTED)
    with pytest.raises(ApiError):
        system.partner.create_client(client())

    with pytest.raises(ApiError) as replayed:
        system.partner.create_client(client())

    assert replayed.value.code is ErrorCode.FINERACT_REJECTED
    assert system.fineract.commands("create_client") == 0
