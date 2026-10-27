"""HTTP endpoints. Creates answer 201 the first time and 200 for an identical replay."""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response

from mock_erp.accounts import account_balance, create_account, list_accounts
from mock_erp.dependencies import FaultsDep, SessionDep, apply_request_faults, require_api_token
from mock_erp.faults import FaultConfig
from mock_erp.journals import get_journal_entry, list_journal_entries, post_journal_entry
from mock_erp.models import Account, JournalEntry
from mock_erp.schemas import (
    AccountIn,
    AccountOut,
    BalanceOut,
    JournalEntryIn,
    JournalEntryOut,
    JournalLineOut,
)

api = APIRouter(prefix="/api", dependencies=[Depends(require_api_token), Depends(apply_request_faults)])
admin = APIRouter(prefix="/_faults", tags=["fault injection"], dependencies=[Depends(require_api_token)])


@api.post("/accounts", tags=["accounts"], response_model=AccountOut, status_code=201)
def post_account(body: AccountIn, response: Response, session: SessionDep) -> AccountOut:
    account, created = create_account(session, body)
    response.status_code = 201 if created else 200
    return _account_out(account)


@api.get("/accounts", tags=["accounts"], response_model=list[AccountOut])
def get_accounts(session: SessionDep) -> list[AccountOut]:
    return [_account_out(account) for account in list_accounts(session)]


@api.get("/accounts/{code}/balance", tags=["accounts"], response_model=BalanceOut)
def get_account_balance(code: str, session: SessionDep) -> BalanceOut:
    return account_balance(session, code)


@api.post("/journal-entries", tags=["journal entries"], response_model=JournalEntryOut, status_code=201)
def post_journal(body: JournalEntryIn, response: Response, session: SessionDep, faults: FaultsDep) -> JournalEntryOut:
    entry, created = post_journal_entry(session, body)
    faults.after_commit()
    response.status_code = 201 if created else 200
    return _entry_out(entry)


@api.get("/journal-entries", tags=["journal entries"], response_model=list[JournalEntryOut])
def get_journals(
    session: SessionDep, from_date: Annotated[date, Query()], to_date: Annotated[date, Query()]
) -> list[JournalEntryOut]:
    return [_entry_out(entry) for entry in list_journal_entries(session, from_date, to_date)]


@api.get("/journal-entries/{external_reference}", tags=["journal entries"], response_model=JournalEntryOut)
def get_journal(external_reference: str, session: SessionDep) -> JournalEntryOut:
    return _entry_out(get_journal_entry(session, external_reference))


@admin.get("", response_model=FaultConfig)
def get_fault(faults: FaultsDep) -> FaultConfig:
    return faults.config


@admin.put("", response_model=FaultConfig)
def put_fault(body: FaultConfig, faults: FaultsDep) -> FaultConfig:
    faults.configure(body)
    return faults.config


def _account_out(account: Account) -> AccountOut:
    return AccountOut(
        code=account.code,
        name=account.name,
        type=account.type,
        currency=account.currency,
        external_reference=account.external_reference,
    )


def _entry_out(entry: JournalEntry) -> JournalEntryOut:
    return JournalEntryOut(
        entry_number=entry.entry_number,
        external_reference=entry.external_reference,
        entry_date=entry.entry_date,
        transaction_type=entry.transaction_type,
        memo=entry.memo,
        currency=entry.currency,
        total=entry.total,
        lines=[
            JournalLineOut(account_code=line.account.code, debit=line.debit, credit=line.credit) for line in entry.lines
        ],
        created_at=entry.created_at,
    )
