"""Drives operations through their lifecycle. The only code that changes an operation's status.

The request that creates an operation runs it first; recovery resumes whatever was interrupted. Both use this same
code, and every step can be cut short (timeout, outage, crash) and resumed later from the persisted status:

* Fineract is always asked first whether it already has the operation, by its external reference. A retry never
  submits a command that may already have been applied.
* The ERP journal is derived from what Fineract stored, and the ERP is idempotent on the external reference, so
  posting it again is safe.
"""

import logging
import os
import sys
import time
from collections.abc import Callable, Mapping
from datetime import timedelta
from typing import Any, NoReturn

from sqlalchemy import func, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from integration_service.domain.accounting import UnmappableTransactionError
from integration_service.domain.fineract import FineractLoanTransaction
from integration_service.domain.operations import OperationStatus, OperationType, ensure_transition
from integration_service.errors import ApiError, ErrorCode
from integration_service.gateways.erp import ErpGateway
from integration_service.gateways.errors import GatewayError, NotAppliedError, OutcomeUnknownError, RejectedError
from integration_service.gateways.faults import FaultInjector
from integration_service.models import Operation
from integration_service.observability import log_context
from integration_service.services.handlers import MoneyMovement, OperationHandler
from integration_service.services.operation_store import BusinessKeyTakenError
from integration_service.settings import Settings

