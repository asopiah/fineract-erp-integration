"""Tests that touch the database run against real PostgreSQL: a throwaway database per session, migrated with the
same Alembic revisions as production. Start the server with ``docker compose up -d postgres`` (or point
``TEST_POSTGRES_URL`` elsewhere)."""

import os
import uuid
from collections.abc import Iterator

import psycopg
import pytest
from sqlalchemy import create_engine, text

from integration_service.database import migrate

ADMIN_URL = os.environ.get("TEST_POSTGRES_URL", "postgresql://postgres:postgres@localhost:55432/postgres")
TABLES = "operations, loan_transactions, loans, clients, gl_account_syncs"


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    name = f"integration_test_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(ADMIN_URL, autocommit=True) as connection:
        connection.execute(f'CREATE DATABASE "{name}"')
    url = f"{ADMIN_URL.rsplit('/', 1)[0].replace('postgresql://', 'postgresql+psycopg://', 1)}/{name}"
    migrate(url)
    yield url
    with psycopg.connect(ADMIN_URL, autocommit=True) as connection:
        connection.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


@pytest.fixture
def clean_database(database_url: str) -> Iterator[str]:
    yield database_url
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))
        connection.execute(text("ALTER SEQUENCE loan_reference_seq RESTART"))
    engine.dispose()
