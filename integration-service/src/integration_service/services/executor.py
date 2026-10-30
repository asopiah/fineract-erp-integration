"""Drives operations through their lifecycle. The only code that changes an operation's status.

The request that creates an operation runs it first; recovery resumes whatever was interrupted. Both use this same
code, and every step can be cut short (timeout, outage, crash) and resumed later from the persisted status:

* Fineract is always asked first whether it already has the operation, by its external reference. A retry never
  submits a command that may already have been applied, and an operation whose outcome is unknown is never FAILED.
* The ERP journal is derived from what Fineract stored, and the ERP is idempotent on the external reference, so
  posting it again is safe.

One executor at a time: the caller passes the lease token it holds, every write checks the row's version, and the
lease is renewed before each external call. An executor whose lease was taken over stops at its next write.
"""

import logging
import os
import sys
import time
from collections.abc import Callable, Mapping
from typing import Any, NoReturn

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.orm.exc import StaleDataError

from integration_service.domain.accounting import UnmappableTransactionError
from integration_service.domain.fineract import FineractLoanTransaction
from integration_service.domain.operations import (
    ALLOWED_TRANSITIONS,
    OperationStatus,
    OperationType,
    ensure_transition,
)
from integration_service.errors import ApiError, ErrorCode
from integration_service.gateways.erp import ErpGateway
from integration_service.gateways.errors import GatewayError, NotAppliedError, OutcomeUnknownError, RejectedError
from integration_service.gateways.faults import FaultInjector
from integration_service.models import Operation
from integration_service.observability import log_context
from integration_service.services import operation_store
from integration_service.services.handlers import MoneyMovement, OperationHandler
from integration_service.services.operation_store import BusinessKeyTakenError, db_now_plus
from integration_service.settings import Settings

logger = logging.getLogger(__name__)


class LeaseLostError(Exception):
    """Another executor took the operation over (this one's lease expired); this one must stop."""


def _exit_process() -> NoReturn:
    sys.stdout.flush()
    os._exit(70)