logger = logging.getLogger(__name__)


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

    def execute(self, operation_id: int) -> None:
        """Advances the operation as far as it can go now. The caller holds its lease; it is released on return.

        A simulated crash (a ``BaseException``) deliberately skips the release, like a process that died.
        """
        with self._session_factory() as session:
            operation = session.get_one(Operation, operation_id)
            with log_context(
                correlation_id=operation.correlation_id,
                external_reference=operation.external_reference,
                client_uid=operation.client_uid,
                loan_reference=operation.loan_reference,
            ):
                try:
                    self._advance(session, operation)
                except Exception:
                    self._release(session, operation_id)
                    raise
                self._release(session, operation_id)

    def _advance(self, session: Session, operation: Operation) -> None:
        handler = self._handlers[operation.type]
        # An operation found in FINERACT_PENDING was interrupted mid-call: Fineract may already have it.
        may_have_reached_fineract = operation.status is OperationStatus.FINERACT_PENDING
        while True:
            match operation.status:
                case OperationStatus.RECEIVED:
                    self._move(session, operation, OperationStatus.FINERACT_PENDING)
                case OperationStatus.FAILED if operation.retryable:
                    self._move(session, operation, OperationStatus.FINERACT_PENDING, error=None, retryable=False)
                case OperationStatus.FINERACT_PENDING:
                    if not self._settle_in_fineract(session, operation, handler, may_have_reached_fineract):
                        return
                case OperationStatus.FINERACT_DONE:
                    self._crash_if_requested()
                    if operation.type.posts_journal:
                        self._move(session, operation, OperationStatus.ERP_PENDING)
                    else:
                        self._complete(session, operation, handler)
                case OperationStatus.ERP_PENDING:
                    if not self._post_journal(session, operation, handler):
                        return
                case _:
                    return

    # --- Fineract ------------------------------------------------------------------------------------------------

    def _settle_in_fineract(
        self, session: Session, operation: Operation, handler: OperationHandler[Any], may_have_applied: bool
    ) -> bool:
        """Leaves FINERACT_PENDING only on evidence: Fineract's record, or certainty that nothing reached it."""
        last_failure: GatewayError | None = None
        for attempt in range(1, self._settings.fineract_attempts + 1):
            record: Any = None
            try:
                record = self._find_or_submit(session, operation, handler)
            except RejectedError as rejection:
                # Fineract's own uniqueness may be what refused us: an earlier attempt may have committed after all.
                try:
                    record = handler.find(session, operation)
                except GatewayError as failure:
                    last_failure, may_have_applied = failure, True
                else:
                    if record is None:
                        handler.release(session, operation)
                        self._fail(session, operation, rejection, retryable=False)
                        return False
            except OutcomeUnknownError as failure:
                last_failure, may_have_applied = failure, True
            except NotAppliedError as failure:
                last_failure = failure
            if record is not None:
                return self._accept(session, operation, handler, record)
            logger.warning("Fineract attempt %d failed: %s", attempt, last_failure, extra={"status": operation.status})
            self._pause(session, operation, attempt)

        assert last_failure is not None
        if may_have_applied:
            self._defer(session, operation, last_failure)
        else:
            self._fail(session, operation, last_failure, retryable=True)
        return False

    def _find_or_submit(self, session: Session, operation: Operation, handler: OperationHandler[Any]) -> Any:
        record = handler.find(session, operation)
        if record is not None:
            logger.info("already in Fineract, not submitting again", extra={"status": operation.status})
            return record
        handler.submit(session, operation)
        record = handler.find(session, operation)
        if record is None:
            raise OutcomeUnknownError("fineract", "command accepted but its result is not visible yet")
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

    def _post_journal(self, session: Session, operation: Operation, handler: OperationHandler[Any]) -> bool:
        assert isinstance(handler, MoneyMovement)
        try:
            draft = handler.journal_entry(session, operation)
        except UnmappableTransactionError as error:
            self._needs_reconciliation(session, operation, str(error))
            return False
        try:
            entry = self._erp.post_journal_entry(draft)
        except RejectedError as error:
            self._needs_reconciliation(session, operation, f"ERP rejected the journal entry: {error.message}")
            return False
        except (NotAppliedError, OutcomeUnknownError) as error:
            self._defer(session, operation, error)
            return False
        handler.record_journal(session, operation, entry)
        with log_context(erp_transaction_id=entry.entry_number):
            self._complete(session, operation, handler)
        return True

    # --- Transitions ---------------------------------------------------------------------------------------------

    def _move(self, session: Session, operation: Operation, target: OperationStatus, **changes: Any) -> None:
        ensure_transition(operation.status, target)
        operation.status = target
        operation.attempts = 0
        operation.next_attempt_at = None
        operation.lease_expires_at = func.now() + timedelta(seconds=self._settings.lease_seconds)
        for name, value in changes.items():
            setattr(operation, name, value)
        try:
            session.commit()
        except IntegrityError as error:
            # Re-entering a once-per-loan slot that another operation took while this one was FAILED.
            session.rollback()
            raise BusinessKeyTakenError(str(error.orig)) from error
        logger.info("operation %s", target.value.lower(), extra={"status": target})

    def _complete(self, session: Session, operation: Operation, handler: OperationHandler[Any]) -> None:
        self._move(session, operation, OperationStatus.COMPLETED, result=handler.result(session, operation))

    def _fail(self, session: Session, operation: Operation, failure: GatewayError, *, retryable: bool) -> None:
        if retryable:
            error = ApiError(
                503, ErrorCode.FINERACT_UNAVAILABLE, f"Fineract did not process the request: {failure.message}"
            )
        else:
            error = ApiError(422, ErrorCode.FINERACT_REJECTED, failure.message, fineract_code=failure.code)
        self._move(session, operation, OperationStatus.FAILED, error=error.as_stored(), retryable=retryable)

    def _needs_reconciliation(self, session: Session, operation: Operation, reason: str) -> None:
        self._move(session, operation, OperationStatus.NEEDS_RECONCILIATION, error={"reason": reason})
        logger.error("needs reconciliation: %s", reason, extra={"status": OperationStatus.NEEDS_RECONCILIATION})

    def _defer(self, session: Session, operation: Operation, failure: GatewayError) -> None:
        """Keeps the status and leaves the next attempt to recovery, with capped exponential backoff."""
        operation.attempts += 1
        if operation.attempts >= self._settings.recovery_max_attempts:
            self._needs_reconciliation(
                session, operation, f"gave up after {operation.attempts} attempts: {failure.message}"
            )
            return
        delay = min(
            self._settings.recovery_backoff_cap_seconds,
            self._settings.recovery_backoff_seconds * 2 ** (operation.attempts - 1),
        )
        operation.next_attempt_at = func.now() + timedelta(seconds=delay)
        session.commit()
        logger.warning(
            "deferred to recovery (attempt %d, next in %.1fs): %s",
            operation.attempts,
            delay,
            failure.message,
            extra={"status": operation.status},
        )

    def _pause(self, session: Session, operation: Operation, attempt: int) -> None:
        if attempt == self._settings.fineract_attempts:
            return
        time.sleep(self._settings.fineract_backoff_seconds * 2 ** (attempt - 1))
        operation.lease_expires_at = func.now() + timedelta(seconds=self._settings.lease_seconds)
        session.commit()

    def _release(self, session: Session, operation_id: int) -> None:
        session.rollback()
        session.execute(update(Operation).where(Operation.id == operation_id).values(lease_expires_at=None))
        session.commit()

    def _crash_if_requested(self) -> None:
        if self._faults is not None and self._faults.take_crash():
            logger.critical("simulated crash after Fineract success, before the ERP call")
            self._crash()
