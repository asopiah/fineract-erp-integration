"""Operator endpoints: explicit recovery, the reconciliation work queue, and (test stacks only) fault injection."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from integration_service.api.dependencies import ServicesDep, require_api_token
from integration_service.domain.operations import OperationStatus
from integration_service.gateways.faults import FaultConfig

admin = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_api_token)])
faults = APIRouter(prefix="/api/admin/faults", tags=["fault injection"], dependencies=[Depends(require_api_token)])


@admin.post("/recovery")
def run_recovery(services: ServicesDep) -> dict[str, int]:
    """Resumes interrupted operations and retries failed GL syncs now, instead of at the next scheduled sweep."""
    report = services.recovery.run_once()
    return {"operations_resumed": report.operations_resumed, "gl_accounts_retried": report.gl_accounts_retried}


@admin.get("/operations")
def list_operations(
    services: ServicesDep, status: Annotated[OperationStatus, Query()] = OperationStatus.NEEDS_RECONCILIATION
) -> list[dict[str, Any]]:
    """Operations in one status; by default those waiting for a human (a dead-letter view)."""
    return services.queries.operations(status)


@faults.get("", response_model=FaultConfig)
def get_faults(services: ServicesDep) -> FaultConfig:
    return services.faults.config


@faults.put("", response_model=FaultConfig)
def set_faults(config: FaultConfig, services: ServicesDep) -> FaultConfig:
    services.faults.configure(config)
    return services.faults.config
