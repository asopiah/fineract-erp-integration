"""How each operation type is carried out in Fineract (and, for money, mirrored into the ERP).

The executor always calls ``find`` before ``submit``, so a retry never submits twice. ``release`` frees the business
keys an operation reserved, and is called only once Fineract has definitively rejected it.
"""

from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from integration_service.commands import ApplyForLoan, ApproveLoan, CreateClient, DisburseLoan, MakeRepayment
from integration_service.domain.accounting import JournalEntryDraft, LedgerAccounts, journal_entry_for
from integration_service.domain.erp import ErpJournalEntry
from integration_service.domain.fineract import (
    FineractClient,
    FineractLoan,
    FineractLoanTransaction,
    LoanTransactionType,
)
from integration_service.domain.money import plain_decimal
from integration_service.domain.operations import OperationType
from integration_service.gateways.fineract import FineractGateway
from integration_service.models import Client, Loan, LoanTransaction, Operation


class OperationHandler[R](ABC):
    def __init__(self, fineract: FineractGateway) -> None:
        self._fineract = fineract

    @abstractmethod
    def find(self, session: Session, operation: Operation) -> R | None: ...

    @abstractmethod
    def submit(self, session: Session, operation: Operation) -> None: ...

    def contradiction(self, operation: Operation, record: R) -> str | None:
        return None

    def release(self, session: Session, operation: Operation) -> None:  # noqa: B027  (optional hook)
        """Nothing reserved by default."""

    @abstractmethod
    def record(self, session: Session, operation: Operation, record: R) -> None: ...

    @abstractmethod
    def result(self, session: Session, operation: Operation) -> dict[str, Any]: ...


class ClientCreation(OperationHandler[FineractClient]):
    def find(self, session: Session, operation: Operation) -> FineractClient | None:
        return self._fineract.find_client(_client_uid(operation))

    def submit(self, session: Session, operation: Operation) -> None:
        command = CreateClient.model_validate(operation.request)
        self._fineract.create_client(command.client_uid, command.first_name, command.last_name, command.activation_date)

    def contradiction(self, operation: Operation, record: FineractClient) -> str | None:
        command = CreateClient.model_validate(operation.request)
        expected = f"{command.first_name} {command.last_name}"
        if record.display_name != expected:
            return f"Fineract client {command.client_uid} is named {record.display_name!r}, not {expected!r}"
        return None

    def record(self, session: Session, operation: Operation, record: FineractClient) -> None:
        _client(session, _client_uid(operation)).fineract_client_id = record.internal_id

    def release(self, session: Session, operation: Operation) -> None:
        session.delete(_client(session, _client_uid(operation)))

    def result(self, session: Session, operation: Operation) -> dict[str, Any]:
        client = _client(session, _client_uid(operation))
        return {
            "external_reference": client.external_reference,
            "client_uid": client.client_uid,
            "first_name": client.first_name,
            "last_name": client.last_name,
        }


class LoanApplication(OperationHandler[FineractLoan]):
    def find(self, session: Session, operation: Operation) -> FineractLoan | None:
        return self._fineract.find_loan(_loan_reference(operation))

    def submit(self, session: Session, operation: Operation) -> None:
        command = ApplyForLoan.model_validate(operation.request)
        fineract_client_id = _client(session, command.client_uid).fineract_client_id
        if fineract_client_id is None:
            raise RuntimeError(f"client {command.client_uid} has no Fineract mapping")
        self._fineract.submit_loan_application(
            _loan_reference(operation),
            fineract_client_id,
            command.principal,
            command.annual_interest_rate,
            command.term_months,
            command.submitted_on,
            command.expected_disbursement_on,
        )

    def contradiction(self, operation: Operation, record: FineractLoan) -> str | None:
        command = ApplyForLoan.model_validate(operation.request)
        if record.principal != command.principal:
            return f"Fineract loan {record.loan_reference} has principal {record.principal}, not {command.principal}"
        return None

    def record(self, session: Session, operation: Operation, record: FineractLoan) -> None:
        _loan(session, _loan_reference(operation)).fineract_loan_id = record.internal_id

    def release(self, session: Session, operation: Operation) -> None:
        session.delete(_loan(session, _loan_reference(operation)))

    def result(self, session: Session, operation: Operation) -> dict[str, Any]:
        loan = _loan(session, _loan_reference(operation))
        return {
            "external_reference": loan.external_reference,
            "loan_reference": loan.loan_reference,
            "client_uid": loan.client_uid,
            "principal": plain_decimal(loan.principal),
            "annual_interest_rate": plain_decimal(loan.annual_interest_rate),
            "term_months": loan.term_months,
            "submitted_on": loan.submitted_on.isoformat(),
            "expected_disbursement_on": loan.expected_disbursement_on.isoformat(),
        }


