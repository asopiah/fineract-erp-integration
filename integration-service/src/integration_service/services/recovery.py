"""Finishes interrupted work: on startup, every few seconds, and on demand (``POST /api/admin/recovery``).

No queue or separate worker is needed. The operations table is the durable queue: every operation's status is
committed before each external call, and a lease names its current owner. An operation whose owner died (lease
expired) or whose backoff has elapsed is simply executed again by the same executor the request used. That is safe
because the executor looks Fineract up before submitting and the ERP is idempotent on the external reference.
"""

import logging
import threading
from dataclasses import dataclass

from sqlalchemy.orm import Session, sessionmaker

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
                if not operation_store.try_lease(session, operation_id, self._settings.lease_seconds):
                    continue  # someone else is working on it
            try:
                self._executor.execute(operation_id)
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
