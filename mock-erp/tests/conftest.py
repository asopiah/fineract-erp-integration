"""Tests run against real PostgreSQL: a throwaway database per session on the compose server.

Start it with ``docker compose up -d postgres`` (or point ``TEST_POSTGRES_URL`` at another server).
"""

import os
import uuid
from collections.abc import Iterator

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from mock_erp.main import create_app
from mock_erp.settings import Settings

ADMIN_URL = os.environ.get("TEST_POSTGRES_URL", "postgresql://postgres:postgres@localhost:55432/postgres")
TOKEN = "test-token"


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    name = f"mock_erp_test_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(ADMIN_URL, autocommit=True) as connection:
        connection.execute(f'CREATE DATABASE "{name}"')
    server = ADMIN_URL.rsplit("/", 1)[0].replace("postgresql://", "postgresql+psycopg://", 1)
    yield f"{server}/{name}"
    with psycopg.connect(ADMIN_URL, autocommit=True) as connection:
        connection.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


@pytest.fixture
def app(database_url: str) -> Iterator[FastAPI]:
    application = create_app(Settings(database_url=database_url, api_token=TOKEN, fault_injection_enabled=True))
    yield application
    session_factory = application.state.session_factory
    with session_factory() as session:
        session.execute(text("TRUNCATE journal_lines, journal_entries, accounts RESTART IDENTITY CASCADE"))
        session.commit()
    session_factory.kw["bind"].dispose()


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"})
