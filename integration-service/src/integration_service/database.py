"""SQLAlchemy engine and session factory, and schema migrations (Alembic)."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

# Deterministic constraint names: migrations and error handling can refer to them.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

_MIGRATIONS = Path(__file__).parent / "migrations"


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def create_session_factory(database_url: str) -> sessionmaker[Session]:
    engine = create_engine(database_url, pool_pre_ping=True, pool_size=20, max_overflow=20)
    return sessionmaker(engine)


def migrate(database_url: str) -> None:
    """Brings the schema to the latest revision. Run once per deployment, before the service starts."""
    config = Config()
    config.set_main_option("script_location", str(_MIGRATIONS))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")
