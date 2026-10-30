from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from mock_erp.chart import DEFAULT_CHART

ACCOUNT = {"code": "1500", "name": "Equipment", "type": "ASSET", "currency": "RWF", "external_reference": "GL-1500"}


def test_default_chart_is_seeded_on_startup(client: TestClient) -> None:
    codes = [account["code"] for account in client.get("/api/accounts").json()]

    assert codes == sorted(account.code for account in DEFAULT_CHART)
    assert {"1100", "1200", "4000"} <= set(codes)


def test_create_then_identical_replays_yield_one_account(client: TestClient) -> None:
    responses = [client.post("/api/accounts", json=ACCOUNT) for _ in range(4)]

    assert [response.status_code for response in responses] == [201, 200, 200, 200]
    assert all(response.json() == responses[0].json() for response in responses)
    assert sum(account["code"] == "1500" for account in client.get("/api/accounts").json()) == 1


def test_replay_is_recognised_after_normalisation(client: TestClient) -> None:
    client.post("/api/accounts", json=ACCOUNT)

    assert client.post("/api/accounts", json={**ACCOUNT, "currency": "rwf", "name": " Equipment "}).status_code == 200


def test_same_reference_with_a_different_payload_is_a_conflict(client: TestClient) -> None:
    client.post("/api/accounts", json=ACCOUNT)

    response = client.post("/api/accounts", json={**ACCOUNT, "name": "Machinery"})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_code_is_unique_across_references(client: TestClient) -> None:
    client.post("/api/accounts", json=ACCOUNT)

    response = client.post("/api/accounts", json={**ACCOUNT, "external_reference": "GL-OTHER"})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ACCOUNT_CODE_TAKEN"


def test_concurrent_identical_creates_insert_once(client: TestClient) -> None:
    with ThreadPoolExecutor(max_workers=10) as pool:
        statuses = sorted(pool.map(lambda _: client.post("/api/accounts", json=ACCOUNT).status_code, range(10)))

    assert statuses == [200] * 9 + [201]


def test_requests_need_the_bearer_token(client: TestClient) -> None:
    response = client.get("/api/accounts", headers={"Authorization": "Bearer wrong"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


def test_errors_carry_the_callers_correlation_id(client: TestClient) -> None:
    response = client.post("/api/accounts", json={**ACCOUNT, "type": "ASSETS"}, headers={"X-Correlation-ID": "corr-42"})

    assert response.status_code == 422
    assert response.headers["X-Correlation-ID"] == "corr-42"
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["correlation_id"] == "corr-42"
    assert error["details"][0]["field"] == "type"


def test_an_oversized_correlation_id_is_replaced(client: TestClient) -> None:
    oversized = "x" * 101
    response = client.post("/api/accounts", json={**ACCOUNT, "type": "ASSETS"}, headers={"X-Correlation-ID": oversized})

    replacement = response.headers["X-Correlation-ID"]
    assert replacement != oversized
    assert response.json()["error"]["correlation_id"] == replacement
