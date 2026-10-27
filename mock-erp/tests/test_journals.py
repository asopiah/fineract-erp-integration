from concurrent.futures import ThreadPoolExecutor
from typing import Any

from fastapi.testclient import TestClient


def disbursement(reference: str = "DISB-1", amount: str = "5000000", entry_date: str = "2026-10-27") -> dict[str, Any]:
    return {
        "external_reference": reference,
        "entry_date": entry_date,
        "transaction_type": "DISBURSEMENT",
        "memo": "Loan LN-2026-000001 disbursed",
        "lines": [
            {"account_code": "1200", "debit": amount},
            {"account_code": "1100", "credit": amount},
        ],
    }


def test_balanced_entry_is_posted_and_replayed_with_the_same_result(client: TestClient) -> None:
    first = client.post("/api/journal-entries", json=disbursement())
    replay = client.post("/api/journal-entries", json=disbursement(amount="5000000.00"))

    assert first.status_code == 201
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert first.json()["entry_number"] == "JE-000001"
    assert first.json()["total"] == "5000000.000000"


def test_same_reference_with_a_different_payload_is_a_conflict(client: TestClient) -> None:
    client.post("/api/journal-entries", json=disbursement())

    response = client.post("/api/journal-entries", json=disbursement(amount="4000000"))

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_unbalanced_entry_is_rejected(client: TestClient) -> None:
    entry = disbursement()
    entry["lines"][1]["credit"] = "4999999"

    response = client.post("/api/journal-entries", json=entry)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "UNBALANCED_ENTRY"
    assert client.get("/api/journal-entries/DISB-1").status_code == 404


def test_line_with_both_sides_is_rejected(client: TestClient) -> None:
    entry = disbursement()
    entry["lines"][0]["credit"] = "1"

    assert client.post("/api/journal-entries", json=entry).json()["error"]["code"] == "VALIDATION_ERROR"


def test_unknown_account_is_rejected(client: TestClient) -> None:
    entry = disbursement()
    entry["lines"][0]["account_code"] = "9999"

    response = client.post("/api/journal-entries", json=entry)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "UNKNOWN_ACCOUNT"


def test_balances_follow_each_accounts_normal_side(client: TestClient) -> None:
    client.post("/api/journal-entries", json=disbursement())

    receivable = client.get("/api/accounts/1200/balance").json()
    bank = client.get("/api/accounts/1100/balance").json()

    assert receivable["balance"] == "5000000.000000"
    assert bank["balance"] == "-5000000.000000"


def test_entries_can_be_listed_by_date_range(client: TestClient) -> None:
    client.post("/api/journal-entries", json=disbursement("DISB-OCT", entry_date="2026-10-27"))
    client.post("/api/journal-entries", json=disbursement("DISB-NOV", entry_date="2026-11-27"))

    listed = client.get("/api/journal-entries", params={"from_date": "2026-10-01", "to_date": "2026-10-31"}).json()

    assert [entry["external_reference"] for entry in listed] == ["DISB-OCT"]


def test_concurrent_identical_posts_record_one_entry(client: TestClient) -> None:
    with ThreadPoolExecutor(max_workers=10) as pool:
        responses = list(pool.map(lambda _: client.post("/api/journal-entries", json=disbursement()), range(10)))

    assert sorted(response.status_code for response in responses) == [200] * 9 + [201]
    assert {response.json()["entry_number"] for response in responses} == {"JE-000001"}
