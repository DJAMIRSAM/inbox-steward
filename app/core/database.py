from __future__ import annotations

from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlmodel import Session, SQLModel

from app.core.config import settings

engine = create_engine(settings.database_url, echo=False, pool_pre_ping=True)


def init_db() -> None:
    SQLModel.metadata.create_all(engine)


@contextmanager
def get_session() -> Session:
    with Session(engine) as session:
        yield session