class OperationExecutor:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        handlers: Mapping[OperationType, OperationHandler[Any]],
        erp: ErpGateway,
        settings: Settings,
        faults: FaultInjector | None = None,
        crash: Callable[[], NoReturn] = _exit_process,
    ) -> None:
        self._session_factory = session_factory
        self._handlers = handlers
        self._erp = erp
        self._settings = settings
        self._faults = faults
        self._crash = crash

    def execute(self, operation_id: int, lease: str) -> None:
        """Advances the operation as far as it can go now, under the caller's lease, then releases the lease.

        A simulated crash (a ``BaseException``) deliberately skips the release, like a process that died.
        """
        self._run(operation_id, lease, self._advance)

    def resume_erp_posting(self, operation_id: int, lease: str) -> None:
        """Operator repair: re-drives the ERP posting of a money movement Fineract recorded (NEEDS_RECONCILIATION)."""

        def resume(session: Session, operation: Operation) -> None:
            self._confirm_still_recorded(session, operation)
            self._move(session, operation, OperationStatus.ERP_PENDING)
            self._advance(session, operation)

        self._run(operation_id, lease, resume)

    def _run(self, operation_id: int, lease: str, work: Callable[[Session, Operation], None]) -> None:
        with self._session_factory() as session:
            operation = session.get_one(Operation, operation_id)
            if operation.lease_owner != lease:
                return  # taken over before we started
            with log_context(
                correlation_id=operation.correlation_id,
                external_reference=operation.external_reference,
                client_uid=operation.client_uid,
                loan_reference=operation.loan_reference,
            ):
                try:
                    work(session, operation)
                except LeaseLostError:
                    session.rollback()
                    logger.warning("lease lost: another executor now owns this operation; stopping")
                    return
                except Exception as error:
                    session.rollback()
                    if not operation.status.in_flight:
                        operation_store.release(session, operation_id, lease)
                        raise
                    # The outcome may be unknown: back off and count the attempt, never FAIL.
                    logger.exception("unexpected failure while in flight")
                    try:
                        self._defer(session, operation, f"unexpected {type(error).__name__}: {error}")
                    except LeaseLostError:
                        return
                session.rollback()
                operation_store.release(session, operation_id, lease)

    def _advance(self, session: Session, operation: Operation) -> None:
        handler = self._handlers[operation.type]
        # An operation found in FINERACT_PENDING was interrupted mid-call: Fineract may already have it.
        may_have_applied = operation.status is OperationStatus.FINERACT_PENDING
        while True:
            match operation.status:
                case OperationStatus.RECEIVED:
                    self._move(session, operation, OperationStatus.FINERACT_PENDING)
                case OperationStatus.FAILED if operation.retryable:
                    self._move(session, operation, OperationStatus.FINERACT_PENDING, error=None, retryable=False)
                case OperationStatus.FINERACT_PENDING:
                    if not self._settle_in_fineract(session, operation, handler, may_have_applied):
                        return
                case OperationStatus.FINERACT_DONE:
                    self._crash_if_requested()
                    if operation.type.posts_journal:
                        self._move(session, operation, OperationStatus.ERP_PENDING)
                    else:
                        self._complete(session, operation, handler)
                case OperationStatus.ERP_PENDING:
                    if not self._post_journal(session, operation):
                        return
                case _:
                    return

    # --- Fineract ------------------------------------------------------------------------------------------------

    def _settle_in_fineract(
        self, session: Session, operation: Operation, handler: OperationHandler[Any], may_have_applied: bool
    ) -> bool:
        """Leaves FINERACT_PENDING only on evidence: Fineract's record, or certainty that nothing reached it."""
        last_failure = "no attempt made"
        for attempt in range(1, self._settings.fineract_attempts + 1):
            self._renew_lease(session, operation)
            record: Any = None
            try:
                record = self._find_or_submit(session, operation, handler)
            except RejectedError as rejection:
                # Fineract's own uniqueness may be what refused us: an earlier attempt may have committed after all.
                try:
                    self._renew_lease(session, operation)
                    record = handler.find(session, operation)
                except GatewayError as failure:
                    last_failure, may_have_applied = failure.message, True
                else:
                    if record is None:
                        handler.release(session, operation)
                        self._fail(session, operation, rejection, retryable=False)
                        return False
            except OutcomeUnknownError as failure:
                last_failure, may_have_applied = failure.message, True
            except NotAppliedError as failure:
                last_failure = failure.message
            except LeaseLostError:
                raise
            except Exception as failure:
                # Not a classified outcome (an unparseable answer, missing reference data): never conclude anything.
                logger.exception("unexpected failure talking to Fineract")
                last_failure, may_have_applied = f"{type(failure).__name__}: {failure}", True
            if record is not None:
                return self._accept(session, operation, handler, record)
            logger.warning("Fineract attempt %d failed: %s", attempt, last_failure, extra={"status": operation.status})
            self._pause(attempt)

        if may_have_applied:
            self._defer(session, operation, last_failure)
        else:
            self._fail(session, operation, NotAppliedError("fineract", last_failure), retryable=True)
        return False

    def _find_or_submit(self, session: Session, operation: Operation, handler: OperationHandler[Any]) -> Any:
        record = handler.find(session, operation)
        if record is not None:
            logger.info("already in Fineract, not submitting again", extra={"status": operation.status})
            return record
        self._renew_lease(session, operation)
        handler.submit(session, operation)
        # From here on Fineract has the command: failing to confirm it means "unknown", never "not applied".
        self._renew_lease(session, operation)
        try:
            record = handler.find(session, operation)
        except GatewayError as failure:
            raise OutcomeUnknownError("fineract", f"submitted, but could not confirm: {failure.message}") from failure
        if record is None:
            raise OutcomeUnknownError("fineract", "submitted, but the result is not visible yet")
        logger.info("submitted to Fineract", extra={"status": operation.status})
        return record

    def _accept(self, session: Session, operation: Operation, handler: OperationHandler[Any], record: Any) -> bool:
        problem = handler.contradiction(operation, record)
        if problem is not None:
            self._needs_reconciliation(session, operation, problem)
            return False
        handler.record(session, operation, record)
        fineract_transaction_id = record.internal_id if isinstance(record, FineractLoanTransaction) else None
        with log_context(fineract_transaction_id=fineract_transaction_id):
            self._move(session, operation, OperationStatus.FINERACT_DONE)
        return True

    # --- ERP -----------------------------------------------------------------------------------------------------

    def _post_journal(self, session: Session, operation: Operation) -> bool:
        handler = self._money_movement(operation)
        try:
            draft = handler.journal_entry(session, operation)
        except UnmappableTransactionError as error:
            self._needs_reconciliation(session, operation, str(error))
            return False
        self._renew_lease(session, operation)
        try:
            entry = self._erp.post_journal_entry(draft)
        except RejectedError as error:
            self._needs_reconciliation(session, operation, f"ERP rejected the journal entry: {error.message}")
            return False
        except (NotAppliedError, OutcomeUnknownError) as error:
            self._defer(session, operation, error.message)
            return False
        handler.record_journal(session, operation, entry)
        with log_context(erp_transaction_id=entry.entry_number):
            self._complete(session, operation, handler)
        return True

    def _confirm_still_recorded(self, session: Session, operation: Operation) -> None:
        """Operator repair posts the stored record only while Fineract still holds it, unreversed and unchanged."""
        handler = self._money_movement(operation)
        try:
            record = handler.find(session, operation)
        except GatewayError as failure:
            message = f"could not confirm the transaction in Fineract: {failure.message}"
            raise ApiError(503, ErrorCode.FINERACT_UNAVAILABLE, message) from failure
        if record is None:
            problem: str | None = f"Fineract no longer has {operation.external_reference}"
        else:
            problem = handler.stored_contradiction(session, operation, record)
        if problem is not None:
            raise ApiError(409, ErrorCode.NOT_RESUMABLE, f"{problem}; resolve it in Fineract first")

    def _money_movement(self, operation: Operation) -> MoneyMovement:
        handler = self._handlers[operation.type]
        if not isinstance(handler, MoneyMovement):
            raise TypeError(f"{operation.type} has no ERP journal entry")
        return handler

    # --- Transitions ---------------------------------------------------------------------------------------------

    def _move(self, session: Session, operation: Operation, target: OperationStatus, **changes: Any) -> None:
        ensure_transition(operation.status, target)
        leaving_failed = operation.status is OperationStatus.FAILED
        operation.status = target
        operation.attempts = 0
        operation.next_attempt_at = None
        operation.lease_expires_at = db_now_plus(self._settings.lease_seconds)
        for name, value in changes.items():
            setattr(operation, name, value)
        try:
            self._commit(session)
        except IntegrityError as error:
            session.rollback()
            if leaving_failed:  # re-entering a once-per-loan slot another operation took meanwhile
                raise BusinessKeyTakenError(error) from error
            raise
        logger.info("operation %s", target.value.lower(), extra={"status": target})

    def _complete(self, session: Session, operation: Operation, handler: OperationHandler[Any]) -> None:
        self._move(session, operation, OperationStatus.COMPLETED, result=handler.result(session, operation))

    def _fail(self, session: Session, operation: Operation, failure: GatewayError, *, retryable: bool) -> None:
        if retryable:
            message = f"Fineract did not process the request: {failure.message}"
            error = ApiError(503, ErrorCode.FINERACT_UNAVAILABLE, message)
        else:
            error = ApiError(422, ErrorCode.FINERACT_REJECTED, failure.message, fineract_code=failure.code)
        self._move(session, operation, OperationStatus.FAILED, error=error.as_stored(), retryable=retryable)

    def _needs_reconciliation(self, session: Session, operation: Operation, reason: str) -> None:
        self._move(session, operation, OperationStatus.NEEDS_RECONCILIATION, error={"reason": reason})
        logger.error("needs reconciliation: %s", reason, extra={"status": OperationStatus.NEEDS_RECONCILIATION})

    def _defer(self, session: Session, operation: Operation, reason: str) -> None:
        """Keeps the status and leaves the next attempt to recovery, with capped exponential backoff."""
        operation.attempts += 1
        exhausted = operation.attempts >= self._settings.recovery_max_attempts
        # RECEIVED and FINERACT_DONE cannot be handed to a human; they keep retrying at the capped delay.
        if exhausted and OperationStatus.NEEDS_RECONCILIATION in ALLOWED_TRANSITIONS[operation.status]:
            self._needs_reconciliation(session, operation, f"gave up after {operation.attempts} attempts: {reason}")
            return
        delay = self._settings.recovery_delay(operation.attempts)
        operation.next_attempt_at = db_now_plus(delay)
        self._commit(session)
        logger.warning(
            "deferred to recovery (attempt %d, next in %.1fs): %s",
            operation.attempts,
            delay,
            reason,
            extra={"status": operation.status},
        )

    def _renew_lease(self, session: Session, operation: Operation) -> None:
        """Before every external call: extends the lease, and finds out early if it was lost."""
        operation.lease_expires_at = db_now_plus(self._settings.lease_seconds)
        self._commit(session)

    def _pause(self, attempt: int) -> None:
        if attempt < self._settings.fineract_attempts:
            time.sleep(self._settings.fineract_backoff_seconds * 2 ** (attempt - 1))

    @staticmethod
    def _commit(session: Session) -> None:
        try:
            session.commit()
        except StaleDataError as error:
            session.rollback()
            raise LeaseLostError from error

    def _crash_if_requested(self) -> None:
        if self._faults is not None and self._faults.take_crash():
            logger.critical("simulated crash after Fineract success, before the ERP call")
            self._crash()
