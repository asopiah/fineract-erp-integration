"""Alembic environment: migrates the database given in ``sqlalchemy.url`` to the models' schema."""

from alembic import context
from sqlalchemy import create_engine

from integration_service import models  # noqa: F401  (registers the tables on Base.metadata)
from integration_service.database import Base


def run_migrations() -> None:
    engine = create_engine(context.config.get_main_option("sqlalchemy.url") or "")
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


run_migrations()
