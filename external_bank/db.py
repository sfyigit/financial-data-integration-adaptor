"""
Per-Tenant SQLite Database Manager
Manages isolated SQLite databases for each bank (tenant).
"""

import os
import time
import logging
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker, Session
from models import Base

logger = logging.getLogger("external_bank.db")

DATABASE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "database")

# Cache engines per tenant to avoid recreating on every request
_engine_cache: dict = {}


def get_database_path(tenant_id: str) -> str:
    """Returns the SQLite database file path for the given tenant ID."""
    safe_tenant_id = tenant_id.lower().replace(" ", "_").replace("/", "_")
    return os.path.join(DATABASE_DIR, f"{safe_tenant_id}.sqlite")


def _cleanup_wal_files(db_path: str):
    """
    Remove stale WAL/SHM files that can cause 'disk I/O error'
    on Docker volume mounts (especially Windows hosts).
    """
    for suffix in ("-wal", "-shm"):
        wal_file = db_path + suffix
        if os.path.exists(wal_file):
            try:
                os.remove(wal_file)
                logger.info(f"Cleaned up stale file: {wal_file}")
            except OSError as e:
                logger.warning(f"Could not remove {wal_file}: {e}")


def get_engine(tenant_id: str):
    """Creates (or returns cached) a tenant-specific SQLAlchemy engine."""
    if tenant_id in _engine_cache:
        return _engine_cache[tenant_id]

    db_path = get_database_path(tenant_id)

    # Ensure database directory exists
    os.makedirs(os.path.dirname(db_path), exist_ok=True)

    # Clean up stale WAL/SHM files before opening
    if os.path.exists(db_path):
        _cleanup_wal_files(db_path)

    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
        echo=False,
        pool_pre_ping=True,
    )

    # Use DELETE journal mode (safer on Docker volume mounts) + foreign keys
    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=DELETE")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    _engine_cache[tenant_id] = engine
    return engine


def init_tenant_db(tenant_id: str):
    """Initializes the tenant database - creates tables if they don't exist."""
    engine = get_engine(tenant_id)
    Base.metadata.create_all(bind=engine)
    return engine


def get_session(tenant_id: str, max_retries: int = 3) -> Session:
    """
    Returns a database session for the given tenant.
    Retries on transient SQLite errors (e.g. file locking on Docker volumes).
    """
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            engine = init_tenant_db(tenant_id)
            SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
            session = SessionLocal()
            # Test the connection
            session.execute(text("SELECT 1"))
            return session
        except Exception as e:
            last_error = e
            logger.warning(
                f"[{tenant_id}] DB open attempt {attempt}/{max_retries} failed: {e}"
            )
            # Invalidate cached engine on failure
            _engine_cache.pop(tenant_id, None)
            if attempt < max_retries:
                time.sleep(1 * attempt)

    raise last_error


def list_tenants() -> list[str]:
    """Lists all existing tenants (banks)."""
    tenants = []
    if os.path.exists(DATABASE_DIR):
        for f in os.listdir(DATABASE_DIR):
            if f.endswith(".sqlite"):
                tenants.append(f.replace(".sqlite", "").upper())
    return sorted(tenants)
