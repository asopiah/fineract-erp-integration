from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine

from integration_service import models  # noqa: F401  (registers the tables)
from integration_service.database import Base


def test_migrations_produce_exactly_the_modelled_schema(database_url: str) -> None:
    engine = create_engine(database_url)
    with engine.connect() as connection:
        differences = compare_metadata(MigrationContext.configure(connection), Base.metadata)
    engine.dispose()

    assert differences == []
