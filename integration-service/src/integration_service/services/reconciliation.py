"""Compares Fineract's loan transactions with the ERP's journal entries. Read-only: it never changes either system.

Keying: one row per Fineract money transaction (disbursement or repayment), matched to the ERP journal entry with the
same ``external_reference``. ERP entries of those types with no Fineract counterpart are reported too.

Reversals: a transaction reversed in Fineract nets to zero. If the ERP still holds its entry (no reversing entry is
posted automatically), the pair is an AMOUNT_MISMATCH; if the ERP never received it, the pair matches at zero.
Repairs are explicit and separate: ``POST /api/admin/operations/{type}/{reference}/resume`` re-drives the ERP posting of
a movement Fineract recorded; ``POST /api/admin/recovery`` resumes interrupted work.
"""

from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from integration_service.domain.accounting import reversal_journal_reference
from integration_service.domain.erp import ErpJournalEntry
from integration_service.domain.fineract import FineractLoanTransaction, LoanTransactionType
from integration_service.domain.money import plain_decimal
from integration_service.gateways.erp import ErpGateway
from integration_service.gateways.fineract import FineractGateway
from integration_service.models import Loan

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


def compare(
    fineract: FineractSide | None, erp: ErpJournalEntry | None, erp_reversal: ErpJournalEntry | None = None
) -> tuple[Outcome, str | None]:
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
        if erp_reversal is not None and erp_reversal.total == erp.total:
            return Outcome.MATCHED, f"reversed in Fineract; the ERP entry is reversed by {erp_reversal.entry_number}"
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
        elif criteria.from_date is not None and criteria.to_date is not None:
            fineract_side, erp_side = self._by_dates(criteria.from_date, criteria.to_date)
        else:
            raise ValueError("reconciliation needs external references or a date range")

        items = []
        for reference in sorted(fineract_side.keys() | erp_side.keys()):
            fineract = fineract_side.get(reference)
            reversal = (
                self._erp.find_journal_entry(reversal_journal_reference(reference))
                if fineract is not None and fineract.transaction.reversed
                else None
            )
            outcome, note = compare(fineract, erp_side.get(reference), reversal)
            items.append(_item(reference, fineract_side.get(reference), erp_side.get(reference), outcome, note))
        return {
            "criteria": {
                "from_date": criteria.from_date.isoformat() if criteria.from_date else None,
                "to_date": criteria.to_date.isoformat() if criteria.to_date else None,
                "external_references": sorted(criteria.external_references) if criteria.external_references else None,
            },
            "summary": dict(Counter(item["outcome"] for item in items)),
            "items": items,
        }

    def _by_dates(self, from_date: date, to_date: date) -> tuple[dict[str, FineractSide], dict[str, ErpJournalEntry]]:
        fineract_side = {
            key: side
            for key, side in self._fineract_transactions()
            if from_date <= side.transaction.transaction_date <= to_date
        }
        erp_side = {
            entry.external_reference: entry
            for entry in self._erp.list_journal_entries(from_date, to_date)
            if entry.transaction_type in {kind.value for kind in MIRRORED_TYPES}
        }
        return fineract_side, erp_side

    def _by_references(self, references: frozenset[str]) -> tuple[dict[str, FineractSide], dict[str, ErpJournalEntry]]:
        fineract_side = {key: side for key, side in self._fineract_transactions() if key in references}
        erp_side: dict[str, ErpJournalEntry] = {}
        for reference in references:
            entry = self._erp.find_journal_entry(reference)
            if entry is not None:
                erp_side[reference] = entry
        return fineract_side, erp_side

    def _fineract_transactions(self) -> Iterator[tuple[str, FineractSide]]:
        """Money transactions on every loan the integration originated, keyed by external reference.

        Transactions posted by hand in Fineract have no reference; each gets a descriptive key so none collapse.
        """
        with self._session_factory() as session:
            in_fineract = select(Loan.loan_reference).where(Loan.fineract_loan_id.isnot(None)).order_by(Loan.id)
            loan_references = list(session.scalars(in_fineract))
        for loan_reference in loan_references:
            loan = self._fineract.find_loan(loan_reference)
            unreferenced = 0
            for transaction in loan.transactions if loan is not None else ():
                if transaction.type not in MIRRORED_TYPES:
                    continue
                key = transaction.external_reference
                if key is None:
                    unreferenced += 1
                    key = f"(no reference) {loan_reference} {transaction.fineract_type} #{unreferenced}"
                yield key, FineractSide(loan_reference, transaction)


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
            "amount": plain_decimal(fineract.transaction.amount),
            "reversed": fineract.transaction.reversed,
        },
        "erp": None
        if erp is None
        else {
            "entry_number": erp.entry_number,
            "type": erp.transaction_type,
            "entry_date": erp.entry_date.isoformat(),
            "amount": plain_decimal(erp.total),
        },
    }
