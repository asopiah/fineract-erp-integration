from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, NoReturn, cast

import pytest
from sqlalchemy.orm import Session, sessionmaker

from integration_service.commands import ApplyForLoan, ApproveLoan, CreateClient, DisburseLoan, MakeRepayment
from integration_service.database import create_session_factory
from integration_service.domain.accounting import LedgerAccounts
from integration_service.gateways.faults import FaultInjector
from integration_service.models import Operation
from integration_service.services.executor import OperationExecutor
from integration_service.services.gl_sync import GlAccountSynchronizer
from integration_service.services.handlers import build_handlers
from integration_service.services.partner import Outcome, PartnerService
from integration_service.services.queries import Queries
from integration_service.services.recovery import Recovery
from integration_service.settings import Settings
from tests.component.fakes import FakeErp, FakeFineract

LEASE_SECONDS = 1.0


class SimulatedCrash(BaseException):
    """Stands in for the process dying: it escapes every ``except Exception``."""


def crash() -> NoReturn:
    raise SimulatedCrash


@dataclass
class System:
    fineract: FakeFineract
    erp: FakeErp
    faults: FaultInjector
    partner: PartnerService
    recovery: Recovery
    queries: Queries
    session_factory: sessionmaker[Session]
    gl_sync: GlAccountSynchronizer

    def operation(self, external_reference: str) -> Operation:
        with self.session_factory() as session:
            return session.query(Operation).filter_by(external_reference=external_reference).one()

    def open_loan(self, n: int = 1) -> str:
        """A client and an approved loan, ready for disbursement. Returns the loan reference."""
        self.partner.create_client(
            CreateClient(
                external_reference=f"CLI-{n}",
                client_uid=f"SME-00000{n}",
                first_name="Amina",
                last_name="Uwase",
                activation_date=date(2026, 1, 10),
            )
        )
        application = self.partner.apply_for_loan(
            ApplyForLoan(
                external_reference=f"APP-{n}",
                client_uid=f"SME-00000{n}",
                principal=Decimal(5_000_000),
                annual_interest_rate=Decimal(18),
                term_months=12,
                submitted_on=date(2026, 1, 15),
                expected_disbursement_on=date(2026, 1, 15),
            )
        )
        reference = str(application.body["loan_reference"])
        self.partner.approve_loan(reference, ApproveLoan(external_reference=f"APR-{n}", approved_on=date(2026, 1, 15)))
        return reference

    def disburse(self, loan_reference: str, external_reference: str = "DISB-1", amount: int = 5_000_000) -> Outcome:
        command = DisburseLoan(
            external_reference=external_reference, amount=Decimal(amount), transaction_date=date(2026, 1, 15)
        )
        return self.partner.disburse(loan_reference, command)

    def repay(self, loan_reference: str, external_reference: str, amount: int = 500_000) -> Outcome:
        command = MakeRepayment(
            external_reference=external_reference, amount=Decimal(amount), transaction_date=date(2026, 2, 15)
        )
        return self.partner.repay(loan_reference, command)


@pytest.fixture
def system(clean_database: str) -> Iterator[System]:
    settings = Settings(
        database_url=clean_database,
        fineract_attempts=2,
        fineract_backoff_seconds=0,
        recovery_backoff_seconds=0,
        lease_seconds=LEASE_SECONDS,
    )
    session_factory = create_session_factory(clean_database)
    fineract, erp, faults = FakeFineract(), FakeErp(), FaultInjector()
    gateway: Any = cast(Any, fineract)
    executor = OperationExecutor(
        session_factory, build_handlers(gateway, LedgerAccounts()), cast(Any, erp), settings, faults, crash=crash
    )
    gl_sync = GlAccountSynchronizer(session_factory, cast(Any, erp), gateway, settings)
    yield System(
        fineract=fineract,
        erp=erp,
        faults=faults,
        partner=PartnerService(session_factory, executor, settings),
        recovery=Recovery(session_factory, executor, gl_sync, settings),
        queries=Queries(session_factory, gateway),
        session_factory=session_factory,
        gl_sync=gl_sync,
    )
    session_factory.kw["bind"].dispose()
