from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from integration_service.domain.accounting import (
    JournalLine,
    LedgerAccounts,
    UnmappableTransactionError,
    journal_entry_for,
)
from integration_service.domain.fineract import FineractLoanTransaction, LoanTransactionType

LEDGER = LedgerAccounts()
ZERO = Decimal(0)


def transaction(kind: LoanTransactionType, amount: str, **portions: str) -> FineractLoanTransaction:
    values = {name: Decimal(portions.get(name, "0")) for name in ("principal", "interest", "fees", "penalties")}
    return FineractLoanTransaction(
        internal_id=7,
        external_reference="REF-1",
        type=kind,
        fineract_type=kind.value.capitalize(),
        transaction_date=date(2026, 2, 15),
        amount=Decimal(amount),
        overpayment=Decimal(portions.get("overpayment", "0")),
        outstanding_after=ZERO,
        reversed=False,
        **values,
    )


def test_disbursement_debits_receivable_and_credits_bank() -> None:
    entry = journal_entry_for(transaction(LoanTransactionType.DISBURSEMENT, "5000000"), "DISB-1", "LN-1", LEDGER)

    assert entry.lines == (
        JournalLine("1200", debit=Decimal(5000000)),
        JournalLine("1100", credit=Decimal(5000000)),
    )
    assert entry.is_balanced
    assert entry.transaction_type == "DISBURSEMENT"
    assert entry.entry_date == date(2026, 2, 15)


def test_repayment_splits_credits_exactly_as_fineract_allocated() -> None:
    posted = transaction(LoanTransactionType.REPAYMENT, "458400", principal="383400", interest="75000")

    entry = journal_entry_for(posted, "REP-1", "LN-1", LEDGER)

    assert entry.lines == (
        JournalLine("1100", debit=Decimal(458400)),
        JournalLine("1200", credit=Decimal(383400)),
        JournalLine("4000", credit=Decimal(75000)),
    )
    assert entry.is_balanced


def test_fees_penalties_and_overpayment_have_their_own_accounts() -> None:
    posted = transaction(
        LoanTransactionType.REPAYMENT,
        "1000",
        principal="500",
        interest="200",
        fees="100",
        penalties="50",
        overpayment="150",
    )

    credits = {line.account_code: line.credit for line in journal_entry_for(posted, "R", "L", LEDGER).lines[1:]}

    assert credits == {"1200": 500, "4000": 200, "4100": 100, "4200": 50, "2100": 150}


def test_a_repayment_fineract_did_not_fully_allocate_is_not_posted() -> None:
    posted = transaction(LoanTransactionType.REPAYMENT, "1000", principal="900")

    with pytest.raises(UnmappableTransactionError, match="would not balance"):
        journal_entry_for(posted, "R", "L", LEDGER)


def test_reversed_and_unmirrored_transactions_are_not_posted() -> None:
    reversed_repayment = replace(transaction(LoanTransactionType.REPAYMENT, "100", principal="100"), reversed=True)
    accrual = transaction(LoanTransactionType.OTHER, "100")

    with pytest.raises(UnmappableTransactionError, match="reversed"):
        journal_entry_for(reversed_repayment, "R", "L", LEDGER)
    with pytest.raises(UnmappableTransactionError, match="no journal mapping"):
        journal_entry_for(accrual, "R", "L", LEDGER)


def test_mapping_is_deterministic() -> None:
    posted = transaction(LoanTransactionType.REPAYMENT, "458400", principal="383400", interest="75000")

    assert journal_entry_for(posted, "R", "L", LEDGER) == journal_entry_for(posted, "R", "L", LEDGER)
