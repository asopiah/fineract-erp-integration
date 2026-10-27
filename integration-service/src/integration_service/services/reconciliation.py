"""Compares Fineract's loan transactions with the ERP's journal entries. Read-only: it never changes either system.

Keying: one row per Fineract money transaction (disbursement or repayment), matched to the ERP journal entry with the
same ``external_reference``. ERP entries of those types with no Fineract counterpart are reported too.

Reversals: a transaction reversed in Fineract nets to zero. If the ERP still holds its entry (no reversing entry is
posted automatically), the pair is an AMOUNT_MISMATCH; if the ERP never received it, the pair matches at zero.
Repairs are explicit and separate: ``POST /api/admin/recovery`` re-drives pending ERP postings.
"""

from collections import Counter
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from integration_service.domain.erp import ErpJournalEntry
from integration_service.domain.fineract import FineractLoanTransaction, LoanTransactionType
from integration_service.domain.operations import OperationType
from integration_service.gateways.erp import ErpGateway
from integration_service.gateways.fineract import FineractGateway
from integration_service.models import Loan, LoanTransaction, Operation

MIRRORED_TYPES = frozenset({LoanTransactionType.DISBURSEMENT, LoanTransactionType.REPAYMENT})


class Outcome(StrEnum):
    MATCHED = "MATCHED"
    MISSING_IN_ERP = "MISSING_IN_ERP"
    MISSING_IN_FINERACT = "MISSING_IN_FINERACT"
    AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
    TYPE_MISMATCH = "TYPE_MISMATCH"


@dataclass(frozen=True)
class FineractSide:
    loan_reference: str
    transaction: FineractLoanTransaction


@dataclass(frozen=True)
class Criteria:
    from_date: date | None = None
    to_date: date | None = None
    external_references: frozenset[str] | None = None


def compare(fineract: FineractSide | None, erp: ErpJournalEntry | None) -> tuple[Outcome, str | None]:
    """The reconciliation rule for one external reference."""
    if fineract is None:
        return Outcome.MISSING_IN_FINERACT, None
    transaction = fineract.transaction
    if erp is None:
        if transaction.reversed:
            return Outcome.MATCHED, "reversed in Fineract and never posted to the ERP"
        return Outcome.MISSING_IN_ERP, None
    if erp.transaction_type != transaction.type.value:
        return Outcome.TYPE_MISMATCH, f"Fineract {transaction.type.value}, ERP {erp.transaction_type}"
    if transaction.reversed:
        return Outcome.AMOUNT_MISMATCH, "reversed in Fineract; the ERP entry still stands"
    if erp.total != transaction.amount:
        return Outcome.AMOUNT_MISMATCH, None
    return Outcome.MATCHED, None


class Reconciler:
    def __init__(self, session_factory: sessionmaker[Session], fineract: FineractGateway, erp: ErpGateway) -> None:
        self._session_factory = session_factory
        self._fineract = fineract
        self._erp = erp

    def reconcile(self, criteria: Criteria) -> dict[str, Any]:
        if criteria.external_references is not None:
            fineract_side, erp_side = self._by_references(criteria.external_references)
        else:
            assert criteria.from_date is not None
            assert criteria.to_date is not None
            fineract_side, erp_side = self._by_dates(criteria.from_date, criteria.to_date)

        items = []
        for reference in sorted(fineract_side.keys() | erp_side.keys()):
            outcome, note = compare(fineract_side.get(reference), erp_side.get(reference))
            items.append(_item(reference, fineract_side.get(reference), erp_side.get(reference), outcome, note))
        return {
            "criteria": {
                "from_date": criteria.from_date.isoformat() if criteria.from_date else None,
                "to_date": criteria.to_date.isoformat() if criteria.to_date else None,
                "external_references": sorted(criteria.external_references) if criteria.external_references else None,
            },
            "summary": {outcome.value: count for outcome, count in Counter(item["outcome"] for item in items).items()},
            "items": items,
        }

    def _by_dates(self, from_date: date, to_date: date) -> tuple[dict[str, FineractSide], dict[str, ErpJournalEntry]]:
        with self._session_factory() as session:
            loan_references = list(
                session.scalars(select(Loan.loan_reference).where(Loan.fineract_loan_id.isnot(None)))
            )
        fineract_side: dict[str, FineractSide] = {}
        for loan_reference in loan_references:
            loan = self._fineract.find_loan(loan_reference)
            for transaction in loan.transactions if loan is not None else ():
                if transaction.type in MIRRORED_TYPES and from_date <= transaction.transaction_date <= to_date:
                    key = transaction.external_reference or f"(no external reference) {loan_reference}"
                    fineract_side[key] = FineractSide(loan_reference, transaction)
        erp_side = {
            entry.external_reference: entry
            for entry in self._erp.list_journal_entries(from_date, to_date)
            if entry.transaction_type in {kind.value for kind in MIRRORED_TYPES}
        }
        return fineract_side, erp_side

    def _by_references(self, references: frozenset[str]) -> tuple[dict[str, FineractSide], dict[str, ErpJournalEntry]]:
        with self._session_factory() as session:
            loans_by_reference = _loans_by_reference(session, references)
        fineract_side: dict[str, FineractSide] = {}
        erp_side: dict[str, ErpJournalEntry] = {}
        for reference in references:
            loan_reference = loans_by_reference.get(reference)
            if loan_reference is not None:
                transaction = self._fineract.find_loan_transaction(loan_reference, reference)
                if transaction is not None:
                    fineract_side[reference] = FineractSide(loan_reference, transaction)
            entry = self._erp.find_journal_entry(reference)
            if entry is not None:
                erp_side[reference] = entry
        return fineract_side, erp_side


def _loans_by_reference(session: Session, references: frozenset[str]) -> dict[str, str]:
    """Which loan each money reference belongs to, from what was requested or recorded."""
    requested = session.execute(
        select(Operation.external_reference, Operation.loan_reference).where(
            Operation.external_reference.in_(references),
            Operation.type.in_((OperationType.DISBURSEMENT, OperationType.REPAYMENT)),
        )
    ).tuples()
    recorded = session.execute(
        select(LoanTransaction.external_reference, LoanTransaction.loan_reference).where(
            LoanTransaction.external_reference.in_(references)
        )
    ).tuples()
    loans: dict[str, str] = {}
    for reference, loan_reference in [*requested, *recorded]:
        if loan_reference is not None:
            loans[reference] = loan_reference
    return loans


def _item(
    reference: str,
    fineract: FineractSide | None,
    erp: ErpJournalEntry | None,
    outcome: Outcome,
    note: str | None,
) -> dict[str, Any]:
    return {
        "external_reference": reference,
        "loan_reference": fineract.loan_reference if fineract else None,
        "outcome": outcome.value,
        "note": note,
        "fineract": None
        if fineract is None
        else {
            "type": fineract.transaction.type.value,
            "transaction_date": fineract.transaction.transaction_date.isoformat(),
            "amount": _money(fineract.transaction.amount),
            "reversed": fineract.transaction.reversed,
        },
        "erp": None
        if erp is None
        else {
            "entry_number": erp.entry_number,
            "type": erp.transaction_type,
            "entry_date": erp.entry_date.isoformat(),
            "amount": _money(erp.total),
        },
    }


def _money(value: Decimal) -> str:
    return format(value.normalize(), "f")