class LoanApproval(OperationHandler[FineractLoan]):
    def find(self, session: Session, operation: Operation) -> FineractLoan | None:
        loan = self._fineract.find_loan(_loan_reference(operation))
        return loan if loan is not None and loan.status.is_approved_or_later else None

    def submit(self, session: Session, operation: Operation) -> None:
        command = ApproveLoan.model_validate(operation.request)
        loan = _loan(session, _loan_reference(operation))
        self._fineract.approve_loan(loan.loan_reference, command.approved_on, loan.expected_disbursement_on)

    def record(self, session: Session, operation: Operation, record: FineractLoan) -> None:
        """Nothing to keep: the loan's status is read from Fineract whenever it is needed."""

    def result(self, session: Session, operation: Operation) -> dict[str, Any]:
        command = ApproveLoan.model_validate(operation.request)
        return {
            "external_reference": command.external_reference,
            "loan_reference": _loan_reference(operation),
            "approved_on": command.approved_on.isoformat(),
        }


class MoneyMovement(OperationHandler[FineractLoanTransaction]):
    """A disbursement or repayment: a Fineract loan transaction carrying our external reference, mirrored as an ERP
    journal entry derived from Fineract's own breakdown."""

    transaction_type: LoanTransactionType

    def __init__(self, fineract: FineractGateway, ledger: LedgerAccounts) -> None:
        super().__init__(fineract)
        self._ledger = ledger

    def find(self, session: Session, operation: Operation) -> FineractLoanTransaction | None:
        return self._fineract.find_loan_transaction(_loan_reference(operation), operation.external_reference)

    def contradiction(self, operation: Operation, record: FineractLoanTransaction) -> str | None:
        requested = Decimal(operation.request["amount"])
        if record.type is not self.transaction_type:
            return (
                f"Fineract transaction {operation.external_reference} is a {record.type}, not a {self.transaction_type}"
            )
        if record.amount != requested:
            return f"Fineract recorded {record.amount} for {operation.external_reference}, the request was {requested}"
        if record.reversed:
            return f"Fineract transaction {operation.external_reference} has been reversed"
        return None

    def stored_contradiction(
        self, session: Session, operation: Operation, record: FineractLoanTransaction
    ) -> str | None:
        """Does Fineract's current record disagree with the request, or with what was stored from it earlier?"""
        problem = self.contradiction(operation, record)
        if problem is not None:
            return problem
        stored = _transaction(session, operation.external_reference)
        if _breakdown(record) != _breakdown(_recorded(stored)):
            return f"Fineract's record of {operation.external_reference} changed since it was stored"
        return None

    def record(self, session: Session, operation: Operation, record: FineractLoanTransaction) -> None:
        if session.scalar(
            select(LoanTransaction.id).where(LoanTransaction.external_reference == operation.external_reference)
        ):
            return
        session.add(
            LoanTransaction(
                external_reference=operation.external_reference,
                loan_reference=_loan_reference(operation),
                type=record.type,
                transaction_date=record.transaction_date,
                amount=record.amount,
                principal_portion=record.principal,
                interest_portion=record.interest,
                fee_portion=record.fees,
                penalty_portion=record.penalties,
                overpayment_portion=record.overpayment,
                outstanding_after=record.outstanding_after,
                fineract_transaction_id=record.internal_id,
            )
        )

    def journal_entry(self, session: Session, operation: Operation) -> JournalEntryDraft:
        """Built only from what Fineract recorded (the stored transaction), so every attempt sends the same entry."""
        posted = _recorded(_transaction(session, operation.external_reference))
        return journal_entry_for(posted, operation.external_reference, _loan_reference(operation), self._ledger)

    def record_journal(self, session: Session, operation: Operation, entry: ErpJournalEntry) -> None:
        _transaction(session, operation.external_reference).erp_entry_number = entry.entry_number

    def result(self, session: Session, operation: Operation) -> dict[str, Any]:
        return transaction_body(_transaction(session, operation.external_reference))


