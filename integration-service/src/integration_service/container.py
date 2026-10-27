"""Composition root: builds the object graph once, from settings."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import NoReturn

import httpx
from sqlalchemy.orm import Session, sessionmaker

from integration_service.database import create_session_factory
from integration_service.domain.accounting import LedgerAccounts
from integration_service.gateways.erp import ErpGateway
from integration_service.gateways.faults import FaultInjector
from integration_service.gateways.fineract import FineractGateway
from integration_service.services.executor import OperationExecutor
from integration_service.services.gl_sync import GlAccountSynchronizer
from integration_service.services.handlers import build_handlers
from integration_service.services.partner import PartnerService
from integration_service.services.queries import Queries
from integration_service.services.reconciliation import Reconciler
from integration_service.services.recovery import Recovery
from integration_service.settings import Settings


@dataclass(frozen=True)
class Services:
    settings: Settings
    session_factory: sessionmaker[Session]
    faults: FaultInjector
    fineract: FineractGateway
    erp: ErpGateway
    executor: OperationExecutor
    partner: PartnerService
    gl_sync: GlAccountSynchronizer
    recovery: Recovery
    reconciler: Reconciler
    queries: Queries


def build_services(
    settings: Settings,
    *,
    fineract_transport: httpx.BaseTransport | None = None,
    erp_transport: httpx.BaseTransport | None = None,
    crash: Callable[[], NoReturn] | None = None,
) -> Services:
    session_factory = create_session_factory(settings.database_url)
    faults = FaultInjector()
    transport = fineract_transport or httpx.HTTPTransport()
    fineract = FineractGateway(settings, faults.wrap(transport) if settings.fault_injection_enabled else transport)
    erp = ErpGateway(settings, erp_transport)
    executor_options = {"crash": crash} if crash is not None else {}
    executor = OperationExecutor(
        session_factory, build_handlers(fineract, LedgerAccounts()), erp, settings, faults, **executor_options
    )
    gl_sync = GlAccountSynchronizer(session_factory, erp, fineract, settings)
    return Services(
        settings=settings,
        session_factory=session_factory,
        faults=faults,
        fineract=fineract,
        erp=erp,
        executor=executor,
        partner=PartnerService(session_factory, executor, settings),
        gl_sync=gl_sync,
        recovery=Recovery(session_factory, executor, gl_sync, settings),
        reconciler=Reconciler(session_factory, fineract, erp),
        queries=Queries(session_factory, fineract),
    )
