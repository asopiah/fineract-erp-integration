"""The ERP: chart of accounts and journal entries, both idempotent on ``external_reference``."""

from datetime import date
from decimal import Decimal
from typing import Any

import httpx

from integration_service.domain.accounting import JournalEntryDraft
from integration_service.domain.erp import ErpAccount, ErpJournalEntry
from integration_service.gateways.http import JsonApi, json_or_empty, propagate_correlation_id
from integration_service.settings import Settings


class ErpGateway:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None) -> None:
        client = httpx.Client(
            base_url=settings.erp_url,
            headers={"Authorization": f"Bearer {settings.erp_api_token.get_secret_value()}"},
            timeout=httpx.Timeout(settings.erp_timeout_seconds, connect=3.0),
            transport=transport,
            event_hooks={"request": [propagate_correlation_id]},
        )
        self._api = JsonApi("erp", client, _error_detail)

    def list_accounts(self) -> list[ErpAccount]:
        return [
            ErpAccount(
                code=item["code"],
                name=item["name"],
                type=item["type"],
                currency=item["currency"],
                external_reference=item["external_reference"],
            )
            for item in self._api.get("/api/accounts") or []
        ]

    def post_journal_entry(self, draft: JournalEntryDraft) -> ErpJournalEntry:
        """Safe to repeat: the ERP answers an identical replay with the entry it already holds."""
        body = {
            "external_reference": draft.external_reference,
            "entry_date": draft.entry_date.isoformat(),
            "transaction_type": draft.transaction_type,
            "memo": draft.memo,
            "lines": [
                {"account_code": line.account_code, "debit": str(line.debit), "credit": str(line.credit)}
                for line in draft.lines
            ],
        }
        return _journal_entry(self._api.post("/api/journal-entries", body))

    def find_journal_entry(self, external_reference: str) -> ErpJournalEntry | None:
        entry = self._api.get(f"/api/journal-entries/{external_reference}")
        return None if entry is None else _journal_entry(entry)

    def list_journal_entries(self, from_date: date, to_date: date) -> list[ErpJournalEntry]:
        params = {"from_date": from_date.isoformat(), "to_date": to_date.isoformat()}
        return [_journal_entry(entry) for entry in self._api.get("/api/journal-entries", params=params) or []]


def _journal_entry(entry: dict[str, Any]) -> ErpJournalEntry:
    return ErpJournalEntry(
        entry_number=entry["entry_number"],
        external_reference=entry["external_reference"],
        entry_date=date.fromisoformat(entry["entry_date"]),
        transaction_type=entry.get("transaction_type"),
        total=Decimal(entry["total"]),
    )


def _error_detail(response: httpx.Response) -> tuple[str | None, str]:
    error = json_or_empty(response).get("error") or {}
    return error.get("code"), str(error.get("message") or response.text[:300])
