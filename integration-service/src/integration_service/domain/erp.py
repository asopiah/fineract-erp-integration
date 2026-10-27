"""What the ERP holds, as typed values."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass(frozen=True)
class ErpAccount:
    code: str
    name: str
    type: str
    currency: str
    external_reference: str


@dataclass(frozen=True)
class ErpJournalEntry:
    entry_number: str
    external_reference: str
    entry_date: date
    transaction_type: str | None
    total: Decimal
