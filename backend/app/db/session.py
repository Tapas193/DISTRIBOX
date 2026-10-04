"""Database engine, session factory and FastAPI dependency.

SQLAlchemy queries only - all user input is bound as parameters, so the
ORM's parameterisation is the SQL-injection defence. No string-built SQL
appears anywhere in this codebase.
"""

from __future__ import annotations

from collections.abc import Generator
from typing import Optional

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.core.logging_config import get_logger

logger = get_logger(__name__)

engine: Engine = create_engine(
    settings.database_url,
    echo=settings.db_echo,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_pre_ping=True,   # survives Postgres restarts / idle reconnects
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a request-scoped session."""
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def check_database() -> dict:
    """Probe the database for the health endpoint."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"status": "ok"}
    except Exception as exc:
        logger.error("database health check failed", extra={"error": str(exc)})
        return {"status": "unavailable", "error": type(exc).__name__}


def get_default_hotel_id(db: Session) -> Optional[int]:
    """Resolve the hotel the single-tenant demo operates on."""
    from app.models.entities import Hotel

    hotel = (
        db.query(Hotel)
        .filter(Hotel.code == settings.demo_hotel_code)
        .one_or_none()
    )
    if hotel is None:
        hotel = db.query(Hotel).order_by(Hotel.id).first()
    return hotel.id if hotel else None