"""FastAPI dependencies: database session, authentication, fault injection."""

import secrets
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy.orm import Session

from mock_erp.errors import ApiError, ErrorCode
from mock_erp.faults import FaultInjector


def get_session(request: Request) -> Iterator[Session]:
    with request.app.state.session_factory() as session:
        yield session


def get_faults(request: Request) -> FaultInjector:
    faults: FaultInjector = request.app.state.faults
    return faults


def require_api_token(request: Request, authorization: Annotated[str | None, Header()] = None) -> None:
    expected = f"Bearer {request.app.state.settings.api_token}"
    if authorization is None or not secrets.compare_digest(authorization, expected):
        raise ApiError(401, ErrorCode.UNAUTHORIZED, "a valid bearer token is required")


def apply_request_faults(faults: Annotated[FaultInjector, Depends(get_faults)]) -> None:
    faults.before_request()


SessionDep = Annotated[Session, Depends(get_session)]
FaultsDep = Annotated[FaultInjector, Depends(get_faults)]
