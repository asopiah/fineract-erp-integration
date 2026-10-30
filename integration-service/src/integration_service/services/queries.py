"""Read side of the partner API. Balances and statuses are read live from Fineract, the system of record."""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from integration_service.domain.fineract import FineractLoanTransaction
from integration_service.domain.money import plain_decimal
from integration_service.domain.operations import OperationStatus, OperationType
from integration_service.errors import ApiError, ErrorCode
from integration_service.gateways.fineract import FineractGateway
from integration_service.models import Client, Loan, LoanTransaction, Operation
from integration_service.services.handlers import transaction_body

MONEY_OPERATIONS = (OperationType.DISBURSEMENT, OperationType.REPAYMENT)


class Queries:
    def __init__(self, session_factory: sessionmaker[Session], fineract: FineractGateway) -> None:
        self._session_factory = session_factory
        self._fineract = fineract

    def client(self, client_uid: str) -> dict[str, Any]:
        """The client as Fineract holds it: exists, active, and named as requested."""
        with self._session_factory() as session:
            client = session.scalar(select(Client).where(Client.client_uid == client_uid))
        if client is None:
            raise ApiError(404, ErrorCode.NOT_FOUND, f"client {client_uid} does not exist")
        in_fineract = self._fineract.find_client(client_uid)
        expected_name = f"{client.first_name} {client.last_name}"
        return {
            "client_uid": client.client_uid,
            "external_reference": client.external_reference,
            "first_name": client.first_name,
            "last_name": client.last_name,
            "fineract": None
            if in_fineract is None
            else {
                "exists": True,
                "active": in_fineract.active,
                "status": in_fineract.status,
                "display_name": in_fineract.display_name,
                "name_matches": in_fineract.display_name == expected_name,
            },
        }

    def loan(self, loan_reference: str) -> dict[str, Any]:
        with self._session_factory() as session:
            loan = session.scalar(select(Loan).where(Loan.loan_reference == loan_reference))
        if loan is None:
            raise ApiError(404, ErrorCode.NOT_FOUND, f"loan {loan_reference} does not exist")
        in_fineract = self._fineract.find_loan(loan_reference)
        body: dict[str, Any] = {
            "loan_reference": loan.loan_reference,
            "external_reference": loan.external_reference,
            "client_uid": loan.client_uid,
            "principal": plain_decimal(loan.principal),
            "annual_interest_rate": plain_decimal(loan.annual_interest_rate),
            "term_months": loan.term_months,
            "submitted_on": loan.submitted_on.isoformat(),
            "status": None,
        }
        if in_fineract is None:
            return body
        balances = in_fineract.balances
        return body | {
            "status": in_fineract.status.value,
            "fineract_status": in_fineract.fineract_status,
            "outstanding": {
                "principal": plain_decimal(balances.principal_outstanding),
                "interest": plain_decimal(balances.interest_outstanding),
                "fees": plain_decimal(balances.fees_outstanding),
                "penalties": plain_decimal(balances.penalties_outstanding),
                "total": plain_decimal(balances.total_outstanding),
            },
            "transactions": [_fineract_transaction(transaction) for transaction in in_fineract.transactions],
        }

    def transaction(self, external_reference: str) -> dict[str, Any]:
        """Everything known about one money movement, in one call: its lifecycle, Fineract's record, the ERP entry."""
        with self._session_factory() as session:
            # At most one: money references are unique across both types (uq_operations_money_reference).
            operation = session.scalar(
                select(Operation).where(
                    Operation.external_reference == external_reference, Operation.type.in_(MONEY_OPERATIONS)
                )
            )
            if operation is None:
                raise ApiError(404, ErrorCode.NOT_FOUND, f"no disbursement or repayment {external_reference}")
            recorded = session.scalar(
                select(LoanTransaction).where(LoanTransaction.external_reference == external_reference)
            )
            return {
                "external_reference": operation.external_reference,
                "type": operation.type.value,
                "loan_reference": operation.loan_reference,
                "client_uid": operation.client_uid,
                "status": operation.status.value,
                "correlation_id": operation.correlation_id,
                "attempts": operation.attempts,
                "next_attempt_at": operation.next_attempt_at.isoformat() if operation.next_attempt_at else None,
                "error": operation.error,
                "requested": operation.request,
                "recorded": transaction_body(recorded) if recorded is not None else None,
                "created_at": operation.created_at.isoformat(),
                "updated_at": operation.updated_at.isoformat(),
            }

    def operations(self, status: OperationStatus) -> list[dict[str, Any]]:
        """Operations in one status, oldest first: e.g. the NEEDS_RECONCILIATION work queue."""
        with self._session_factory() as session:
            rows = session.scalars(select(Operation).where(Operation.status == status).order_by(Operation.id)).all()
            return [
                {
                    "type": row.type.value,
                    "external_reference": row.external_reference,
                    "loan_reference": row.loan_reference,
                    "status": row.status.value,
                    "attempts": row.attempts,
                    "error": row.error,
                    "updated_at": row.updated_at.isoformat(),
                }
                for row in rows
            ]


def _fineract_transaction(transaction: FineractLoanTransaction) -> dict[str, Any]:
    return {
        "external_reference": transaction.external_reference,
        "type": transaction.type.value,
        "fineract_type": transaction.fineract_type,
        "transaction_date": transaction.transaction_date.isoformat(),
        "amount": plain_decimal(transaction.amount),
        "principal_portion": plain_decimal(transaction.principal),
        "interest_portion": plain_decimal(transaction.interest),
        "fee_portion": plain_decimal(transaction.fees),
        "penalty_portion": plain_decimal(transaction.penalties),
        "overpayment_portion": plain_decimal(transaction.overpayment),
        "outstanding_after": plain_decimal(transaction.outstanding_after),
        "reversed": transaction.reversed,
    }
