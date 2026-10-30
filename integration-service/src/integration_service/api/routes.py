"""The partner API. Every state-changing request carries an ``external_reference``; see ``services.partner``."""

from typing import Any

from fastapi import APIRouter, Depends, Path
from fastapi.responses import JSONResponse

from integration_service.api.dependencies import ServicesDep, require_api_token
from integration_service.api.schemas import (
    ApprovalOut,
    ClientOut,
    ClientView,
    ErrorOut,
    GlAccountSyncOut,
    GlAccountSyncRequest,
    LoanApplicationOut,
    LoanView,
    PendingOut,
    ReconciliationReport,
    ReconciliationRequest,
    TransactionOut,
    TransactionView,
)
from integration_service.commands import (
    ApplyForLoan,
    ApproveLoan,
    ClientUid,
    CreateClient,
    DisburseLoan,
    ExternalReference,
    LoanReference,
    MakeRepayment,
)
from integration_service.errors import ApiError, ErrorCode
from integration_service.models import GlAccountSync
from integration_service.services.partner import Outcome
from integration_service.services.reconciliation import Criteria

router = APIRouter(prefix="/api", dependencies=[Depends(require_api_token)])


def _command_responses(model: type[Any], conflict: str) -> dict[int | str, dict[str, Any]]:
    return {
        201: {"model": model, "description": "Processed by this request."},
        200: {"model": model, "description": "Replay of a completed request: the original result."},
        202: {"model": PendingOut, "description": "Already in progress, or awaiting reconciliation."},
        409: {"model": ErrorOut, "description": conflict},
        422: {"model": ErrorOut, "description": "Invalid request, or rejected by Fineract."},
        503: {"model": ErrorOut, "description": "Fineract did not process the request; safe to retry."},
    }


def _respond(outcome: Outcome) -> JSONResponse:
    return JSONResponse(outcome.body, status_code=outcome.status_code)


LoanReferencePath = Path(examples=["LN-2026-000001"])


# --- GL accounts -----------------------------------------------------------------------------------------------


@router.post("/accounts/sync", tags=["GL accounts"], response_model=list[GlAccountSyncOut])
def sync_gl_accounts(services: ServicesDep, body: GlAccountSyncRequest | None = None) -> list[GlAccountSyncOut]:
    """Creates the ERP's accounts in Fineract (create-only; repeating it is harmless)."""
    only = set(body.external_references) if body is not None and body.external_references is not None else None
    return [_sync_out(row) for row in services.gl_sync.sync_chart(only)]


@router.get("/accounts/sync", tags=["GL accounts"], response_model=list[GlAccountSyncOut])
def gl_account_sync_status(services: ServicesDep) -> list[GlAccountSyncOut]:
    return [_sync_out(row) for row in services.gl_sync.statuses()]


# --- Clients ---------------------------------------------------------------------------------------------------


@router.post(
    "/clients",
    tags=["clients"],
    status_code=201,
    responses=_command_responses(ClientOut, "Reference reused with another payload, or client_uid taken."),
)
def create_client(command: CreateClient, services: ServicesDep) -> JSONResponse:
    return _respond(services.partner.create_client(command))


@router.get("/clients/{client_uid}", tags=["clients"], response_model=ClientView)
def get_client(client_uid: ClientUid, services: ServicesDep) -> dict[str, Any]:
    return services.queries.client(client_uid)


# --- Loans -----------------------------------------------------------------------------------------------------


@router.post(
    "/loans",
    tags=["loans"],
    status_code=201,
    responses=_command_responses(LoanApplicationOut, "Reference reused with another payload."),
)
def apply_for_loan(command: ApplyForLoan, services: ServicesDep) -> JSONResponse:
    """Submits a loan application. The response carries the ``loan_reference`` issued for it."""
    return _respond(services.partner.apply_for_loan(command))


@router.get("/loans/{loan_reference}", tags=["loans"], response_model=LoanView)
def get_loan(services: ServicesDep, loan_reference: LoanReference = LoanReferencePath) -> dict[str, Any]:
    """Status, balances and transactions, read live from Fineract."""
    return services.queries.loan(loan_reference)


@router.post(
    "/loans/{loan_reference}/approve",
    tags=["loans"],
    status_code=201,
    responses=_command_responses(ApprovalOut, "Reference reused, or the loan is already approved."),
)
def approve_loan(
    command: ApproveLoan, services: ServicesDep, loan_reference: LoanReference = LoanReferencePath
) -> JSONResponse:
    return _respond(services.partner.approve_loan(loan_reference, command))


@router.post(
    "/loans/{loan_reference}/disburse",
    tags=["loans"],
    status_code=201,
    responses=_command_responses(TransactionOut, "Reference reused, or the loan already has a disbursement."),
)
def disburse_loan(
    command: DisburseLoan, services: ServicesDep, loan_reference: LoanReference = LoanReferencePath
) -> JSONResponse:
    """Single disbursement: exactly one per loan, whatever references concurrent callers use."""
    return _respond(services.partner.disburse(loan_reference, command))


@router.post(
    "/loans/{loan_reference}/repayments",
    tags=["loans"],
    status_code=201,
    responses=_command_responses(TransactionOut, "Reference reused with another payload."),
)
def make_repayment(
    command: MakeRepayment, services: ServicesDep, loan_reference: LoanReference = LoanReferencePath
) -> JSONResponse:
    """Each external reference is applied once; Fineract decides the principal/interest split."""
    return _respond(services.partner.repay(loan_reference, command))


# --- Transactions and reconciliation ---------------------------------------------------------------------------


@router.get("/transactions/{external_reference}", tags=["transactions"], response_model=TransactionView)
def get_transaction(external_reference: ExternalReference, services: ServicesDep) -> dict[str, Any]:
    """What happened to a disbursement or repayment: lifecycle status, Fineract's record and the ERP entry."""
    return services.queries.transaction(external_reference)


@router.post("/reconciliation", tags=["reconciliation"], response_model=ReconciliationReport)
def reconcile(body: ReconciliationRequest, services: ServicesDep) -> dict[str, Any]:
    """Read-only comparison of Fineract transactions and ERP journal entries, by date range or by references."""
    if body.external_references is None and (body.from_date is None or body.to_date is None):
        raise ApiError(422, ErrorCode.VALIDATION_ERROR, "give either external_references or from_date and to_date")
    return services.reconciler.reconcile(
        Criteria(
            from_date=body.from_date,
            to_date=body.to_date,
            external_references=frozenset(body.external_references) if body.external_references is not None else None,
        )
    )


def _sync_out(row: GlAccountSync) -> GlAccountSyncOut:
    return GlAccountSyncOut(
        external_reference=row.external_reference,
        code=row.code,
        name=row.name,
        erp_type=row.erp_type,
        status=row.status.value,
        attempts=row.attempts,
        last_error=row.last_error,
    )
