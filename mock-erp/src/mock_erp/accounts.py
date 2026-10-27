"""Chart of accounts: idempotent create, listing and balances."""

import logging
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from mock_erp.errors import ApiError, ErrorCode
from mock_erp.fingerprint import fingerprint
from mock_erp.idempotency import ensure_same_request
from mock_erp.models import Account, JournalLine
from mock_erp.schemas import AccountIn, BalanceOut

logger = logging.getLogger(__name__)


def create_account(session: Session, request: AccountIn) -> tuple[Account, bool]:
    """Returns the account and whether this call created it (``False`` for a replay)."""
    request_fingerprint = fingerprint(request)
    existing = _find_by_reference(session, request.external_reference)
    if existing is not None:
        ensure_same_request(existing.request_fingerprint, request_fingerprint, request.external_reference)
        return existing, False
    if session.scalar(select(Account.id).where(Account.code == request.code)) is not None:
        raise _code_taken(request.code)

    account = Account(
        code=request.code,
        name=request.name,
        type=request.type,
        currency=request.currency,
        external_reference=request.external_reference,
        request_fingerprint=request_fingerprint,
    )
    session.add(account)
    try:
        session.commit()
    except IntegrityError:
        # A concurrent request inserted the same reference or code first; the unique constraints decided the winner.
        session.rollback()
        winner = _find_by_reference(session, request.external_reference)
        if winner is None:
            raise _code_taken(request.code) from None
        ensure_same_request(winner.request_fingerprint, request_fingerprint, request.external_reference)
        return winner, False

    logger.info("account created", extra={"external_reference": account.external_reference, "status": "CREATED"})
    return account, True


def list_accounts(session: Session) -> list[Account]:
    return list(session.scalars(select(Account).order_by(Account.code)))


def get_account(session: Session, code: str) -> Account:
    account = session.scalar(select(Account).where(Account.code == code))
    if account is None:
        raise ApiError(404, ErrorCode.NOT_FOUND, f"account {code} does not exist")
    return account


def account_balance(session: Session, code: str) -> BalanceOut:
    account = get_account(session, code)
    debit_total, credit_total = session.execute(
        select(
            func.coalesce(func.sum(JournalLine.debit), Decimal(0)),
            func.coalesce(func.sum(JournalLine.credit), Decimal(0)),
        ).where(JournalLine.account_id == account.id)
    ).one()
    balance = debit_total - credit_total if account.type.is_debit_normal else credit_total - debit_total
    return BalanceOut(
        code=account.code,
        currency=account.currency,
        debit_total=debit_total,
        credit_total=credit_total,
        balance=balance,
    )


def _find_by_reference(session: Session, external_reference: str) -> Account | None:
    return session.scalar(select(Account).where(Account.external_reference == external_reference))


def _code_taken(code: str) -> ApiError:
    return ApiError(409, ErrorCode.ACCOUNT_CODE_TAKEN, f"account code {code} is already used by another account")
