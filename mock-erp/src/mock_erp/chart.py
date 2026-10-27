"""The chart of accounts the ERP starts with.

Covers every account a cash-based Fineract loan product maps to, so the product can be configured entirely from
accounts the ERP owns.
"""

from sqlalchemy.orm import Session

from mock_erp.accounts import create_account
from mock_erp.models import AccountType
from mock_erp.schemas import AccountIn

CURRENCY = "RWF"

DEFAULT_CHART: tuple[AccountIn, ...] = tuple(
    AccountIn(code=code, name=name, type=account_type, currency=CURRENCY, external_reference=f"GL-{code}")
    for code, name, account_type in (
        ("1100", "Bank", AccountType.ASSET),
        ("1200", "Loan Receivable", AccountType.ASSET),
        ("1300", "Transfers in Suspense", AccountType.ASSET),
        ("2100", "Overpayment Liability", AccountType.LIABILITY),
        ("4000", "Interest Income", AccountType.INCOME),
        ("4100", "Fee Income", AccountType.INCOME),
        ("4200", "Penalty Income", AccountType.INCOME),
        ("4300", "Recovery Income", AccountType.INCOME),
        ("5100", "Loan Write-off Expense", AccountType.EXPENSE),
    )
)


def seed_chart(session: Session) -> None:
    """Idempotent: creating an account that already exists with the same definition is a replay."""
    for account in DEFAULT_CHART:
        create_account(session, account)
