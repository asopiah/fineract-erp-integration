"""Request and response bodies. Money is a decimal string on the wire, never a float."""

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from mock_erp.models import AccountType

Reference = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
AccountCode = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^[0-9A-Za-z.\-]{1,32}$")]
CurrencyCode = Annotated[str, StringConstraints(strip_whitespace=True, to_upper=True, pattern=r"^[A-Za-z]{3}$")]
LineAmount = Annotated[Decimal, Field(ge=0, max_digits=19, decimal_places=6)]


class AccountIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: AccountCode
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    type: AccountType
    currency: CurrencyCode
    external_reference: Reference


class AccountOut(BaseModel):
    code: str
    name: str
    type: AccountType
    currency: str
    external_reference: str


class BalanceOut(BaseModel):
    code: str
    currency: str
    debit_total: Decimal
    credit_total: Decimal
    balance: Decimal = Field(description="Signed on the account's normal side: debits for assets and expenses.")


class JournalLineIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_code: AccountCode
    debit: LineAmount = Decimal(0)
    credit: LineAmount = Decimal(0)

    @model_validator(mode="after")
    def one_side_only(self) -> "JournalLineIn":
        if (self.debit > 0) == (self.credit > 0):
            raise ValueError("a line carries either a debit or a credit, not both and not neither")
        return self


class JournalEntryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    external_reference: Reference
    entry_date: date
    transaction_type: Annotated[str, StringConstraints(strip_whitespace=True, max_length=32)] | None = None
    memo: Annotated[str, StringConstraints(max_length=255)] | None = None
    lines: list[JournalLineIn] = Field(min_length=2)


class JournalLineOut(BaseModel):
    account_code: str
    debit: Decimal
    credit: Decimal


class JournalEntryOut(BaseModel):
    entry_number: str
    external_reference: str
    entry_date: date
    transaction_type: str | None
    memo: str | None
    currency: str
    total: Decimal
    lines: list[JournalLineOut]
    created_at: datetime
