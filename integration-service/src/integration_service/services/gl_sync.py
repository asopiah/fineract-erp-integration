"""ERP chart of accounts → Fineract GL accounts. Create-only.

Triggered by an explicit call (``POST /api/accounts/sync``); failures are retried by recovery. Each account is
SYNC_PENDING → SYNCED, or FAILED with the reason. Sending the same account any number of times yields one ERP
account, one Fineract GL account (Fineract enforces it, see the Fineract patch) and one mapping row.
"""

import logging
from collections.abc import Collection

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from integration_service.domain.erp import ErpAccount
from integration_service.domain.gl_accounts import SyncStatus, UnsupportedAccountTypeError
from integration_service.gateways.erp import ErpGateway
from integration_service.gateways.errors import GatewayError, RejectedError
from integration_service.gateways.fineract import FineractGateway
from integration_service.models import GlAccountSync
from integration_service.observability import log_context
from integration_service.services.operation_store import db_now_plus
from integration_service.settings import Settings

logger = logging.getLogger(__name__)


class GlAccountSynchronizer:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        erp: ErpGateway,
        fineract: FineractGateway,
        settings: Settings,
    ) -> None:
        self._session_factory = session_factory
        self._erp = erp
        self._fineract = fineract
        self._settings = settings

    def sync_chart(self, only: Collection[str] | None = None) -> list[GlAccountSync]:
        """Synchronizes the ERP's accounts (or the listed external references) and returns their mapping rows."""
        accounts = [
            account for account in self._erp.list_accounts() if only is None or account.external_reference in only
        ]
        for account in accounts:
            self._sync(account)
        return self.statuses([account.external_reference for account in accounts])

    def retry_due(self) -> int:
        """Recovery: retries FAILED accounts whose failure was transient and whose backoff has elapsed."""
        with self._session_factory() as session:
            due = set(
                session.scalars(
                    select(GlAccountSync.external_reference).where(
                        GlAccountSync.status == SyncStatus.FAILED, GlAccountSync.next_attempt_at <= func.now()
                    )
                )
            )
        if not due:
            return 0
        # Re-read the ERP, the owner of the account's definition, rather than retry from a local copy.
        accounts = [account for account in self._erp.list_accounts() if account.external_reference in due]
        for account in accounts:
            self._sync(account)
        return len(accounts)

    def statuses(self, external_references: Collection[str] | None = None) -> list[GlAccountSync]:
        with self._session_factory() as session:
            query = select(GlAccountSync).order_by(GlAccountSync.code)
            if external_references is not None:
                query = query.where(GlAccountSync.external_reference.in_(external_references))
            return list(session.scalars(query))

    def _sync(self, account: ErpAccount) -> None:
        with self._session_factory() as session, log_context(external_reference=account.external_reference):
            row = self._locked_mapping(session, account)
            if row is None or row.status is SyncStatus.SYNCED:
                session.commit()
                return
            if account.currency != self._settings.ledger_currency:
                self._failed(row, f"currency {account.currency} is not {self._settings.ledger_currency}", retry=False)
                session.commit()
                return
            try:
                fineract_id = self._fineract.create_gl_account(account)
            except (RejectedError, UnsupportedAccountTypeError) as error:
                self._failed(row, str(error), retry=False)
            except GatewayError as error:
                # Safe to retry blindly: Fineract resolves a replay of the same external id to the existing account.
                self._failed(row, str(error), retry=True)
            else:
                row.status = SyncStatus.SYNCED
                row.fineract_gl_account_id = fineract_id
                row.last_error = None
                row.next_attempt_at = None
                logger.info("GL account synced to Fineract", extra={"status": SyncStatus.SYNCED})
            session.commit()

    def _locked_mapping(self, session: Session, account: ErpAccount) -> GlAccountSync | None:
        """The account's mapping row, created SYNC_PENDING if new, locked so concurrent syncs take turns."""
        session.execute(
            insert(GlAccountSync)
            .values(
                external_reference=account.external_reference,
                code=account.code,
                name=account.name,
                erp_type=account.type,
                status=SyncStatus.SYNC_PENDING,
                attempts=0,
            )
            .on_conflict_do_nothing()
        )
        row = session.scalar(
            select(GlAccountSync)
            .where(GlAccountSync.external_reference == account.external_reference)
            .with_for_update()
        )
        if row is None:
            logger.error("GL account code %s is mapped to another external reference", account.code)
        return row

    def _failed(self, row: GlAccountSync, reason: str, *, retry: bool) -> None:
        row.status = SyncStatus.FAILED
        row.attempts += 1
        row.last_error = reason[:500]
        row.next_attempt_at = db_now_plus(self._settings.recovery_delay(row.attempts)) if retry else None
        logger.warning("GL account sync failed: %s", reason, extra={"status": SyncStatus.FAILED})
