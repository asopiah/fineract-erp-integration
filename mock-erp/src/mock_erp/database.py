"""SQLAlchemy engine, session factory and declarative base."""

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


def create_session_factory(database_url: str) -> sessionmaker[Session]:
    """Connects and creates the schema. A mock ERP has no upgrade path to manage, so no migration tool is used.

    Objects expire on commit, so a response is always built from what was persisted: a first response and its
    replay serialize identically.
    """
    engine = create_engine(database_url, pool_pre_ping=True)
    Base.metadata.create_all(engine)
    return sessionmaker(engine)
