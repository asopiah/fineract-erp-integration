"""What leaves the service: amounts as plain digits, and correlation ids that fit where they are stored."""

import json
from datetime import date
from decimal import Decimal

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from integration_service.api.schemas import ReconciliationRequest
from integration_service.domain.accounting import JournalEntryDraft, JournalLine
from integration_service.gateways.erp import ErpGateway
from integration_service.gateways.fineract import FineractGateway
from integration_service.observability import CORRELATION_ID_HEADER, install_correlation_middleware
from integration_service.settings import Settings

FIVE_MILLION = Decimal("5E+6")


class Recorder(httpx.BaseTransport):
    def __init__(self, answers: dict[str, object]) -> None:
        self.bodies: list[dict[str, object]] = []
        self._answers = answers

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            self.bodies.append(json.loads(request.content))
        answer = next((body for path, body in self._answers.items() if request.url.path.endswith(path)), {})
        return httpx.Response(200, json=answer)


def test_fineract_receives_amounts_as_plain_digits() -> None:
    settings = Settings()
    server = Recorder({"/paymenttypes": [{"id": 1, "name": settings.fineract_payment_type_name}]})
    fineract = FineractGateway(settings, transport=server)

    fineract.disburse_loan("LN-2026-000001", "DISB-1", FIVE_MILLION, date(2026, 1, 15))
    fineract.make_repayment("LN-2026-000001", "REP-1", FIVE_MILLION, date(2026, 2, 15))

    assert [body["transactionAmount"] for body in server.bodies] == ["5000000", "5000000"]


def test_the_erp_receives_amounts_as_plain_digits() -> None:
    entry = {"entry_number": "JE-1", "external_reference": "DISB-1", "entry_date": "2026-01-15", "total": "5000000"}
    server = Recorder({"/api/journal-entries": entry})
    draft = JournalEntryDraft(
        "DISB-1",
        date(2026, 1, 15),
        "DISBURSEMENT",
        "memo",
        (JournalLine("1200", debit=FIVE_MILLION), JournalLine("1100", credit=FIVE_MILLION)),
    )

    ErpGateway(Settings(), transport=server).post_journal_entry(draft)

    lines = server.bodies[0]["lines"]
    assert lines == [
        {"account_code": "1200", "debit": "5000000", "credit": "0"},
        {"account_code": "1100", "debit": "0", "credit": "5000000"},
    ]


def echoing_app() -> TestClient:
    app = FastAPI()
    install_correlation_middleware(app)
    app.get("/")(lambda: {})
    return TestClient(app)


def test_a_valid_correlation_id_is_kept() -> None:
    response = echoing_app().get("/", headers={CORRELATION_ID_HEADER: "partner-run:42.a_b"})
    assert response.headers[CORRELATION_ID_HEADER] == "partner-run:42.a_b"


@pytest.mark.parametrize("header", ["x" * 101, "has space", "semi;colon", "a/b"])
def test_an_unusable_correlation_id_is_replaced(header: str) -> None:
    response = echoing_app().get("/", headers={CORRELATION_ID_HEADER: header})
    replacement = response.headers[CORRELATION_ID_HEADER]
    assert replacement != header
    assert len(replacement) == 36


@pytest.mark.parametrize(
    "body",
    [{"external_references": []}, {"from_date": "2026-02-01", "to_date": "2026-01-01"}],
    ids=["no-references", "dates-reversed"],
)
def test_reconciliation_criteria_are_validated(body: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ReconciliationRequest.model_validate(body)
