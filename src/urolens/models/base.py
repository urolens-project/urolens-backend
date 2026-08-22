"""Shared SQLAlchemy declarative base for every ORM model in `models/`."""
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Declarative base class every ORM model inherits from."""

    pass
