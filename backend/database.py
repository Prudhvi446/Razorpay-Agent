"""
Database connection — SQLAlchemy engine, session factory, Base class.
"""

from urllib.parse import quote_plus
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from config import DATABASE_URL


def normalize_database_url(url: str | None) -> str:
    """Safely URL-encode password if it contains special characters like @ or #."""
    if not url:
        return "sqlite:///./recovery_agent.db"

    # Handle postgres:// vs postgresql://
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)

    # Check if there are multiple '@' characters in credentials
    # e.g., postgresql://user:pass@word@host:5432/db
    if "://" in url and "@" in url:
        prefix, rest = url.split("://", 1)
        # Split on the last '@' to separate user:pass from host:port/db
        creds_part, host_part = rest.rsplit("@", 1)
        if ":" in creds_part:
            user, password = creds_part.split(":", 1)
            # URL-encode password if not already percent-encoded
            encoded_password = quote_plus(password)
            return f"{prefix}://{user}:{encoded_password}@{host_part}"

    return url


engine = create_engine(
    normalize_database_url(DATABASE_URL), 
    pool_pre_ping=True, 
    pool_size=10, 
    max_overflow=20, 
    echo=False
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    """FastAPI dependency — yields a DB session and closes it after the request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
