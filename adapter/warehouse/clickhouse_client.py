"""
ClickHouse Data Warehouse Client
=================================
Handles all interactions with the ClickHouse OLAP database.

Features:
- Table creation (production + staging)
- Atomic data replacement using partition swap
- Data profiling (numeric stats, categorical stats, quality metrics)
- Tenant-isolated queries

Expanded schema to store full Turkish banking credit portfolio fields.
"""

import logging
import math
import re
import time
from datetime import datetime, date
from typing import Optional
from clickhouse_driver import Client as CHDriver

logger = logging.getLogger("adapter.warehouse")


def _safe_float(value, decimals: int = 4):
    """Sanitize a float value for JSON serialization.
    Replaces inf, -inf, NaN with None and rounds to given decimals.
    """
    if value is None:
        return None
    try:
        f = float(value)
        if math.isinf(f) or math.isnan(f):
            return None
        return round(f, decimals)
    except (TypeError, ValueError):
        return None


def _parse_date(date_str) -> Optional[date]:
    """Convert a date string (YYYY-MM-DD) to a Python date object for ClickHouse."""
    if not date_str:
        return None
    try:
        return datetime.strptime(str(date_str), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


class ClickHouseClient:
    """Client for ClickHouse data warehouse operations."""

    def __init__(self, host: str = None, port: int = None, database: str = None,
                 user: str = None, password: str = None):
        from django.conf import settings
        self.host = host or getattr(settings, "CLICKHOUSE_HOST", "localhost")
        self.port = port or getattr(settings, "CLICKHOUSE_PORT", 9000)
        raw_db = database or getattr(settings, "CLICKHOUSE_DATABASE", "fsec")
        # Allow only safe database names (alphanumeric + underscore)
        if not re.fullmatch(r"[A-Za-z0-9_]+", raw_db):
            raise ValueError("Invalid ClickHouse database name.")
        self.database = raw_db
        self.user = user or getattr(settings, "CLICKHOUSE_USER", "default")
        self.password = password or getattr(settings, "CLICKHOUSE_PASSWORD")
        if self.password is None:
            raise RuntimeError("CLICKHOUSE_PASSWORD setting must be configured.")
        self._client = None

    @property
    def client(self) -> CHDriver:
        """Lazy-initialized ClickHouse connection."""
        if self._client is None:
            self._client = CHDriver(
                host=self.host,
                port=self.port,
                database=self.database,
                user=self.user,
                password=self.password,
            )
        return self._client

    def _execute(self, query: str, params: dict = None):
        """Execute a query and return results."""
        logger.debug(f"Executing query: {query[:200]}...")
        return self.client.execute(query, params or {})

    # =========================================================================
    # Database & Table Initialization
    # =========================================================================

    def initialize(self):
        """Create database and all required tables."""
        self._create_database()
        self._create_loans_table()
        self._create_payments_table()
        logger.info("ClickHouse tables initialized successfully")

    def _create_database(self):
        """Create the database if it doesn't exist."""
        # Connect without database first
        temp_client = CHDriver(
            host=self.host, port=self.port,
            user=self.user, password=self.password,
        )
        temp_client.execute(f"CREATE DATABASE IF NOT EXISTS {self.database}")
        temp_client.disconnect()

    def _create_loans_table(self):
        """Create the loans production table partitioned by tenant_id."""
        self._execute("""
            CREATE TABLE IF NOT EXISTS loans (
                -- Core identifiers
                tenant_id                   String,
                loan_id                     String,
                loan_type                   String,

                -- Amounts
                amount                      Float64,
                outstanding_principal_balance Float64 DEFAULT 0,

                -- Interest rates (normalized + original)
                interest_rate               Float64,
                original_interest_rate      String,
                total_interest_amount       Float64 DEFAULT 0,
                kkdf_rate                   Float64 DEFAULT 0,
                kkdf_amount                 Float64 DEFAULT 0,
                bsmv_rate                   Float64 DEFAULT 0,
                bsmv_amount                 Float64 DEFAULT 0,

                -- Dates (normalized)
                start_date                  Nullable(Date),
                final_maturity_date         Nullable(Date),
                first_payment_date          Nullable(Date),
                loan_closing_date           Nullable(Date),
                original_start_date         String,

                -- Status
                status                      String,
                original_status             String,
                loan_status_flag            String DEFAULT '',
                days_past_due               Int32 DEFAULT 0,

                -- Customer info
                customer_id                 String DEFAULT '',
                customer_type               String DEFAULT '',

                -- Installment info
                total_installment_count     Nullable(Int32),
                outstanding_installment_count Nullable(Int32),
                paid_installment_count      Nullable(Int32),
                installment_frequency       Nullable(Int32),
                grace_period_months         Nullable(Int32),

                -- Insurance
                insurance_included          String DEFAULT '',

                -- Location
                customer_district_code      String DEFAULT '',
                customer_province_code      String DEFAULT '',
                customer_region_code        String DEFAULT '',

                -- Ratings
                internal_rating             String DEFAULT '',
                external_rating             String DEFAULT '',

                -- Commercial-only
                loan_product_type           String DEFAULT '',
                sector_code                 String DEFAULT '',
                internal_credit_rating      String DEFAULT '',
                default_probability         Float64 DEFAULT 0,
                risk_class                  String DEFAULT '',
                customer_segment            String DEFAULT '',

                -- Metadata
                synced_at                   DateTime DEFAULT now()
            ) ENGINE = MergeTree()
            PARTITION BY tenant_id
            ORDER BY (tenant_id, loan_type, loan_id)
        """)

    def _create_payments_table(self):
        """Create the payments production table partitioned by tenant_id."""
        self._execute("""
            CREATE TABLE IF NOT EXISTS payments (
                -- Core identifiers
                tenant_id                   String,
                payment_id                  String,
                loan_id                     String,
                loan_type                   String,
                installment_number          Nullable(Int32),

                -- Dates (normalized)
                payment_date                Nullable(Date),
                actual_payment_date         Nullable(Date),
                scheduled_payment_date      Nullable(Date),
                original_payment_date       String,

                -- Amounts
                payment_amount              Float64,
                principal_component         Float64 DEFAULT 0,
                interest_component          Float64 DEFAULT 0,
                kkdf_component              Float64 DEFAULT 0,
                bsmv_component              Float64 DEFAULT 0,

                -- Status
                installment_status          String DEFAULT '',

                -- Remaining balances
                remaining_principal         Float64 DEFAULT 0,
                remaining_interest          Float64 DEFAULT 0,
                remaining_kkdf              Float64 DEFAULT 0,
                remaining_bsmv              Float64 DEFAULT 0,

                -- Metadata
                synced_at                   DateTime DEFAULT now()
            ) ENGINE = MergeTree()
            PARTITION BY tenant_id
            ORDER BY (tenant_id, loan_type, payment_id)
        """)

    # =========================================================================
    # Staging Table Operations (Atomic Replacement)
    # =========================================================================

    def create_staging_table(self, table_name: str):
        """
        Create a staging table with the same structure as the production table.
        Drops existing staging table first to ensure clean state.
        """
        if table_name not in ("loans", "payments"):
            raise ValueError("Invalid table name for staging creation.")
        staging_name = f"{table_name}_staging"
        self._execute(f"DROP TABLE IF EXISTS {staging_name}")
        self._execute(f"CREATE TABLE {staging_name} AS {table_name}")
        logger.info(f"Staging table '{staging_name}' created")

    def load_loans_to_staging(self, tenant_id: str, records: list[dict]):
        """
        Insert normalized loan records into the staging table.

        Args:
            tenant_id: Tenant identifier.
            records: List of normalized loan record dictionaries.
        """
        if not records:
            return

        staging_name = "loans_staging"
        rows = []
        for r in records:
            rows.append({
                "tenant_id": tenant_id,
                "loan_id": r["loan_id"],
                "loan_type": r["loan_type"],
                "amount": r["amount"],
                "outstanding_principal_balance": r.get("outstanding_principal_balance", 0.0),
                "interest_rate": r["interest_rate"] or 0.0,
                "original_interest_rate": r.get("original_interest_rate", ""),
                "total_interest_amount": r.get("total_interest_amount", 0.0),
                "kkdf_rate": r.get("kkdf_rate", 0.0),
                "kkdf_amount": r.get("kkdf_amount", 0.0),
                "bsmv_rate": r.get("bsmv_rate", 0.0),
                "bsmv_amount": r.get("bsmv_amount", 0.0),
                "start_date": _parse_date(r["start_date"]),
                "final_maturity_date": _parse_date(r.get("final_maturity_date")),
                "first_payment_date": _parse_date(r.get("first_payment_date")),
                "loan_closing_date": _parse_date(r.get("loan_closing_date")),
                "original_start_date": r.get("original_start_date", ""),
                "status": r["status"] or "UNKNOWN",
                "original_status": r.get("original_status", ""),
                "loan_status_flag": r.get("loan_status_flag", ""),
                "days_past_due": r.get("days_past_due", 0),
                "customer_id": r.get("customer_id", ""),
                "customer_type": r.get("customer_type", ""),
                "total_installment_count": r.get("total_installment_count"),
                "outstanding_installment_count": r.get("outstanding_installment_count"),
                "paid_installment_count": r.get("paid_installment_count"),
                "installment_frequency": r.get("installment_frequency"),
                "grace_period_months": r.get("grace_period_months"),
                "insurance_included": r.get("insurance_included", ""),
                "customer_district_code": r.get("customer_district_code", ""),
                "customer_province_code": r.get("customer_province_code", ""),
                "customer_region_code": r.get("customer_region_code", ""),
                "internal_rating": r.get("internal_rating", ""),
                "external_rating": r.get("external_rating", ""),
                "loan_product_type": r.get("loan_product_type", ""),
                "sector_code": r.get("sector_code", ""),
                "internal_credit_rating": r.get("internal_credit_rating", ""),
                "default_probability": r.get("default_probability", 0.0),
                "risk_class": r.get("risk_class", ""),
                "customer_segment": r.get("customer_segment", ""),
            })

        columns = ", ".join(rows[0].keys())
        self._execute(
            f"INSERT INTO {staging_name} ({columns}) VALUES",
            rows,
        )
        logger.info(f"Loaded {len(rows)} loan records into staging for tenant '{tenant_id}'")

    def load_payments_to_staging(self, tenant_id: str, records: list[dict]):
        """
        Insert normalized payment records into the staging table.

        Args:
            tenant_id: Tenant identifier.
            records: List of normalized payment record dictionaries.
        """
        if not records:
            return

        staging_name = "payments_staging"
        rows = []
        for r in records:
            rows.append({
                "tenant_id": tenant_id,
                "payment_id": r["payment_id"],
                "loan_id": r["loan_id"],
                "loan_type": r["loan_type"],
                "installment_number": r.get("installment_number"),
                "payment_date": _parse_date(r["payment_date"]),
                "actual_payment_date": _parse_date(r.get("actual_payment_date")),
                "scheduled_payment_date": _parse_date(r.get("scheduled_payment_date")),
                "original_payment_date": r.get("original_payment_date", ""),
                "payment_amount": r["payment_amount"],
                "principal_component": r.get("principal_component", 0.0),
                "interest_component": r.get("interest_component", 0.0),
                "kkdf_component": r.get("kkdf_component", 0.0),
                "bsmv_component": r.get("bsmv_component", 0.0),
                "installment_status": r.get("installment_status", ""),
                "remaining_principal": r.get("remaining_principal", 0.0),
                "remaining_interest": r.get("remaining_interest", 0.0),
                "remaining_kkdf": r.get("remaining_kkdf", 0.0),
                "remaining_bsmv": r.get("remaining_bsmv", 0.0),
            })

        columns = ", ".join(rows[0].keys())
        self._execute(
            f"INSERT INTO {staging_name} ({columns}) VALUES",
            rows,
        )
        logger.info(f"Loaded {len(rows)} payment records into staging for tenant '{tenant_id}'")

    def swap_staging_to_production(self, table_name: str, tenant_id: str, loan_type: str = None):
        """
        Atomically replace production data with staging data for a specific
        tenant AND loan_type combination.

        Strategy:
        1. Delete existing tenant+loan_type data from production table.
        2. Insert staging data into production table.
        3. Drop the staging table.

        CRITICAL: The DELETE must filter by BOTH tenant_id AND loan_type
        to preserve data for other loan_types of the same tenant.
        E.g., uploading COMMERCIAL data must NOT delete RETAIL data.

        Args:
            table_name: Base table name ('loans' or 'payments').
            tenant_id: Tenant whose data is being replaced.
            loan_type: Loan type being replaced ('RETAIL' or 'COMMERCIAL').
                       If None, replaces ALL data for the tenant (legacy behavior).
        """
        if table_name not in ("loans", "payments"):
            raise ValueError("Invalid table name for atomic swap.")
        staging_name = f"{table_name}_staging"

        try:
            # Step 1: Remove old tenant+loan_type data from production
            if loan_type:
                self._execute(
                    f"ALTER TABLE {table_name} DELETE "
                    f"WHERE tenant_id = %(tenant_id)s AND loan_type = %(loan_type)s",
                    {"tenant_id": tenant_id, "loan_type": loan_type},
                )
                logger.info(
                    f"Deleted existing {table_name} data for "
                    f"tenant='{tenant_id}', loan_type='{loan_type}'"
                )
            else:
                self._execute(
                    f"ALTER TABLE {table_name} DELETE WHERE tenant_id = %(tenant_id)s",
                    {"tenant_id": tenant_id},
                )

            # ClickHouse DELETE is async — wait for mutations to complete
            # Wait up to ~60 seconds (120 * 0.5s) for large partitions.
            for _ in range(120):
                mutations = self._execute(
                    "SELECT count() FROM system.mutations "
                    "WHERE is_done = 0 AND database = %(db)s AND table = %(tbl)s",
                    {"db": self.database, "tbl": table_name},
                )
                if mutations and mutations[0][0] == 0:
                    break
                time.sleep(0.5)

            # Step 2: Move staging data to production
            self._execute(
                f"INSERT INTO {table_name} SELECT * FROM {staging_name} "
                f"WHERE tenant_id = %(tenant_id)s",
                {"tenant_id": tenant_id},
            )

            # Step 3: Clean up staging
            self._execute(f"DROP TABLE IF EXISTS {staging_name}")

            logger.info(
                f"Atomic swap completed: {staging_name} -> {table_name} "
                f"for tenant '{tenant_id}'"
                + (f", loan_type '{loan_type}'" if loan_type else "")
            )

        except Exception as e:
            # If anything fails, try to clean up staging
            try:
                self._execute(f"DROP TABLE IF EXISTS {staging_name}")
            except Exception:
                pass
            logger.error(f"Atomic swap failed for {table_name}/{tenant_id}: {e}")
            raise

    def drop_staging_table(self, table_name: str):
        """Drop a staging table (used when validation fails)."""
        if table_name not in ("loans", "payments"):
            raise ValueError("Invalid table name for staging drop.")
        staging_name = f"{table_name}_staging"
        self._execute(f"DROP TABLE IF EXISTS {staging_name}")
        logger.info(f"Staging table '{staging_name}' dropped (validation failed)")

    # =========================================================================
    # Data Profiling
    # =========================================================================

    def compute_loan_profile(self, tenant_id: str) -> dict:
        """
        Compute profiling statistics for loan data.

        Returns:
            Dictionary with numeric stats, categorical stats, and quality metrics.
        """
        profile = {"numeric": {}, "categorical": {}, "quality": {}}

        try:
            # Numeric profiling: amount
            result = self._execute(
                "SELECT "
                "  min(amount) as min_amount, "
                "  max(amount) as max_amount, "
                "  avg(amount) as avg_amount, "
                "  stddevPop(amount) as stddev_amount, "
                "  count() as total_count "
                "FROM loans WHERE tenant_id = %(tenant_id)s",
                {"tenant_id": tenant_id},
            )
            if result:
                row = result[0]
                profile["numeric"]["amount"] = {
                    "min": _safe_float(row[0], 2), "max": _safe_float(row[1], 2),
                    "avg": _safe_float(row[2], 2), "stddev": _safe_float(row[3], 2),
                    "count": row[4],
                }

            # Numeric profiling: interest_rate
            result = self._execute(
                "SELECT "
                "  min(interest_rate) as min_rate, "
                "  max(interest_rate) as max_rate, "
                "  avg(interest_rate) as avg_rate, "
                "  stddevPop(interest_rate) as stddev_rate "
                "FROM loans WHERE tenant_id = %(tenant_id)s",
                {"tenant_id": tenant_id},
            )
            if result:
                row = result[0]
                profile["numeric"]["interest_rate"] = {
                    "min": _safe_float(row[0], 6), "max": _safe_float(row[1], 6),
                    "avg": _safe_float(row[2], 6), "stddev": _safe_float(row[3], 6),
                }

            # Numeric profiling: days_past_due
            result = self._execute(
                "SELECT "
                "  min(days_past_due), max(days_past_due), "
                "  avg(days_past_due), stddevPop(days_past_due) "
                "FROM loans WHERE tenant_id = %(tenant_id)s",
                {"tenant_id": tenant_id},
            )
            if result:
                row = result[0]
                profile["numeric"]["days_past_due"] = {
                    "min": _safe_float(row[0], 0), "max": _safe_float(row[1], 0),
                    "avg": _safe_float(row[2], 2), "stddev": _safe_float(row[3], 2),
                }

            # Categorical profiling: status
            result = self._execute(
                "SELECT status, count() as cnt "
                "FROM loans WHERE tenant_id = %(tenant_id)s "
                "GROUP BY status ORDER BY cnt DESC",
                {"tenant_id": tenant_id},
            )
            status_dist = {row[0]: row[1] for row in result}
            profile["categorical"]["status"] = {
                "unique_count": len(status_dist),
                "mode": max(status_dist, key=status_dist.get) if status_dist else None,
                "distribution": status_dist,
            }

            # Categorical profiling: loan_type
            result = self._execute(
                "SELECT loan_type, count() as cnt "
                "FROM loans WHERE tenant_id = %(tenant_id)s "
                "GROUP BY loan_type ORDER BY cnt DESC",
                {"tenant_id": tenant_id},
            )
            type_dist = {row[0]: row[1] for row in result}
            profile["categorical"]["loan_type"] = {
                "unique_count": len(type_dist),
                "mode": max(type_dist, key=type_dist.get) if type_dist else None,
                "distribution": type_dist,
            }

            # Categorical profiling: customer_type
            result = self._execute(
                "SELECT customer_type, count() as cnt "
                "FROM loans WHERE tenant_id = %(tenant_id)s "
                "AND customer_type != '' "
                "GROUP BY customer_type ORDER BY cnt DESC",
                {"tenant_id": tenant_id},
            )
            ctype_dist = {row[0]: row[1] for row in result}
            if ctype_dist:
                profile["categorical"]["customer_type"] = {
                    "unique_count": len(ctype_dist),
                    "mode": max(ctype_dist, key=ctype_dist.get),
                    "distribution": ctype_dist,
                }

            # Quality metrics: null/missing ratios
            result = self._execute(
                "SELECT "
                "  countIf(start_date IS NULL) / count() as null_date_ratio, "
                "  countIf(interest_rate = 0) / count() as zero_rate_ratio, "
                "  countIf(status = 'UNKNOWN') / count() as unknown_status_ratio, "
                "  count() as total "
                "FROM loans WHERE tenant_id = %(tenant_id)s",
                {"tenant_id": tenant_id},
            )
            if result:
                row = result[0]
                profile["quality"] = {
                    "null_date_ratio": _safe_float(row[0], 4),
                    "zero_rate_ratio": _safe_float(row[1], 4),
                    "unknown_status_ratio": _safe_float(row[2], 4),
                    "total_records": row[3],
                }

        except Exception as e:
            logger.error(f"Loan profiling failed for tenant '{tenant_id}': {e}")

        return profile

    def compute_payment_profile(self, tenant_id: str) -> dict:
        """
        Compute profiling statistics for payment data.

        Returns:
            Dictionary with numeric stats, categorical stats, and quality metrics.
        """
        profile = {"numeric": {}, "categorical": {}, "quality": {}}

        try:
            # Numeric profiling: payment_amount
            result = self._execute(
                "SELECT "
                "  min(payment_amount), max(payment_amount), "
                "  avg(payment_amount), stddevPop(payment_amount), "
                "  count() "
                "FROM payments WHERE tenant_id = %(tenant_id)s",
                {"tenant_id": tenant_id},
            )
            if result:
                row = result[0]
                profile["numeric"]["payment_amount"] = {
                    "min": _safe_float(row[0], 2), "max": _safe_float(row[1], 2),
                    "avg": _safe_float(row[2], 2), "stddev": _safe_float(row[3], 2),
                    "count": row[4],
                }

            # Categorical profiling: installment_status
            result = self._execute(
                "SELECT installment_status, count() as cnt "
                "FROM payments WHERE tenant_id = %(tenant_id)s "
                "AND installment_status != '' "
                "GROUP BY installment_status ORDER BY cnt DESC",
                {"tenant_id": tenant_id},
            )
            status_dist = {row[0]: row[1] for row in result}
            if status_dist:
                profile["categorical"]["installment_status"] = {
                    "unique_count": len(status_dist),
                    "mode": max(status_dist, key=status_dist.get),
                    "distribution": status_dist,
                }

            # Quality metrics
            result = self._execute(
                "SELECT "
                "  countIf(payment_date IS NULL) / count() as null_date_ratio, "
                "  count() "
                "FROM payments WHERE tenant_id = %(tenant_id)s",
                {"tenant_id": tenant_id},
            )
            if result:
                row = result[0]
                profile["quality"] = {
                    "null_date_ratio": _safe_float(row[0], 4),
                    "total_records": row[1],
                }

        except Exception as e:
            logger.error(f"Payment profiling failed for tenant '{tenant_id}': {e}")

        return profile

    def get_profiling_summary(self, tenant_ids: list[str] = None) -> dict:
        """
        Get a summary of profiling stats for given tenants.
        If tenant_ids is None, profiles all tenants found in the database.
        Used by the dashboard API.

        Args:
            tenant_ids: Optional list of tenant IDs to profile.
        """
        summary = {}
        try:
            if tenant_ids is None:
                # Get all distinct tenants
                result = self._execute("SELECT DISTINCT tenant_id FROM loans")
                tenant_ids = [row[0] for row in result]

            for tid in tenant_ids:
                summary[tid] = {
                    "loans": self.compute_loan_profile(tid),
                    "payments": self.compute_payment_profile(tid),
                }
        except Exception as e:
            logger.error(f"Profiling summary failed: {e}")

        return summary

    # =========================================================================
    # Data Query
    # =========================================================================

    def get_loan_count(self, tenant_id: str) -> int:
        """Get total loan count for a tenant."""
        result = self._execute(
            "SELECT count() FROM loans WHERE tenant_id = %(tenant_id)s",
            {"tenant_id": tenant_id},
        )
        return result[0][0] if result else 0

    def get_payment_count(self, tenant_id: str) -> int:
        """Get total payment count for a tenant."""
        result = self._execute(
            "SELECT count() FROM payments WHERE tenant_id = %(tenant_id)s",
            {"tenant_id": tenant_id},
        )
        return result[0][0] if result else 0

    # =========================================================================
    # Data Browsing (for Data Tables page)
    # =========================================================================

    def get_loans(self, tenant_id: str, loan_type: str = None,
                  limit: int = 50, offset: int = 0) -> dict:
        """
        Retrieve loan records with pagination.

        Returns:
            Dictionary with 'records', 'total', 'limit', 'offset'.
        """
        where = "WHERE tenant_id = %(tenant_id)s"
        params = {"tenant_id": tenant_id}

        if loan_type:
            where += " AND loan_type = %(loan_type)s"
            params["loan_type"] = loan_type

        # Total count
        total_result = self._execute(
            f"SELECT count() FROM loans {where}", params
        )
        total = total_result[0][0] if total_result else 0

        # Fetch records (limit/offset injected directly as validated ints)
        result = self._execute(
            f"SELECT tenant_id, loan_id, loan_type, amount, interest_rate, "
            f"original_interest_rate, start_date, status, original_status, "
            f"customer_id, customer_segment, synced_at "
            f"FROM loans {where} "
            f"ORDER BY loan_id "
            f"LIMIT {int(limit)} OFFSET {int(offset)}",
            params,
        )

        columns = [
            "tenant_id", "loan_id", "loan_type", "amount", "interest_rate",
            "original_interest_rate", "start_date", "status", "original_status",
            "customer_id", "customer_segment", "synced_at",
        ]
        records = [dict(zip(columns, row)) for row in result]

        # Serialize dates
        for rec in records:
            for key in ("start_date", "synced_at"):
                val = rec.get(key)
                if val and hasattr(val, "isoformat"):
                    rec[key] = val.isoformat()

        return {
            "records": records,
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def get_payments(self, tenant_id: str, loan_type: str = None,
                     limit: int = 50, offset: int = 0) -> dict:
        """
        Retrieve payment records with pagination.

        Returns:
            Dictionary with 'records', 'total', 'limit', 'offset'.
        """
        where = "WHERE tenant_id = %(tenant_id)s"
        params = {"tenant_id": tenant_id}

        if loan_type:
            where += " AND loan_type = %(loan_type)s"
            params["loan_type"] = loan_type

        # Total count
        total_result = self._execute(
            f"SELECT count() FROM payments {where}", params
        )
        total = total_result[0][0] if total_result else 0

        # Fetch records (limit/offset injected directly as validated ints)
        result = self._execute(
            f"SELECT tenant_id, payment_id, loan_id, loan_type, payment_amount, "
            f"payment_date, original_payment_date, installment_status, "
            f"principal_component, interest_component, synced_at "
            f"FROM payments {where} "
            f"ORDER BY payment_id "
            f"LIMIT {int(limit)} OFFSET {int(offset)}",
            params,
        )

        columns = [
            "tenant_id", "payment_id", "loan_id", "loan_type", "payment_amount",
            "payment_date", "original_payment_date", "installment_status",
            "principal_component", "interest_component", "synced_at",
        ]
        records = [dict(zip(columns, row)) for row in result]

        # Serialize dates
        for rec in records:
            for key in ("payment_date", "synced_at"):
                val = rec.get(key)
                if val and hasattr(val, "isoformat"):
                    rec[key] = val.isoformat()

        return {
            "records": records,
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def disconnect(self):
        """Close the ClickHouse connection."""
        if self._client:
            self._client.disconnect()
            self._client = None
