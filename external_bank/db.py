"""
Per-Tenant SQLite Database Manager
Manages isolated SQLite databases for each bank (tenant).
"""

import os
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, Session
from models import Base

DATABASE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "database")


def get_database_path(tenant_id: str) -> str:
    """Returns the SQLite database file path for the given tenant ID."""
    safe_tenant_id = tenant_id.lower().replace(" ", "_").replace("/", "_")
    return os.path.join(DATABASE_DIR, f"{safe_tenant_id}.sqlite")


def get_engine(tenant_id: str):
    """Creates a tenant-specific SQLAlchemy engine."""
    db_path = get_database_path(tenant_id)
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
        echo=False,
    )

    # Enable SQLite WAL mode and foreign keys
    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def init_tenant_db(tenant_id: str):
    """Initializes the tenant database - creates tables if they don't exist."""
    engine = get_engine(tenant_id)
    Base.metadata.create_all(bind=engine)
    return engine


def get_session(tenant_id: str) -> Session:
    """Returns a database session for the given tenant."""
    engine = init_tenant_db(tenant_id)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    return SessionLocal()


def list_tenants() -> list[str]:
    """Lists all existing tenants (banks)."""
    tenants = []
    if os.path.exists(DATABASE_DIR):
        for f in os.listdir(DATABASE_DIR):
            if f.endswith(".sqlite"):
                tenants.append(f.replace(".sqlite", "").upper())
    return sorted(tenants)
