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


def migrate_schema(bind_engine=None):
    """Ensure newly added columns exist in existing database tables (SQLite & PostgreSQL compatible)."""
    target_engine = bind_engine or engine
    from sqlalchemy import inspect, text
    try:
        # Import models so Base.metadata is fully populated with all tables
        import models
        Base.metadata.create_all(bind=target_engine)

        inspector = inspect(target_engine)
        if "payment_events" in inspector.get_table_names():
            columns = {col["name"] for col in inspector.get_columns("payment_events")}
            is_postgres = (target_engine.dialect.name == "postgresql")
            dt_type = "TIMESTAMP" if is_postgres else "DATETIME"
            bool_default = "FALSE" if is_postgres else "0"

            with target_engine.begin() as conn:
                if "contact_count" not in columns:
                    conn.execute(text("ALTER TABLE payment_events ADD COLUMN contact_count INTEGER DEFAULT 0"))
                if "last_contacted_at" not in columns:
                    conn.execute(text(f"ALTER TABLE payment_events ADD COLUMN last_contacted_at {dt_type}"))
                if "disputed" not in columns:
                    conn.execute(text(f"ALTER TABLE payment_events ADD COLUMN disputed BOOLEAN DEFAULT {bool_default}"))
                if "fraud_suspected" not in columns:
                    conn.execute(text(f"ALTER TABLE payment_events ADD COLUMN fraud_suspected BOOLEAN DEFAULT {bool_default}"))
                if "ab_group" not in columns:
                    conn.execute(text("ALTER TABLE payment_events ADD COLUMN ab_group VARCHAR DEFAULT 'ai_group'"))
                if "escalation_stage" not in columns:
                    conn.execute(text("ALTER TABLE payment_events ADD COLUMN escalation_stage INTEGER DEFAULT 1"))
                if "webhook_event_id" not in columns:
                    conn.execute(text("ALTER TABLE payment_events ADD COLUMN webhook_event_id VARCHAR"))
                    try:
                        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_payment_events_webhook_event_id ON payment_events (webhook_event_id)"))
                    except Exception:
                        pass
                if "lifecycle_status" not in columns:
                    conn.execute(text("ALTER TABLE payment_events ADD COLUMN lifecycle_status VARCHAR DEFAULT 'PENDING'"))
                if "opted_out" not in columns:
                    conn.execute(text(f"ALTER TABLE payment_events ADD COLUMN opted_out BOOLEAN DEFAULT {bool_default}"))
    except Exception as e:
        print(f"Notice: schema migration skipped or failed: {e}")


# Run initial migration check
migrate_schema(engine)


def get_db():
    """FastAPI dependency — yields a DB session and closes it after the request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
