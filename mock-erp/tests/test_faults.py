import threading
import time

from fastapi.testclient import TestClient

from tests.test_journals import disbursement


def test_unavailable_answers_503_for_the_requested_number_of_calls(client: TestClient) -> None:
    client.put("/_faults", json={"mode": "unavailable", "remaining": 1})

    down = client.post("/api/journal-entries", json=disbursement())
    recovered = client.post("/api/journal-entries", json=disbursement())

    assert down.status_code == 503
    assert down.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
    assert recovered.status_code == 201
    assert client.get("/_faults").json()["mode"] == "none"


def test_rate_limited_answers_429_with_retry_after(client: TestClient) -> None:
    client.put("/_faults", json={"mode": "rate_limited"})

    response = client.get("/api/accounts")

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "1"


def test_timeout_after_commit_writes_before_the_response_is_delayed(client: TestClient) -> None:
    client.put("/_faults", json={"mode": "timeout_after_commit", "delay_seconds": 2, "remaining": 1})
    slow_post = threading.Thread(target=client.post, args=("/api/journal-entries",), kwargs={"json": disbursement()})

    slow_post.start()
    written_while_response_pending = _eventually_found(client, "DISB-1", within_seconds=1.5)
    slow_post.join()

    assert written_while_response_pending
    assert client.post("/api/journal-entries", json=disbursement()).status_code == 200


def _eventually_found(client: TestClient, reference: str, within_seconds: float) -> bool:
    deadline = time.monotonic() + within_seconds
    while time.monotonic() < deadline:
        if client.get(f"/api/journal-entries/{reference}").status_code == 200:
            return True
        time.sleep(0.05)
    return False
