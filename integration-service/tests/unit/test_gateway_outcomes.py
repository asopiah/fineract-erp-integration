"""How failed calls are classified decides what is safe to do next; see ``gateways.errors``."""

from collections.abc import Callable

import httpx
import pytest

from integration_service.gateways.errors import (
    GatewayError,
    NotAppliedError,
    OutcomeUnknownError,
    RejectedError,
)
from integration_service.gateways.faults import FaultConfig, FaultInjector, FineractFault
from integration_service.gateways.fineract import _error_detail, _is_transient_conflict
from integration_service.gateways.http import JsonApi

Handler = Callable[[httpx.Request], httpx.Response]


def api(handler: Handler) -> JsonApi:
    client = httpx.Client(base_url="http://fineract", transport=httpx.MockTransport(handler))
    return JsonApi("fineract", client, _error_detail, _is_transient_conflict)


def raising(error: type[httpx.HTTPError]) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        raise error("simulated", request=request)

    return handler


def answering(status: int, body: dict[str, object] | None = None) -> Handler:
    return lambda _: httpx.Response(status, json=body or {})


@pytest.mark.parametrize("error", [httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout])
def test_a_command_that_never_left_is_not_applied(error: type[httpx.HTTPError]) -> None:
    with pytest.raises(NotAppliedError):
        api(raising(error)).post("/loans", {})


@pytest.mark.parametrize("error", [httpx.ReadTimeout, httpx.RemoteProtocolError, httpx.ReadError])
def test_a_command_whose_answer_was_lost_has_an_unknown_outcome(error: type[httpx.HTTPError]) -> None:
    with pytest.raises(OutcomeUnknownError):
        api(raising(error)).post("/loans", {})


def test_a_failed_read_never_changed_anything() -> None:
    with pytest.raises(NotAppliedError):
        api(raising(httpx.ReadTimeout)).get("/loans/1")


def test_a_missing_resource_is_none_not_an_error() -> None:
    assert api(answering(404)).get("/loans/external-id/LN-1") is None


@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        (500, OutcomeUnknownError),
        (502, OutcomeUnknownError),
        (504, OutcomeUnknownError),
        (503, NotAppliedError),
        (429, NotAppliedError),
        (400, RejectedError),
        (403, RejectedError),
    ],
)
def test_http_statuses(status: int, outcome: type[GatewayError]) -> None:
    with pytest.raises(outcome):
        api(answering(status)).post("/loans", {})


def test_a_lost_concurrency_race_in_fineract_is_retryable() -> None:
    body = {"userMessageGlobalisationCode": "error.msg.platform.service.loan.conflict"}
    with pytest.raises(NotAppliedError):
        api(answering(409, body)).post("/loans/external-id/LN-1/transactions", {})


def test_a_business_conflict_in_fineract_is_a_rejection() -> None:
    body = {"errors": [{"userMessageGlobalisationCode": "error.msg.glaccount.external.id.conflict"}]}
    with pytest.raises(RejectedError) as raised:
        api(answering(409, body)).post("/glaccounts", {})
    assert raised.value.code == "error.msg.glaccount.external.id.conflict"


def test_rejections_carry_fineracts_reason() -> None:
    body = {
        "errors": [{"userMessageGlobalisationCode": "error.msg.loan.disbursal", "defaultUserMessage": "Not approved"}]
    }
    with pytest.raises(RejectedError) as raised:
        api(answering(403, body)).post("/loans", {})
    assert raised.value.message == "Not approved"
    assert raised.value.status_code == 403


class Recorder(httpx.BaseTransport):
    def __init__(self) -> None:
        self.requests: list[str] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request.method)
        return httpx.Response(200, json={})


def test_injected_timeout_after_send_reaches_the_server_and_loses_the_answer() -> None:
    faults, server = FaultInjector(), Recorder()
    faults.configure(FaultConfig(fineract=FineractFault.TIMEOUT_AFTER_SEND, remaining=1))
    client = httpx.Client(base_url="http://fineract", transport=faults.wrap(server))

    with pytest.raises(httpx.ReadTimeout):
        client.post("/loans")
    client.get("/loans")  # reads are not affected
    client.post("/loans")  # the fault was used up

    assert server.requests == ["POST", "GET", "POST"]
    assert faults.config.fineract is FineractFault.NONE


def test_injected_outage_sends_nothing() -> None:
    faults, server = FaultInjector(), Recorder()
    faults.configure(FaultConfig(fineract=FineractFault.UNAVAILABLE))
    client = httpx.Client(base_url="http://fineract", transport=faults.wrap(server))

    with pytest.raises(httpx.ConnectError):
        client.get("/loans")

    assert server.requests == []


def test_crash_request_is_used_once() -> None:
    faults = FaultInjector()
    faults.configure(FaultConfig(crash_after_fineract=True))

    assert faults.take_crash()
    assert not faults.take_crash()
