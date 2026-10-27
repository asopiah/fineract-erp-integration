from datetime import date
from decimal import Decimal

from mock_erp.fingerprint import fingerprint
from mock_erp.schemas import JournalEntryIn, JournalLineIn


def entry(amount: str, memo: str = "disbursement") -> JournalEntryIn:
    return JournalEntryIn(
        external_reference="DISB-1",
        entry_date=date(2026, 10, 27),
        memo=memo,
        lines=[
            JournalLineIn(account_code="1200", debit=Decimal(amount)),
            JournalLineIn(account_code="1100", credit=Decimal(amount)),
        ],
    )


def test_equal_amounts_written_differently_fingerprint_the_same() -> None:
    assert fingerprint(entry("5000000")) == fingerprint(entry("5000000.00")) == fingerprint(entry("5E+6"))


def test_any_difference_changes_the_fingerprint() -> None:
    assert fingerprint(entry("5000000")) != fingerprint(entry("5000001"))
    assert fingerprint(entry("5000000")) != fingerprint(entry("5000000", memo="other"))
