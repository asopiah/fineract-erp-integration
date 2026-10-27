from dataclasses import replace
from datetime import date
from decimal import Decimal

from integration_service.domain.erp import ErpJournalEntry
from integration_service.domain.fineract import FineractLoanTransaction, LoanTransactionType
from integration_service.services.reconciliation import FineractSide, Outcome, compare

ZERO = Decimal(0)
REPAYMENT = FineractSide(
    "LN-2026-000001",
    FineractLoanTransaction(
        internal_id=9,
        external_reference="REP-1",
        type=LoanTransactionType.REPAYMENT,
        fineract_type="Repayment",
        transaction_date=date(2026, 2, 15),
        amount=Decimal(458400),
        principal=Decimal(383400),
        interest=Decimal(75000),
        fees=ZERO,
        penalties=ZERO,
        overpayment=ZERO,
        outstanding_after=Decimal(4616600),
        reversed=False,
    ),
)
ENTRY = ErpJournalEntry("JE-000002", "REP-1", date(2026, 2, 15), "REPAYMENT", Decimal(458400))
REVERSED = FineractSide(REPAYMENT.loan_reference, replace(REPAYMENT.transaction, reversed=True))


def test_same_amount_and_type_match() -> None:
    assert compare(REPAYMENT, ENTRY) == (Outcome.MATCHED, None)


def test_one_sided_transactions_are_missing_on_the_other_side() -> None:
    assert compare(REPAYMENT, None)[0] is Outcome.MISSING_IN_ERP
    assert compare(None, ENTRY)[0] is Outcome.MISSING_IN_FINERACT


def test_different_amounts_are_a_mismatch() -> None:
    assert compare(REPAYMENT, replace(ENTRY, total=Decimal(458000)))[0] is Outcome.AMOUNT_MISMATCH


def test_different_types_are_reported_as_such() -> None:
    assert compare(REPAYMENT, replace(ENTRY, transaction_type="DISBURSEMENT"))[0] is Outcome.TYPE_MISMATCH


def test_a_reversal_nets_to_zero() -> None:
    assert compare(REVERSED, None)[0] is Outcome.MATCHED  # reversed and never posted: both sides are zero
    assert compare(REVERSED, ENTRY)[0] is Outcome.AMOUNT_MISMATCH  # the ERP entry still needs reversing
