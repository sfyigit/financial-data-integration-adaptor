"""
PostgreSQL Database Manager - External Bank Simulation
=======================================================
All tenants share a single PostgreSQL database (multi-tenant via tenant_id column).
Replaces the per-tenant SQLite approach for better concurrency, bulk insert
performance (via COPY), and Docker reliability.
"""

import os
import logging
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, Session
from models import Base

logger = logging.getLogger("external_bank.db")

DATABASE_URL = os.environ.get(
    "EXTERNAL_BANK_DB_URL",
    "postgresql://fsec_user:fsec_pg_pass_2026@postgres_db:5432/fsec_db",
)

# Use a schema prefix so external_bank tables don't clash with Django tables
SCHEMA = "ext_bank"

# Single engine for all tenants (PostgreSQL handles concurrency natively)
_engine = None
_SessionLocal = None


def get_engine():
    """Get or create the shared SQLAlchemy engine."""
    global _engine
    if _engine is None:
        _engine = create_engine(
            DATABASE_URL,
            pool_size=10,
            max_overflow=20,
            pool_pre_ping=True,
            pool_recycle=300,
            echo=False,
        )
    return _engine


def init_db():
    """
    Initialize the database: create schema and tables if they don't exist.
    Called once on application startup.
    """
    engine = get_engine()

    # Create schema
    with engine.connect() as conn:
        conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}"))
        conn.commit()

    # Create all tables within the schema
    Base.metadata.create_all(bind=engine)
    logger.info(f"Database initialized (schema: {SCHEMA})")


def get_session() -> Session:
    """Returns a database session."""
    global _SessionLocal
    if _SessionLocal is None:
        engine = get_engine()
        _SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    return _SessionLocal()


def list_tenants() -> list[str]:
    """Lists all existing tenants by querying distinct tenant_ids from data_versions."""
    session = get_session()
    try:
        from models import DataVersion
        result = session.query(DataVersion.tenant_id).distinct().all()
        return sorted([r[0] for r in result])
    except Exception as e:
        logger.warning(f"Could not list tenants: {e}")
        return []
    finally:
        session.close()
