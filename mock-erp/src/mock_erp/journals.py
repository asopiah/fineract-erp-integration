"""Journal entries: balanced, single-currency, and unique per ``external_reference``."""

import logging
from collections.abc import Iterable
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from mock_erp.errors import ApiError, ErrorCode
from mock_erp.fingerprint import fingerprint
from mock_erp.idempotency import ensure_same_request
from mock_erp.models import Account, JournalEntry, JournalLine
from mock_erp.schemas import JournalEntryIn, JournalLineIn

logger = logging.getLogger(__name__)


def post_journal_entry(session: Session, request: JournalEntryIn) -> tuple[JournalEntry, bool]:
    """Returns the entry and whether this call created it (``False`` for a replay)."""
    request_fingerprint = fingerprint(request)
    existing = _find_by_reference(session, request.external_reference)
    if existing is not None:
        ensure_same_request(existing.request_fingerprint, request_fingerprint, request.external_reference)
        return existing, False

    _ensure_balanced(request.lines)
    accounts = _accounts_by_code(session, {line.account_code for line in request.lines})
    entry = JournalEntry(
        external_reference=request.external_reference,
        entry_date=request.entry_date,
        transaction_type=request.transaction_type,
        memo=request.memo,
        currency=_single_currency(accounts.values()),
        request_fingerprint=request_fingerprint,
        lines=[
            JournalLine(account=accounts[line.account_code], debit=line.debit, credit=line.credit)
            for line in request.lines
        ],
    )
    session.add(entry)
    try:
        session.commit()
    except IntegrityError:
        # A concurrent request posted the same reference first; the unique constraint decided the winner.
        session.rollback()
        winner = _find_by_reference(session, request.external_reference)
        if winner is None:
            raise
        ensure_same_request(winner.request_fingerprint, request_fingerprint, request.external_reference)
        return winner, False

    logger.info(
        "journal entry posted",
        extra={
            "external_reference": entry.external_reference,
            "erp_transaction_id": entry.entry_number,
            "status": "POSTED",
        },
    )
    return entry, True


def get_journal_entry(session: Session, external_reference: str) -> JournalEntry:
    entry = _find_by_reference(session, external_reference)
    if entry is None:
        raise ApiError(404, ErrorCode.NOT_FOUND, f"no journal entry with external_reference {external_reference}")
    return entry


def list_journal_entries(session: Session, from_date: date, to_date: date) -> list[JournalEntry]:
    query = (
        select(JournalEntry)
        .where(JournalEntry.entry_date.between(from_date, to_date))
        .order_by(JournalEntry.entry_date, JournalEntry.id)
    )
    return list(session.scalars(query))


def _find_by_reference(session: Session, external_reference: str) -> JournalEntry | None:
    return session.scalar(select(JournalEntry).where(JournalEntry.external_reference == external_reference))


def _ensure_balanced(lines: list[JournalLineIn]) -> None:
    debits = sum((line.debit for line in lines), Decimal(0))
    credits = sum((line.credit for line in lines), Decimal(0))
    if debits != credits:
        raise ApiError(422, ErrorCode.UNBALANCED_ENTRY, f"debits ({debits}) must equal credits ({credits})")


def _accounts_by_code(session: Session, codes: set[str]) -> dict[str, Account]:
    accounts = {account.code: account for account in session.scalars(select(Account).where(Account.code.in_(codes)))}
    unknown = sorted(codes - accounts.keys())
    if unknown:
        raise ApiError(422, ErrorCode.UNKNOWN_ACCOUNT, f"unknown account codes: {', '.join(unknown)}")
    return accounts


def _single_currency(accounts: Iterable[Account]) -> str:
    currencies = {account.currency for account in accounts}
    if len(currencies) != 1:
        listed = ", ".join(sorted(currencies))
        raise ApiError(422, ErrorCode.CURRENCY_MISMATCH, f"lines span several currencies: {listed}")
    return currencies.pop()
