"""FastAPI dependencies: the service container and bearer-token authentication."""

import secrets
from typing import Annotated

from fastapi import Depends, Header, Request

from integration_service.container import Services
from integration_service.errors import ApiError, ErrorCode


def get_services(request: Request) -> Services:
    services: Services = request.app.state.services
    return services


def require_api_token(
    services: Annotated[Services, Depends(get_services)], authorization: Annotated[str | None, Header()] = None
) -> None:
    expected = f"Bearer {services.settings.api_token.get_secret_value()}"
    if authorization is None or not secrets.compare_digest(authorization, expected):
        raise ApiError(401, ErrorCode.UNAUTHORIZED, "a valid bearer token is required")


ServicesDep = Annotated[Services, Depends(get_services)]
