"""Chart of accounts and double-entry journal, as persisted by the ERP."""

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import CheckConstraint, Date, DateTime, Enum, ForeignKey, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from mock_erp.database import Base

MONEY = Numeric(19, 6)


class AccountType(StrEnum):
    ASSET = "ASSET"
    LIABILITY = "LIABILITY"
    EQUITY = "EQUITY"
    INCOME = "INCOME"
    EXPENSE = "EXPENSE"

    @property
    def is_debit_normal(self) -> bool:
        return self in (AccountType.ASSET, AccountType.EXPENSE)


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    type: Mapped[AccountType] = mapped_column(Enum(AccountType, native_enum=False, length=16))
    currency: Mapped[str] = mapped_column(String(3))
    external_reference: Mapped[str] = mapped_column(String(100), unique=True)
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class JournalEntry(Base):
    __tablename__ = "journal_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    external_reference: Mapped[str] = mapped_column(String(100), unique=True)
    entry_date: Mapped[date] = mapped_column(Date, index=True)
    transaction_type: Mapped[str | None] = mapped_column(String(32))
    memo: Mapped[str | None] = mapped_column(String(255))
    currency: Mapped[str] = mapped_column(String(3))
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    lines: Mapped[list["JournalLine"]] = relationship(
        back_populates="entry", cascade="all, delete-orphan", order_by="JournalLine.id", lazy="selectin"
    )

    @property
    def entry_number(self) -> str:
        """The ERP's own identifier for the entry (the ``erp_transaction_id`` of the integration logs)."""
        return f"JE-{self.id:06d}"

    @property
    def total(self) -> Decimal:
        return sum((line.debit for line in self.lines), Decimal(0))


class JournalLine(Base):
    __tablename__ = "journal_lines"
    __table_args__ = (
        CheckConstraint("debit >= 0 AND credit >= 0", name="ck_journal_lines_non_negative"),
        CheckConstraint("(debit > 0) <> (credit > 0)", name="ck_journal_lines_one_side"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    entry_id: Mapped[int] = mapped_column(ForeignKey("journal_entries.id"), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    debit: Mapped[Decimal] = mapped_column(MONEY)
    credit: Mapped[Decimal] = mapped_column(MONEY)

    entry: Mapped[JournalEntry] = relationship(back_populates="lines")
    account: Mapped[Account] = relationship(lazy="joined")