class Disbursement(MoneyMovement):
    transaction_type = LoanTransactionType.DISBURSEMENT

    def submit(self, session: Session, operation: Operation) -> None:
        command = DisburseLoan.model_validate(operation.request)
        self._fineract.disburse_loan(
            _loan_reference(operation), command.external_reference, command.amount, command.transaction_date
        )


class Repayment(MoneyMovement):
    transaction_type = LoanTransactionType.REPAYMENT

    def submit(self, session: Session, operation: Operation) -> None:
        command = MakeRepayment.model_validate(operation.request)
        self._fineract.make_repayment(
            _loan_reference(operation), command.external_reference, command.amount, command.transaction_date
        )


def build_handlers(fineract: FineractGateway, ledger: LedgerAccounts) -> dict[OperationType, OperationHandler[Any]]:
    return {
        OperationType.CLIENT_CREATION: ClientCreation(fineract),
        OperationType.LOAN_APPLICATION: LoanApplication(fineract),
        OperationType.LOAN_APPROVAL: LoanApproval(fineract),
        OperationType.DISBURSEMENT: Disbursement(fineract, ledger),
        OperationType.REPAYMENT: Repayment(fineract, ledger),
    }


def transaction_body(transaction: LoanTransaction) -> dict[str, Any]:
    """The public view of a money movement: Fineract's breakdown, the ERP's entry, no Fineract ids."""
    return {
        "external_reference": transaction.external_reference,
        "loan_reference": transaction.loan_reference,
        "type": transaction.type.value,
        "transaction_date": transaction.transaction_date.isoformat(),
        "amount": plain_decimal(transaction.amount),
        "principal_portion": plain_decimal(transaction.principal_portion),
        "interest_portion": plain_decimal(transaction.interest_portion),
        "fee_portion": plain_decimal(transaction.fee_portion),
        "penalty_portion": plain_decimal(transaction.penalty_portion),
        "overpayment_portion": plain_decimal(transaction.overpayment_portion),
        "outstanding_after": plain_decimal(transaction.outstanding_after),
        "erp_entry_number": transaction.erp_entry_number,
    }


def _recorded(stored: LoanTransaction) -> FineractLoanTransaction:
    # Only unreversed transactions are stored; operator repair re-checks that in Fineract first.
    return FineractLoanTransaction(
        internal_id=stored.fineract_transaction_id,
        external_reference=stored.external_reference,
        type=stored.type,
        fineract_type=stored.type.value,
        transaction_date=stored.transaction_date,
        amount=stored.amount,
        principal=stored.principal_portion,
        interest=stored.interest_portion,
        fees=stored.fee_portion,
        penalties=stored.penalty_portion,
        overpayment=stored.overpayment_portion,
        outstanding_after=stored.outstanding_after,
        reversed=False,
    )


def _breakdown(transaction: FineractLoanTransaction) -> tuple[object, ...]:
    return (
        transaction.internal_id,
        transaction.type,
        transaction.transaction_date,
        transaction.amount,
        transaction.principal,
        transaction.interest,
        transaction.fees,
        transaction.penalties,
        transaction.overpayment,
    )


def _client_uid(operation: Operation) -> str:
    if operation.client_uid is None:
        raise ValueError(f"{operation.type} {operation.external_reference} has no client_uid")
    return operation.client_uid


def _loan_reference(operation: Operation) -> str:
    if operation.loan_reference is None:
        raise ValueError(f"{operation.type} {operation.external_reference} has no loan_reference")
    return operation.loan_reference


def _client(session: Session, client_uid: str) -> Client:
    return session.execute(select(Client).where(Client.client_uid == client_uid)).scalar_one()


def _loan(session: Session, loan_reference: str) -> Loan:
    return session.execute(select(Loan).where(Loan.loan_reference == loan_reference)).scalar_one()


def _transaction(session: Session, external_reference: str) -> LoanTransaction:
    return session.execute(
        select(LoanTransaction).where(LoanTransaction.external_reference == external_reference)
    ).scalar_one()
