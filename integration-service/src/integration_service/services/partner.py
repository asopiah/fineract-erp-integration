"""The partner's state-changing use cases.

Each one checks local preconditions, claims the external reference, lets the executor carry the operation as far as
it can, and answers with the operation's state (see ``_outcome``).
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from integration_service.commands import (
    ApplyForLoan,
    ApproveLoan,
    Command,
    CreateClient,
    DisburseLoan,
    MakeRepayment,
    ReverseRepayment,
)
from integration_service.domain.fineract import LoanTransactionType
from integration_service.domain.operations import OperationStatus, OperationType
from integration_service.domain.references import loan_reference
from integration_service.errors import ApiError, ErrorCode
from integration_service.models import (
    LOAN_REFERENCE_SEQUENCE,
    MONEY_REFERENCE_INDEX,
    Client,
    Loan,
    LoanTransaction,
    Operation,
)
from integration_service.services import operation_store
from integration_service.services.executor import OperationExecutor
from integration_service.services.operation_store import BusinessKeyTakenError, ReferenceReusedError, Reserve
from integration_service.settings import Settings

_PENDING_DETAIL = {
    OperationStatus.RECEIVED: "accepted; processing has not started",
    OperationStatus.FINERACT_PENDING: "Fineract's outcome is not confirmed yet; it is resolved by looking it up",
    OperationStatus.FINERACT_DONE: "recorded in Fineract",
    OperationStatus.ERP_PENDING: "recorded in Fineract; the ERP journal entry is being posted",
}


@dataclass(frozen=True)
class Outcome:
    status_code: int
    body: dict[str, Any]


class PartnerService:
    def __init__(self, session_factory: sessionmaker[Session], executor: OperationExecutor, settings: Settings) -> None:
        self._session_factory = session_factory
        self._executor = executor
        self._settings = settings

    def create_client(self, command: CreateClient) -> Outcome:
        def reserve(session: Session, _: Operation) -> None:
            session.add(
                Client(
                    client_uid=command.client_uid,
                    external_reference=command.external_reference,
                    first_name=command.first_name,
                    last_name=command.last_name,
                )
            )

        return self._run(
            OperationType.CLIENT_CREATION,
            command,
            client_uid=command.client_uid,
            reserve=reserve,
            key_taken=lambda: ApiError(
                409, ErrorCode.CLIENT_UID_TAKEN, f"client_uid {command.client_uid} belongs to another client"
            ),
        )

    def apply_for_loan(self, command: ApplyForLoan) -> Outcome:
        with self._session_factory() as session:
            client = session.scalar(select(Client).where(Client.client_uid == command.client_uid))
            if client is None:
                raise ApiError(404, ErrorCode.NOT_FOUND, f"client {command.client_uid} does not exist")
            if client.fineract_client_id is None:
                raise ApiError(409, ErrorCode.CLIENT_NOT_READY, f"client {command.client_uid} is still being created")

        def reserve(session: Session, operation: Operation) -> None:
            sequence = session.execute(LOAN_REFERENCE_SEQUENCE.next_value()).scalar_one()
            operation.loan_reference = loan_reference(command.submitted_on.year, sequence)
            session.add(
                Loan(
                    loan_reference=operation.loan_reference,
                    external_reference=command.external_reference,
                    client_uid=command.client_uid,
                    principal=command.principal,
                    annual_interest_rate=command.annual_interest_rate,
                    term_months=command.term_months,
                    submitted_on=command.submitted_on,
                    expected_disbursement_on=command.expected_disbursement_on,
                )
            )

        return self._run(OperationType.LOAN_APPLICATION, command, client_uid=command.client_uid, reserve=reserve)

    def approve_loan(self, reference: str, command: ApproveLoan) -> Outcome:
        loan = self._loan_in_fineract(reference)
        return self._run(
            OperationType.LOAN_APPROVAL,
            command,
            client_uid=loan.client_uid,
            loan_reference=loan.loan_reference,
            key_taken=lambda: self._slot_taken(OperationType.LOAN_APPROVAL, reference, ErrorCode.LOAN_ALREADY_APPROVED),
        )

    def disburse(self, reference: str, command: DisburseLoan) -> Outcome:
        loan = self._loan_in_fineract(reference)
        return self._run(
            OperationType.DISBURSEMENT,
            command,
            client_uid=loan.client_uid,
            loan_reference=loan.loan_reference,
            key_taken=lambda: self._slot_taken(OperationType.DISBURSEMENT, reference, ErrorCode.LOAN_ALREADY_DISBURSED),
        )

    def repay(self, reference: str, command: MakeRepayment) -> Outcome:
        loan = self._loan_in_fineract(reference)
        return self._run(
            OperationType.REPAYMENT, command, client_uid=loan.client_uid, loan_reference=loan.loan_reference
        )

    def reverse_repayment(self, reference: str, command: ReverseRepayment) -> Outcome:
        loan = self._loan_in_fineract(reference)
        with self._session_factory() as session:
            repayment = session.scalar(
                select(LoanTransaction).where(LoanTransaction.external_reference == command.repayment_reference)
            )
        if (
            repayment is None
            or repayment.loan_reference != reference
            or repayment.type is not LoanTransactionType.REPAYMENT
        ):
            raise ApiError(404, ErrorCode.NOT_FOUND, f"loan {reference} has no repayment {command.repayment_reference}")
        if repayment.reversed_by not in (None, command.external_reference):
            raise ApiError(
                409,
                ErrorCode.REPAYMENT_ALREADY_REVERSED,
                f"repayment {command.repayment_reference} is already reversed by {repayment.reversed_by}",
            )
        return self._run(
            OperationType.REPAYMENT_REVERSAL, command, client_uid=loan.client_uid, loan_reference=loan.loan_reference
        )

    # --- Shared flow ---------------------------------------------------------------------------------------------

    def _run(
        self,
        operation_type: OperationType,
        command: Command,
        *,
        client_uid: str | None = None,
        loan_reference: str | None = None,
        reserve: Reserve | None = None,
        key_taken: Callable[[], ApiError] | None = None,
    ) -> Outcome:
        with self._session_factory() as session, _translating_taken_keys(operation_type, key_taken):
            try:
                claim = operation_store.claim(
                    session,
                    operation_type,
                    command.external_reference,
                    command,
                    lease_seconds=self._settings.lease_seconds,
                    client_uid=client_uid,
                    loan_reference=loan_reference,
                    reserve=reserve,
                )
            except ReferenceReusedError as error:
                raise ApiError(409, ErrorCode.IDEMPOTENCY_CONFLICT, str(error)) from error

        lease = claim.lease if claim.is_new else self._lease_failed_for_retry(claim.operation_id)
        if lease is not None:
            with _translating_taken_keys(operation_type, key_taken):
                self._executor.execute(claim.operation_id, lease)
        return self._outcome(claim.operation_id, executed=lease is not None)

    def _lease_failed_for_retry(self, operation_id: int) -> str | None:
        """A retryable failure never reached Fineract: the same request may run again (once, under a lease)."""
        with self._session_factory() as session:
            return operation_store.try_lease(
                session, operation_id, self._settings.lease_seconds, only_retryable_failure=True
            )

    def _outcome(self, operation_id: int, executed: bool) -> Outcome:
        """201 or 200 (a replay) with the result, the stored error, or 202 with the status while unfinished."""
        with self._session_factory() as session:
            operation = session.get_one(Operation, operation_id)
            match operation.status:
                case OperationStatus.COMPLETED:
                    return Outcome(201 if executed else 200, _stored(operation.result, operation))
                case OperationStatus.FAILED:
                    raise ApiError.from_stored(_stored(operation.error, operation))
                case OperationStatus.NEEDS_RECONCILIATION:
                    reason = (operation.error or {}).get("reason", "see the operations view")
                    return Outcome(202, pending_body(operation, f"needs manual reconciliation: {reason}"))
                case status:
                    return Outcome(202, pending_body(operation, _PENDING_DETAIL[status]))

    def _loan_in_fineract(self, reference: str) -> Loan:
        with self._session_factory() as session:
            loan = session.scalar(select(Loan).where(Loan.loan_reference == reference))
            if loan is None:
                raise ApiError(404, ErrorCode.NOT_FOUND, f"loan {reference} does not exist")
            if loan.fineract_loan_id is None:
                raise ApiError(409, ErrorCode.LOAN_NOT_READY, f"loan {reference} is still being created")
            return loan

    def _slot_taken(self, operation_type: OperationType, reference: str, code: ErrorCode) -> ApiError:
        with self._session_factory() as session:
            holder = operation_store.active_for_loan(session, operation_type, reference)
        held_by = f" by {holder.external_reference}" if holder is not None else ""
        noun = "approved" if operation_type is OperationType.LOAN_APPROVAL else "disbursed"
        return ApiError(409, code, f"loan {reference} is already {noun}{held_by}")


@contextmanager
def _translating_taken_keys(operation_type: OperationType, key_taken: Callable[[], ApiError] | None) -> Iterator[None]:
    """A business key taken by another operation becomes the use case's own 409 (e.g. LOAN_ALREADY_DISBURSED)."""
    try:
        yield
    except BusinessKeyTakenError as error:
        if error.constraint == MONEY_REFERENCE_INDEX:
            message = f"external_reference is already used by another disbursement or repayment ({operation_type})"
            raise ApiError(409, ErrorCode.IDEMPOTENCY_CONFLICT, message) from error
        if key_taken is None:
            raise
        raise key_taken() from error


def _stored(body: dict[str, Any] | None, operation: Operation) -> dict[str, Any]:
    if body is None:
        raise RuntimeError(f"{operation.status} operation {operation.id} has nothing stored to answer with")
    return body


def pending_body(operation: Operation, detail: str) -> dict[str, Any]:
    return {
        "external_reference": operation.external_reference,
        "operation": operation.type.value,
        "status": operation.status.value,
        "detail": detail,
    }
