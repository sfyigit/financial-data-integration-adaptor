# FSec — Architecture Document

## Table of Contents

1. [Overview](#1-overview)
2. [System Architecture](#2-system-architecture)
3. [Service Components](#3-service-components)
4. [Data Flow](#4-data-flow)
5. [Multi-Tenancy & Isolation](#5-multi-tenancy--isolation)
6. [Authentication & Authorization](#6-authentication--authorization)
7. [Validation & Normalization](#7-validation--normalization)
8. [Atomic Replacement (ClickHouse)](#8-atomic-replacement-clickhouse)
9. [Background Tasks & Scheduling](#9-background-tasks--scheduling)
10. [Data Warehouse & Profiling](#10-data-warehouse--profiling)
11. [Monitoring & Observability](#11-monitoring--observability)
12. [Testing Strategy](#12-testing-strategy)
13. [Infrastructure & Docker Compose](#13-infrastructure--docker-compose)
14. [Database Schema](#14-database-schema)
15. [API Endpoints](#15-api-endpoints)
16. [Error Handling & Resilience](#16-error-handling--resilience)
17. [Possible Future Improvements](#17-possible-future-improvements)

---

## 1. Overview

**FSec** is a multi-tenant SaaS platform that integrates with external banking systems. It extracts credit portfolio data, validates and normalizes it, then stores the processed data in **ClickHouse** for Asset-Backed Securities (ABS) analysis.

### Core Design Principles

| Principle | Description |
|---|---|
| **Tenant Isolation** | BANK001 data can never be accessed by BANK002. |
| **Loan Type Isolation** | RETAIL and COMMERCIAL data coexist independently within the same tenant. |
| **All-or-Nothing** | If a critical validation error occurs, **no** production data is modified. |
| **Atomic Swap** | The staging → production transition in ClickHouse is performed atomically, scoped by tenant + loan_type. |
| **Full Replacement** | New data for the same tenant + loan_type completely replaces existing data. |
| **Periodic Sync** | Celery Beat periodically checks for updates from the external bank. |
| **Observability** | End-to-end metrics and dashboards via Prometheus + Grafana. |

---

## 2. System Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                        Docker Network (fsec_network)                 │
│                                                                      │
│  ┌──────────────┐    ┌──────────────┐    ┌───────────────────┐      │
│  │  External     │    │  Django       │    │  ClickHouse       │      │
│  │  Bank API     │◄───│  Adapter API  │───►│  Data Warehouse   │      │
│  │  (FastAPI)    │    │  (DRF)        │    │  (OLAP)           │      │
│  │  :8001        │    │  :8000        │    │  :8123/:9000      │      │
│  └──────┬───────┘    └──────┬───────┘    └───────────────────┘      │
│         │                   │                                        │
│         │            ┌──────┴────────┐                               │
│         │      ┌─────▼─────┐   ┌────▼─────────┐                     │
│         │      │  Celery    │   │  Celery       │                    │
│         │      │  Worker    │   │  Beat         │                    │
│         │      └─────┬──────┘   └───────────────┘                   │
│         │            │                                               │
│  ┌──────▼──────┐     │      ┌───────────────┐                       │
│  │  PostgreSQL  │◄────┘      │  Redis        │                       │
│  │  (Django +   │            │  :6379        │                       │
│  │  Ext. Bank)  │            └───────────────┘                       │
│  │  :5432       │                                                    │
│  └──────────────┘    ┌──────────────┐    ┌──────────────┐           │
│                      │  Prometheus  │    │  Grafana     │           │
│                      │  :9090       │    │  :3000       │           │
│                      └──────────────┘    └──────────────┘           │
└──────────────────────────────────────────────────────────────────────┘
```

**Current data path:** Users upload CSV files to the **External Bank API** via the **Django Adapter API**. The External Bank writes data into **PostgreSQL** (under the `ext_bank` schema). The Adapter's **SyncService** fetches data from the External Bank using JSON cursor pagination, validates and normalizes it, then loads the results into **ClickHouse**.

---

## 3. Service Components

### 3.1 External Bank API (FastAPI + PostgreSQL)

Simulates an external banking system. Uses a single PostgreSQL database with a multi-tenant structure under the `ext_bank` schema. The CSV upload endpoint writes data directly to PostgreSQL.

**Authentication:** All data endpoints require `X-API-KEY` header. API keys are stored in `ext_bank.api_keys` table and managed via the `manage_api_keys.py` CLI tool.

| Method | Path | Auth | Description |
|---|---|---|---|
| `POST` | `/upload` | 🔐 | CSV upload → PostgreSQL (ext_bank) |
| `GET` | `/data` | 🔐 | JSON data (cursor-based pagination via `after_id`) |
| `GET` | `/version` | 🔐 | Version info for a tenant (version, checksum, record_count) |
| `GET` | `/tenants` | 🔐 | List of registered tenants |
| `GET` | `/health` | — | Health check (public) |

**Upload flow:**
1. CSV file is read and rows are parsed.
2. Existing records for the given `tenant_id + loan_type` are deleted (full replacement).
3. New rows are written to PostgreSQL in batches of 5K using `execute_values`.
4. `DataVersion` and `FileUpload` records are updated (version, checksum, record_count).

### 3.2 Django Adapter API (Django + DRF)

The multi-tenant SaaS layer. Provides tenant management, user & membership operations, sync orchestration, web UI, and REST API.

### 3.3 Adapter Business Logic Layer

```
┌────────────────────────────────────────┐
│  Celery Tasks (sync_tasks.py)          │  ← scheduling, triggers, concurrency guard
├────────────────────────────────────────┤
│  SyncService (sync_service.py)         │  ← pipeline orchestration
├────────────────────────────────────────┤
│  Validator          │  Normalizer      │  ← business rules
├────────────────────────────────────────┤
│  ClickHouseClient                      │  ← data warehouse operations (loan_type-aware swap)
└────────────────────────────────────────┘
```

### 3.4 Background Services

- **Celery Worker** — Executes sync jobs.
- **Celery Beat** — Periodically triggers the `check_for_updates` task.
- **Redis** — Used as the Celery broker and result backend.

### 3.5 Monitoring

- **Prometheus** — Collects metrics from all services.
- **Grafana** — Provides FSec-specific dashboards.

---

## 4. Data Flow

### CSV Upload Flow

```
User / API Client
    ↓
Django Adapter (/upload/ web page or /api/upload-csv/)
    ↓  (does tenant exist? is user a member of this tenant? is file valid?)
External Bank API (POST /upload)
    ↓
PostgreSQL — ext_bank.loans / ext_bank.payments
```

- **On the Django side:**
  - `tenant_id`, `file_type` (`loans` / `payments`), and `loan_type` (`RETAIL` / `COMMERCIAL`) are validated.
  - The user's tenant membership is verified.
  - File extension (`.csv`), size, and content type are checked.
- **On the External Bank side:**
  - Existing PostgreSQL records for `tenant_id + loan_type` are deleted.
  - New rows are written in batches of 5K.
  - `DataVersion` and `FileUpload` tables are updated.

### Sync Pipeline (v4 — PostgreSQL-based)

```
Celery Beat (every 5 min) → check_for_updates()
    │
    ▼
External Bank /version call for all active tenants
    │
    ▼
For each new version: run_sync_for_tenant(tenant_id) [Celery task]
    │
    ├── Step 1: Fetch data from External Bank /data via JSON cursor pagination (50K chunks)
    ├── Step 2: Field-level validation for all records
    ├── Step 3: Cross-file integrity check for payments (does loan_id exist?)
    │       └── On failure: SyncLog = failed, production data preserved
    ├── Step 4: Normalize (dates, rates, statuses) — 100K batches
    ├── Step 5: Batch insert into ClickHouse staging table
    ├── Step 6: Atomic swap (staging → production), scoped by tenant + loan_type
    └── Step 7: Update SyncState + compute profiling metrics
```

---

## 5. Multi-Tenancy & Isolation

### Isolation Layers

| Layer | Mechanism |
|---|---|
| **External Bank (PostgreSQL)** | `tenant_id` column + `ext_bank` schema + filtered queries |
| **ClickHouse** | `PARTITION BY tenant_id`; DELETE and INSERT scoped by `tenant_id + loan_type` |
| **PostgreSQL (Django)** | `Tenant` / `TenantMembership` models + FK relationships |
| **API** | DRF permissions + queryset filters (`IsTenantMember`, `get_user_tenants`) |

### Access Matrix

| Operation | Super Admin | Tenant Member | API Key | Anonymous |
|---|:---:|:---:|:---:|:---:|
| List all tenants | ✅ | ❌ | ❌ | ❌ |
| View own tenant data | ✅ | ✅ | ✅ | ❌ |
| View other tenant data | ✅ | ❌ | ❌ | ❌ |
| Upload CSV | ✅ | ✅ | ✅ | ❌ |
| Trigger sync | ✅ | ✅ | ✅ | ❌ |
| Manage memberships | ✅ | ❌ | ❌ | ❌ |
| Register user + tenant | — | — | — | ✅ |

---

## 6. Authentication & Authorization

The system supports multiple authentication mechanisms across different services:

### Django Adapter API

| Method | Use Case | Mechanism |
|---|---|---|
| **JWT** | Programmatic API access | `Authorization: Bearer <token>` |
| **API Key** | Machine-to-machine (M2M) | `X-API-Key: <tenant-api-key>` |
| **Session** | Web UI (dashboard & forms) | Django session cookie + CSRF |

### External Bank API

| Method | Use Case | Mechanism |
|---|---|---|
| **API Key** | Service-to-service auth | `X-API-KEY: <service-api-key>` |

The External Bank API requires authentication for all data endpoints (`/upload`, `/data`, `/version`, `/tenants`). Only `/health` and `/metrics` endpoints remain public for monitoring purposes.

API keys are stored in the `ext_bank.api_keys` table and can be managed via CLI:

```bash
# Create a new API key
docker exec external_bank_api python manage_api_keys.py create \
    --service "django_adapter" --description "SaaS sync service"

# List all keys
docker exec external_bank_api python manage_api_keys.py list

# Deactivate/activate/delete keys
docker exec external_bank_api python manage_api_keys.py deactivate --key "fsec_..."
```

- **JWT**:
  - Access token: 2 hours by default (configurable via env).
  - Refresh token: 7 days by default.
  - Token rotation enabled; old tokens are blacklisted after rotation.
- **API Key**:
  - Each `Tenant` model has an `api_key` field.
  - Requests authenticated via API key can only access that tenant's data.
- **Session**:
  - Dashboard, CSV upload, and data explorer pages use session auth.
  - In production, session cookies are set as `Secure` and `HttpOnly`.

### Permission Classes

- `IsTenantMember` — Verifies that the user belongs to the requested tenant.
- `IsTenantAdmin` — Required for sensitive operations that need a tenant admin role.
- `IsAdminOrReadOnly` — Read: all authenticated users; Write: superusers only.

---

## 7. Validation & Normalization

### Field-Level Validation

**Loan records:**

| Field | Rule | Error Type |
|---|---|---|
| `loan_id` / `loan_account_number` | Required, unique | `missing_required`, `duplicate` |
| `amount` / `original_loan_amount` | Numeric, `0 < x ≤ 100B` | `invalid_type`, `range_violation` |
| `interest_rate` / `nominal_interest_rate` | Parseable, `0.0 ≤ x ≤ 1.0` | `invalid_format`, `range_violation` |
| `start_date` / `loan_start_date` | Parseable date | `invalid_format` |
| `status` / `loan_status_code` | Recognized label | `invalid_value` |

**Payment records:**

| Field | Rule | Error Type |
|---|---|---|
| `payment_id` / `loan_account_number` | Required or derivable | `missing_required` |
| `loan_id` / `loan_account_number` | Required | `missing_required` |
| `payment_amount` / `installment_amount` | Numeric, `≥ 0` | `invalid_type`, `range_violation` |
| `payment_date` / `actual_payment_date` / `scheduled_payment_date` | Parseable date | `invalid_format` |

### Cross-File Integrity

- Every payment record's `loan_id` must exist in the loan dataset.
- If orphan payments are detected:
  - The entire sync is **aborted**.
  - The existing ClickHouse production data remains unchanged.

### Normalization Rules

**Date Formats:**

`DD/MM/YYYY`, `MM/DD/YYYY`, `YYYYMMDD`, `DD-MM-YYYY`, `DD.MM.YYYY` → `YYYY-MM-DD`

**Interest Rates:**

| Input Value | Normalized |
|---|---|
| `"18.5%"` | `0.185` |
| `"1850 bps"` | `0.185` |
| `"18.5"` (treated as % if > 1) | `0.185` |

**Status Labels:**

| Input Value | Normalized |
|---|---|
| `Active`, `Open`, `Aktif` | `ACTIVE` |
| `Paid`, `Closed`, `Kapalı` | `CLOSED` |
| `Default`, `Gecikme` | `DEFAULT` |
| `Restructured`, `Yapılandırılmış` | `RESTRUCTURED` |
| Unrecognized values | `UNKNOWN` |

---

## 8. Atomic Replacement (ClickHouse)

Data updates in ClickHouse are performed using a **staging table** approach.

### Steps

```
1. CREATE TABLE loans_staging AS loans
2. INSERT normalized data INTO loans_staging
3. ALTER TABLE loans DELETE
     WHERE tenant_id = 'BANK001' AND loan_type = 'RETAIL'
4. Wait for ClickHouse mutation to complete (async DELETE)
5. INSERT INTO loans SELECT * FROM loans_staging WHERE tenant_id = 'BANK001'
6. DROP TABLE loans_staging
```

**Critical:** The DELETE statement filters by **both** `tenant_id` **and** `loan_type`. This ensures:

- Uploading COMMERCIAL data does not delete RETAIL data (and vice versa).
- Other tenants' data is never affected.

### Safety Guarantees

| Scenario | Behavior |
|---|---|
| Staging load fails | Staging table is dropped; production remains unchanged. |
| Swap fails | Staging is cleaned up, error is logged; production is preserved. |
| Validation fails | Staging table is never created. |
| Orphan payment detected | Sync aborted; production data preserved. |
| Other tenants' data | Unaffected — scoped by `WHERE tenant_id`. |
| Other loan_types' data | Unaffected — scoped by `WHERE loan_type`. |

**Full Replacement:** 1000 existing records + 2000 new records = **2000** records after sync (replace, not append).

---

## 9. Background Tasks & Scheduling

```
Celery Beat (scheduler)
├── Every 5 min: check_for_updates()
│   → Calls External Bank /version for all active tenants
│   → Skips tenants that already have a running sync (concurrency guard)
│   → If new version detected: run_sync_for_tenant.delay(tenant_id)
│
Celery Worker (executor)
├── run_sync_for_tenant() → SyncService.sync_tenant()
│   → Concurrency guard (only one sync job per tenant at a time)
│   → Fetches data from External Bank /data in JSON chunks
│   → Validate + normalize (100K batches) + load to ClickHouse staging
│   → Atomic swap scoped by tenant + loan_type
└── On failure: 2 retries with 30s delay, then SyncLog = failed
```

**Version Check Logic:** A sync is triggered if either the `version` number **or** the `checksum` from the External Bank has changed. Identical data is never re-synced.

---

## 10. Data Warehouse & Profiling

### ClickHouse Tables

**loans** — Full credit portfolio fields:

`tenant_id, loan_id, loan_type, amount, outstanding_principal_balance, interest_rate, original_interest_rate, total_interest_amount, kkdf_rate, kkdf_amount, bsmv_rate, bsmv_amount, start_date, final_maturity_date, first_payment_date, loan_closing_date, original_start_date, status, original_status, loan_status_flag, days_past_due, customer_id, customer_type, total_installment_count, outstanding_installment_count, paid_installment_count, installment_frequency, grace_period_months, insurance_included, customer_district_code, customer_province_code, customer_region_code, internal_rating, external_rating, loan_product_type, sector_code, internal_credit_rating, default_probability, risk_class, customer_segment, synced_at`

**payments** — Full payment fields:

`tenant_id, payment_id, loan_id, loan_type, installment_number, payment_date, actual_payment_date, scheduled_payment_date, original_payment_date, payment_amount, principal_component, interest_component, kkdf_component, bsmv_component, installment_status, remaining_principal, remaining_interest, remaining_kkdf, remaining_bsmv, synced_at`

Both tables use:
- **Engine:** `MergeTree()`
- **Partition:** `PARTITION BY tenant_id`
- **Order By:** `(tenant_id, loan_type, loan_id)` / `(tenant_id, loan_type, payment_id)`

### Profiling Metrics

| Category | Metrics |
|---|---|
| **Numeric** | Min, Max, Avg, StdDev — (`amount`, `interest_rate`, `days_past_due`, `payment_amount`) |
| **Categorical** | Unique count, Mode, Distribution — (`status`, `loan_type`, `customer_type`, `installment_status`) |
| **Quality** | Null date ratio, zero interest rate ratio, `UNKNOWN` status ratio |

Profiling results are visualized on the dashboard using **Chart.js**.

---

## 11. Monitoring & Observability

| Service | Library | Metrics |
|---|---|---|
| **Django Adapter** | `django-prometheus` | HTTP request count/duration, DB query metrics |
| **FastAPI External Bank** | `prometheus-fastapi-instrumentator` | HTTP request count/duration, response size, upload latency |
| **ClickHouse** | Built-in exporter (`:9363`) | Query count, memory usage |

Prometheus scrapes all services every **15 seconds**. The Grafana dashboard (`fsec-overview.json`) provides:

- Service health and request rates
- P50 / P90 / P99 response times
- Error rates (4xx / 5xx)
- Upload endpoint latency and file size distribution
- ClickHouse query and resource metrics

---

## 12. Testing Strategy

```
tests/
├── conftest.py                    # Shared fixtures (users, tenants, auth clients)
├── unit/
│   ├── test_normalizer.py         # Date, interest rate, status normalization tests
│   ├── test_validator.py          # Field validation, cross-file integrity tests
│   └── test_authentication.py     # Auth backends, permission classes
└── integration/
    ├── test_api_auth.py           # JWT, session, me, my-tenants endpoints
    ├── test_api_tenants.py        # Tenant CRUD, sync logs, dashboard data
    ├── test_tenant_isolation.py   # 🔒 Tenant leakage tests (critical)
    ├── test_registration.py       # Registration flow
    ├── test_upload_csv.py         # CSV upload (web UI + API)
    └── test_sync_pipeline.py      # Pipeline consistency and resilience tests
```

### Critical Test Scenarios

- **Tenant Isolation:** A BANK001 user must never be able to access BANK002 data on any endpoint. All JWT, API Key, and Superuser scenarios are tested.
- **Loan Type Isolation:** Uploading RETAIL data followed by COMMERCIAL data — both must remain independently accessible. Uploading COMMERCIAL must not delete RETAIL (and vice versa).
- **Consistency:** 1000 old records + 2000 new records → exactly **2000** records after sync (replace, not append).
- **Resilience:** In the event of invalid data or orphan payments, the production ClickHouse data must remain untouched.

---

## 13. Infrastructure & Docker Compose

### Services

| Service | Container | Ports | Description |
|---|---|---|---|
| External Bank | `external_bank_api` | 8001 → 8000 | FastAPI + PostgreSQL client |
| PostgreSQL | `postgres_db` | 5432 | Shared DB for Django and External Bank |
| Redis | `redis_broker` | 6379 | Celery broker / result backend |
| ClickHouse | `clickhouse_dw` | 8123, 9000, 9363 | OLAP data warehouse |
| Django Adapter | `django_adapter` | 8000 | SaaS API + web UI |
| Celery Worker | `celery_worker` | — | Background sync tasks (4 workers) |
| Celery Beat | `celery_beat` | — | Periodic task scheduler |
| Prometheus | `prometheus` | 9090 | Metrics collector |
| Grafana | `grafana` | 3000 | Dashboard interface |

All services communicate over `fsec_network` (bridge driver). The shared `entrypoint.sh` script:

1. Waits for PostgreSQL to be ready.
2. Runs Django migrations.
3. Starts `runserver` in development or `gunicorn` in production.

### Service Dependencies

```
PostgreSQL ──────────────────────────┐
                                     ├──► External Bank API
PostgreSQL ──────────────────────────┤
ClickHouse ──────────────────────────┤──► Django Adapter / Celery Worker
Redis ───────────────────────────────┤
External Bank ───────────────────────┘
```

---

## 14. Database Schema

### PostgreSQL — Django Metadata (default schema)

```
Tenant (tenant_id, name, api_key, is_active, loan_types, last_sync_at)
    │
    ├── TenantMembership (user_id FK, tenant_id FK, role, is_default)
    │       └── User (Django built-in)
    │
    ├── SyncState (file_type, loan_type, last_version, last_checksum, last_synced_at)
    │
    └── SyncLog (file_type, loan_type, status,
                 version_before, version_after,
                 records_fetched, records_valid, records_invalid,
                 error_message, completed_at)
            └── ValidationErrorLog (row_number, field_name,
                                    error_type, error_message, raw_value)
```

### PostgreSQL — External Bank (`ext_bank` schema)

```
Loan (
    id, tenant_id, loan_account_number, loan_type,
    customer_id, customer_type,
    loan_status_code, loan_status_flag, days_past_due,
    loan_start_date, final_maturity_date, first_payment_date, loan_closing_date,
    total_installment_count, outstanding_installment_count, paid_installment_count,
    installment_frequency, grace_period_months,
    original_loan_amount, outstanding_principal_balance,
    nominal_interest_rate, total_interest_amount,
    kkdf_rate, kkdf_amount, bsmv_rate, bsmv_amount,
    insurance_included,
    customer_district_code, customer_province_code, customer_region_code,
    internal_rating, external_rating,
    loan_product_type, sector_code,
    internal_credit_rating, default_probability, risk_class, customer_segment,
    created_at
)
  Unique: (tenant_id, loan_account_number, loan_type)

Payment (
    id, tenant_id, payment_id, loan_account_number, loan_type,
    installment_number,
    actual_payment_date, scheduled_payment_date,
    installment_amount, principal_component, interest_component,
    kkdf_component, bsmv_component, installment_status,
    remaining_principal, remaining_interest, remaining_kkdf, remaining_bsmv,
    created_at
)
  Unique: (tenant_id, payment_id, loan_type)

DataVersion (
    id, tenant_id, file_type, loan_type,
    version, record_count, last_updated, checksum
)
  Unique: (tenant_id, file_type, loan_type)

FileUpload (
    id, tenant_id, file_type, loan_type, version,
    filename, file_size, record_count, checksum, uploaded_at
)

ApiKey (
    id, api_key, key_prefix, service_name, description,
    is_active, created_at, last_used_at
)
  Unique: (api_key)
  Index: (service_name)
```

---

## 15. API Endpoints

### Web Pages

| Path | Description |
|---|---|
| `/` | Dashboard |
| `/login/` | Login form |
| `/register/` | User + tenant registration |
| `/upload/` | CSV upload form |
| `/data/` | Data explorer for ClickHouse records |

### Adapter REST API

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/tenants/` | Tenant list (filtered by user access) |
| `GET` | `/api/tenants/{id}/` | Tenant detail |
| `GET` | `/api/tenants/{id}/sync_states/` | All SyncState records for a tenant |
| `GET` | `/api/tenants/{id}/sync_logs/` | Sync history for a tenant |
| `GET` | `/api/sync-logs/` | SyncLog list |
| `POST` | `/api/trigger-sync/` | Manual sync trigger (rate-limited) |
| `GET` | `/api/dashboard-data/` | Aggregated summary and profiling for dashboard |
| `POST` | `/api/upload-csv/` | CSV upload via API (tenant membership required) |
| `GET` | `/api/data-tables/` | Paginated ClickHouse loans/payments records |

### Auth API

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/auth/token/` | Obtain JWT access + refresh tokens |
| `POST` | `/api/auth/token/refresh/` | Refresh JWT access token |
| `GET` | `/api/auth/me/` | Current user profile + tenant info |
| `GET` | `/api/auth/my-tenants/` | List of tenants accessible to the current user |

### External Bank API

All data endpoints require `X-API-KEY` header authentication. Only `/health` and `/metrics` are public.

| Method | Path | Auth | Description |
|---|---|---|---|
| `POST` | `/upload` | 🔐 | CSV upload → PostgreSQL (ext_bank) |
| `GET` | `/data` | 🔐 | JSON data (cursor-based pagination via `after_id`) |
| `GET` | `/version` | 🔐 | Version info for a tenant (version, checksum, record_count) |
| `GET` | `/tenants` | 🔐 | List of registered tenants |
| `GET` | `/health` | — | Health check (public) |
| `GET` | `/metrics` | — | Prometheus metrics (public) |

---

## 16. Error Handling & Resilience

| Scenario | System Behavior | Data State |
|---|---|---|
| External Bank API unreachable | Celery retries 2x, then SyncLog → `failed` | ClickHouse data preserved |
| Error while fetching data (/data) | Sync task logs error, status → `failed` | ClickHouse data preserved |
| Invalid field values in CSV | Validation errors written to `ValidationErrorLog`, sync aborted | ClickHouse data preserved |
| Orphan payments detected | Cross-file check fails → sync fully aborted | ClickHouse data preserved |
| Staging load error | Staging table is dropped | Production table preserved |
| Atomic swap error | Staging cleaned up, error logged | Production table preserved |
| Concurrent sync for same tenant | Concurrency guard: second job does not start | Production table preserved |
| All retries exhausted | Stuck `running` SyncLogs are marked as `failed` | Production table preserved |
| Unauthorized access | HTTP 401 / 403 returned | — |
| Tenant not found (upload) | HTTP 404 returned | — |

**Celery Retry Policy:** `max_retries=2`, `default_retry_delay=30s`. After all retries are exhausted, any `SyncLog` entries stuck in the `running` state are automatically marked as `failed`.

---

## 17. Possible Future Improvements

Although not implemented in the current version, the system design allows for several enhancements.

### IP Whitelisting for External Bank API

The External Bank API authentication module includes a TODO placeholder for IP whitelisting. This would add an additional security layer by restricting API access to specific IP addresses or CIDR ranges (e.g., only allowing requests from the Django Adapter container's IP).

### AI-assisted Normalization

An LLM could be used to normalize unknown categorical values (e.g. `"Kapalı"`, `"Closed"`, `"Paid"`) into canonical representations. This would reduce the need for manually maintained status mapping tables and handle edge cases in multilingual datasets more gracefully.

### AI-based Anomaly Detection

Machine learning models could detect abnormal financial records (e.g. unrealistic interest rates, negative loan amounts, or implausible days-past-due values). These models could run as a post-validation step in the sync pipeline and flag suspicious records for manual review rather than blocking the entire sync.

### AI-driven Data Insights

Profiling results could be summarized automatically using LLMs to provide human-readable insights for analysts. Instead of raw statistics, the dashboard could surface plain-language summaries such as _"RETAIL loan portfolio shows a 12% increase in default rate compared to the previous version"_.

---

> This document describes the current architecture of the FSec platform **(v4 — PostgreSQL-based External Bank + ClickHouse Data Warehouse)**, its security design, and data flow. It should be updated as the system evolves.
