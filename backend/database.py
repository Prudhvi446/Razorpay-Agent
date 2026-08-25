"""
Database connection — SQLAlchemy engine, session factory, Base class.
"""

from urllib.parse import quote_plus
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from config import DATABASE_URL


def normalize_database_url(url: str | None) -> str:
    if not url:
        return "sqlite:///./recovery_agent.db"
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url


from sqlalchemy.pool import NullPool

def init_engine():
    db_url = normalize_database_url(DATABASE_URL)
    if not db_url or db_url.startswith("sqlite"):
        return create_engine(
            "sqlite:///./recovery_agent.db",
            connect_args={"check_same_thread": False, "timeout": 30},
            pool_pre_ping=True,
            echo=False,
        )

    if "pooler.supabase.com" in db_url or ":6543" in db_url:
        return create_engine(
            db_url,
            poolclass=NullPool,
            connect_args={"sslmode": "require", "connect_timeout": 5},
            echo=False,
        )

    return create_engine(
        db_url,
        pool_pre_ping=True,
        pool_recycle=300,
        pool_size=5,
        max_overflow=10,
        connect_args={"connect_timeout": 5},
        echo=False,
    )

engine = init_engine()
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    """FastAPI dependency — yields a DB session and closes it after the request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
