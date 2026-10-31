"""Journal entries derived from what Fineract recorded, never from our own arithmetic.

The principal/interest/fee/penalty split comes from the portions on Fineract's loan transaction. The functions are
pure: the same Fineract transaction always yields the same, balanced entry.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from integration_service.domain.fineract import FineractLoanTransaction, LoanTransactionType

ZERO = Decimal(0)


@dataclass(frozen=True)
class LedgerAccounts:
    """ERP account codes the loan transactions post to (the ERP's chart of accounts)."""

    bank: str = "1100"
    loan_receivable: str = "1200"
    overpayment_liability: str = "2100"
    interest_income: str = "4000"
    fee_income: str = "4100"
    penalty_income: str = "4200"


@dataclass(frozen=True)
class JournalLine:
    account_code: str
    debit: Decimal = ZERO
    credit: Decimal = ZERO


@dataclass(frozen=True)
class JournalEntryDraft:
    external_reference: str
    entry_date: date
    transaction_type: str
    memo: str
    lines: tuple[JournalLine, ...]

    @property
    def is_balanced(self) -> bool:
        return sum(line.debit for line in self.lines) == sum(line.credit for line in self.lines)


class UnmappableTransactionError(ValueError):
    """Fineract's transaction cannot be turned into a balanced entry; a human has to look at it."""


def journal_entry_for(
    transaction: FineractLoanTransaction, external_reference: str, loan_reference: str, ledger: LedgerAccounts
) -> JournalEntryDraft:
    """Disbursement: DR loan receivable / CR bank. Repayment: DR bank / CR receivable, income and liability portions."""
    if transaction.reversed:
        raise UnmappableTransactionError(f"transaction {external_reference} is reversed in Fineract")
    match transaction.type:
        case LoanTransactionType.DISBURSEMENT:
            lines = _disbursement_lines(transaction, ledger)
        case LoanTransactionType.REPAYMENT:
            lines = _repayment_lines(transaction, ledger)
        case _:
            raise UnmappableTransactionError(f"no journal mapping for Fineract transaction type {transaction.type}")
    return JournalEntryDraft(
        external_reference=external_reference,
        entry_date=transaction.transaction_date,
        transaction_type=transaction.type.value,
        memo=f"{transaction.type.value.capitalize()} {external_reference} on loan {loan_reference}",
        lines=lines,
    )


def reversal_journal_reference(repayment_reference: str) -> str:
    """The ERP reference of a repayment's reversing entry: one per repayment, so the ERP refuses a second."""
    return f"{repayment_reference}:REVERSAL"


def reversing_entry(original: JournalEntryDraft, external_reference: str, memo: str) -> JournalEntryDraft:
    """The original entry with every debit and credit swapped, so the two together net to zero."""
    return JournalEntryDraft(
        external_reference=external_reference,
        entry_date=original.entry_date,
        transaction_type=f"{original.transaction_type}_REVERSAL",
        memo=memo,
        lines=tuple(JournalLine(line.account_code, debit=line.credit, credit=line.debit) for line in original.lines),
    )


def _disbursement_lines(transaction: FineractLoanTransaction, ledger: LedgerAccounts) -> tuple[JournalLine, ...]:
    _ensure_positive(transaction)
    return (
        JournalLine(ledger.loan_receivable, debit=transaction.amount),
        JournalLine(ledger.bank, credit=transaction.amount),
    )


def _repayment_lines(transaction: FineractLoanTransaction, ledger: LedgerAccounts) -> tuple[JournalLine, ...]:
    _ensure_positive(transaction)
    portions = (
        (ledger.loan_receivable, transaction.principal),
        (ledger.interest_income, transaction.interest),
        (ledger.fee_income, transaction.fees),
        (ledger.penalty_income, transaction.penalties),
        (ledger.overpayment_liability, transaction.overpayment),
    )
    allocated = sum(amount for _, amount in portions)
    if allocated != transaction.amount:
        raise UnmappableTransactionError(
            f"Fineract allocated {allocated} of a {transaction.amount} repayment; the entry would not balance"
        )
    credits = tuple(JournalLine(account, credit=amount) for account, amount in portions if amount > 0)
    return (JournalLine(ledger.bank, debit=transaction.amount), *credits)


def _ensure_positive(transaction: FineractLoanTransaction) -> None:
    if transaction.amount <= 0:
        raise UnmappableTransactionError(f"transaction amount must be positive, got {transaction.amount}")
