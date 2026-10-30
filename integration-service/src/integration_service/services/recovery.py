"""Finishes interrupted work on startup, every few seconds, and on demand. The operations table is the queue:
an operation whose lease expired or whose backoff elapsed is executed again by the same executor."""

import logging
import threading
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from integration_service.domain.operations import OperationStatus, OperationType
from integration_service.errors import ApiError, ErrorCode
from integration_service.models import LoanTransaction, Operation
from integration_service.services import operation_store
from integration_service.services.executor import OperationExecutor
from integration_service.services.gl_sync import GlAccountSynchronizer
from integration_service.settings import Settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RecoveryReport:
    operations_resumed: int
    gl_accounts_retried: int


class Recovery:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        executor: OperationExecutor,
        gl_sync: GlAccountSynchronizer,
        settings: Settings,
    ) -> None:
        self._session_factory = session_factory
        self._executor = executor
        self._gl_sync = gl_sync
        self._settings = settings
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None

    def run_once(self) -> RecoveryReport:
        resumed = 0
        with self._session_factory() as session:
            due = operation_store.due_for_recovery(session)
        for operation_id in due:
            with self._session_factory() as session:
                lease = operation_store.try_lease(session, operation_id, self._settings.lease_seconds)
            if lease is None:
                continue  # someone else is working on it
            try:
                self._executor.execute(operation_id, lease)
                resumed += 1
            except Exception:
                logger.exception("recovery of operation %d failed; it stays in place for the next sweep", operation_id)
        try:
            gl_retried = self._gl_sync.retry_due()
        except Exception:
            logger.exception("GL account retry failed; it stays in place for the next sweep")
            gl_retried = 0
        if resumed or gl_retried:
            logger.info("recovery resumed %d operation(s), retried %d GL account(s)", resumed, gl_retried)
        return RecoveryReport(operations_resumed=resumed, gl_accounts_retried=gl_retried)

    def resume_erp_posting(self, operation_type: OperationType, external_reference: str) -> OperationStatus:
        """Explicit operator repair for a money movement in NEEDS_RECONCILIATION whose Fineract side is recorded.

        Idempotent: the ERP accepts the journal once. Refused unless Fineract still holds the stored transaction,
        unreversed and unchanged; anything else needs a decision in Fineract first.
        """
        with self._session_factory() as session:
            operation = operation_store.find(session, operation_type, external_reference)
            if operation is None:
                raise ApiError(404, ErrorCode.NOT_FOUND, f"no {operation_type} {external_reference}")
            if operation.status is not OperationStatus.NEEDS_RECONCILIATION:
                raise ApiError(
                    409, ErrorCode.NOT_RESUMABLE, f"{external_reference} is {operation.status}, not awaiting repair"
                )
            recorded = session.scalar(
                select(LoanTransaction.id).where(LoanTransaction.external_reference == external_reference)
            )
            if not operation.type.posts_journal or recorded is None:
                raise ApiError(
                    409, ErrorCode.NOT_RESUMABLE, "Fineract's side is not settled; resolve it in Fineract first"
                )
            lease = operation_store.try_lease(session, operation.id, self._settings.lease_seconds)
            if lease is None:
                raise ApiError(409, ErrorCode.NOT_RESUMABLE, f"{external_reference} is being worked on")
            operation_id = operation.id
        self._executor.resume_erp_posting(operation_id, lease)
        with self._session_factory() as session:
            return session.get_one(Operation, operation_id).status

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="recovery", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopped.set()
        if self._thread is not None:
            self._thread.join(timeout=10)

    def _loop(self) -> None:
        while True:
            try:
                self.run_once()
            except Exception:
                logger.exception("recovery sweep failed")
            if self._stopped.wait(self._settings.recovery_interval_seconds):
                return
