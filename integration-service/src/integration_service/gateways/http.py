"""JSON-over-HTTP calls with one classification of failures for every downstream system."""

from collections.abc import Callable
from typing import Any

import httpx

from integration_service.gateways.errors import GatewayError, NotAppliedError, OutcomeUnknownError, RejectedError
from integration_service.observability import CORRELATION_ID_HEADER, current_correlation_id

# Refused before any processing: safe to send again after a pause.
_NOT_APPLIED_STATUSES = frozenset({429, 503})
# The server may have committed before failing to answer.
_UNKNOWN_STATUSES = frozenset({500, 502, 504})
# The request never reached the server.
_NOT_DELIVERED = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)

ErrorDetail = Callable[[httpx.Response], tuple[str | None, str]]


def propagate_correlation_id(request: httpx.Request) -> None:
    correlation_id = current_correlation_id()
    if correlation_id:
        request.headers[CORRELATION_ID_HEADER] = correlation_id


class JsonApi:
    """A downstream system. ``error_detail`` extracts (code, message) from that system's error bodies;
    ``transient_conflict`` says whether a 409 means "rolled back, try again" rather than a business conflict."""

    def __init__(
        self,
        system: str,
        client: httpx.Client,
        error_detail: ErrorDetail,
        transient_conflict: Callable[[httpx.Response], bool] = lambda _: False,
    ) -> None:
        self._system = system
        self._client = client
        self._error_detail = error_detail
        self._transient_conflict = transient_conflict

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any | None:
        """The resource, or ``None`` if it does not exist. A failed read never changed anything: ``NotAppliedError``."""
        response = self._send("GET", path, params=params)
        if response.status_code == 404:
            return None
        self._raise_for_status(response, read_only=True)
        return response.json()

    def post(self, path: str, body: dict[str, Any], params: dict[str, Any] | None = None) -> Any:
        response = self._send("POST", path, body=body, params=params)
        self._raise_for_status(response, read_only=False)
        return response.json()

    def put(self, path: str, body: dict[str, Any]) -> Any:
        response = self._send("PUT", path, body=body)
        self._raise_for_status(response, read_only=False)
        return response.json()

    def _send(
        self, method: str, path: str, *, body: dict[str, Any] | None = None, params: dict[str, Any] | None = None
    ) -> httpx.Response:
        try:
            return self._client.request(method, path, json=body, params=params)
        except _NOT_DELIVERED as error:
            raise NotAppliedError(self._system, f"{method} {path} not delivered: {error!r}") from error
        except httpx.HTTPError as error:
            if method == "GET":
                raise NotAppliedError(self._system, f"GET {path} failed: {error!r}") from error
            raise OutcomeUnknownError(self._system, f"{method} {path} outcome unknown: {error!r}") from error

    def _raise_for_status(self, response: httpx.Response, *, read_only: bool) -> None:
        status = response.status_code
        if status < 400:
            return
        code, message = self._error_detail(response)
        error_type: type[GatewayError]
        if read_only or status in _NOT_APPLIED_STATUSES or (status == 409 and self._transient_conflict(response)):
            error_type = NotAppliedError
        elif status in _UNKNOWN_STATUSES:
            error_type = OutcomeUnknownError
        else:
            error_type = RejectedError
        raise error_type(self._system, message, status_code=status, code=code)


def json_or_empty(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}
