# FSec — Architecture Document

## Table of Contents

1. [Overview](#1-overview)
2. [System Architecture](#2-system-architecture)
3. [Service Components](#3-service-components)
4. [Data Flow](#4-data-flow)
5. [Multi-Tenancy](#5-multi-tenancy)
6. [Authentication & Authorization](#6-authentication--authorization)
7. [Validation & Normalization](#7-validation--normalization)
8. [Atomic Replacement](#8-atomic-replacement)
9. [Background Tasks](#9-background-tasks)
10. [Data Warehouse & Profiling](#10-data-warehouse--profiling)
11. [Monitoring](#11-monitoring)
12. [Testing Strategy](#12-testing-strategy)
13. [Infrastructure](#13-infrastructure)
14. [Database Schema](#14-database-schema)
15. [API Endpoints](#15-api-endpoints)
16. [Error Handling & Resilience](#16-error-handling--resilience)

---

## 1. Overview

**FSec** is a multi-tenant SaaS platform that integrates with external banking systems. It extracts credit portfolio data, validates and normalizes it, then stores the processed data in ClickHouse for Asset-Backed Securities (ABS) analysis.

### Core Design Principles

| Principle | Description |
|---|---|
| **Tenant Isolation** | BANK001 can never see BANK002's data |
| **Loan Type Isolation** | RETAIL and COMMERCIAL data coexist independently per tenant |
| **All-or-Nothing** | If validation fails, existing data is preserved |
| **Atomic Swap** | Staging → Production transition with zero downtime, scoped by tenant + loan_type |
| **Periodic Sync** | Automated data update checks via Celery Beat |
| **Streaming I/O** | Large files (200MB+) are streamed to MinIO, never fully loaded into memory |
| **Observability** | Full-stack monitoring with Prometheus + Grafana |

---

## 2. System Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                     Docker Network (fsec_network)                     │
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
│         │      │  Worker    │   │  Beat          │                   │
│         │      └─────┬──────┘   └───────────────┘                   │
│         │            │                                               │
│  ┌──────▼──────┐     │      ┌───────────────┐                       │
│  │   MinIO      │◄────┘     │  PostgreSQL    │                       │
│  │   (S3)       │           │  :5432         │                       │
│  │   :9000/:9001│           └───────────────┘                       │
│  └──────────────┘    ┌──────────────┐                               │
│                      │   Redis       │                               │
│                      │   :6379       │                               │
│                      └──────────────┘                               │
│  ┌──────────────┐    ┌──────────────┐                               │
│  │  Prometheus   │    │  Grafana      │                              │
│  │  :9090        │    │  :3000        │                              │
│  └──────────────┘    └──────────────┘                               │
└──────────────────────────────────────────────────────────────────────┘
```

**Key data path:** CSV uploads are streamed to **MinIO** via the External Bank API. The Adapter's sync service downloads CSVs from MinIO using **presigned URLs**, validates, normalizes, and loads them into **ClickHouse**.

---

## 3. Service Components

### 3.1 External Bank API (FastAPI + PostgreSQL + MinIO)

Simulates an external banking system. Uses a shared **PostgreSQL** database (multi-tenant via `tenant_id` column, `ext_bank` schema) for metadata and record storage, and **MinIO** (S3-compatible) for durable CSV file storage.

| Method | Path | Description |
|---|---|---|
| `POST` | `/upload` | Upload CSV file → MinIO + PostgreSQL |
| `GET` | `/data` | Return stored data as JSON (cursor-based pagination) |
| `GET` | `/version` | Data version info (includes MinIO object keys) |
| `GET` | `/files/download` | Get presigned MinIO URL for a specific version |
| `GET` | `/tenants` | List existing tenants |
| `GET` | `/health` | Service health check |

**Key design:** Full replacement strategy — new data completely replaces existing data for the same `tenant_id` + `loan_type` combination. Each upload is tracked with a version number, SHA-256 checksum, and MinIO object key. CSV files are organized in MinIO as `{tenant_id}/{file_type}/{loan_type}/v{version}_{filename}`.

**Upload flow:**
1. Stream upload to temp file on disk (8MB chunks)
2. Upload temp file to MinIO (S3)
3. Delete existing PostgreSQL records for tenant+loan_type
4. Parse CSV and bulk-insert into PostgreSQL (`bulk_insert_mappings`, 50K batch size)
5. Update `DataVersion` with MinIO object key and metadata
6. Record upload history in `FileUpload` table
7. Clean up temp file

### 3.2 Django Adapter API

Main SaaS platform providing tenant management, sync orchestration, web UI, and REST API. Communicates with the External Bank API to forward CSV uploads and with ClickHouse for data warehousing.

### 3.3 Adapter Business Logic Layer

```
┌────────────────────────────────────────┐
│  Celery Tasks (sync_tasks.py)          │  ← Scheduling, triggers & concurrency guard
├────────────────────────────────────────┤
│  SyncService (sync_service.py)         │  ← Pipeline orchestration (MinIO download)
├────────────────────────────────────────┤
│  Validator          │  Normalizer      │  ← Business rules
├────────────────────────────────────────┤
│  ClickHouseClient                      │  ← Data warehouse ops (loan_type-aware swap)
└────────────────────────────────────────┘
```

---

## 4. Data Flow

### Sync Pipeline (v3 — MinIO-based)

```
Celery Beat (every 5 min) → check_for_updates() → version comparison
    │
    ▼ (if new data detected)
run_sync_for_tenant() (async Celery task, with concurrency guard)
    │
    ├── Step 1: Get presigned URL from External Bank /files/download
    ├── Step 2: Download CSV from MinIO via presigned URL (streaming)
    │     └── Fallback: cursor-based pagination via /data API
    ├── Step 3: Validate all records (field-level + cross-file)
    │     ├── ✅ Pass → Step 4: Normalize
    │     └── ❌ Fail → Log errors, PRESERVE existing data
    ├── Step 4: Normalize (dates, rates, statuses) in 100K-record batches
    ├── Step 5: Load normalized batches to ClickHouse staging table
    ├── Step 6: Atomic swap (staging → production) with tenant+loan_type isolation
    └── Step 7: Update SyncState + compute profiling
```

### CSV Upload Flow

```
User/API → Django (upload_csv) → Tenant existence check
    ├── Tenant exists → Forward file to External Bank /upload
    │     └── External Bank → Stream to temp file → MinIO + PostgreSQL
    └── Tenant not found → HTTP 404

Large files (200MB+): Dynamic timeout (5s/MB, min 600s), streaming transfer
```

---

## 5. Multi-Tenancy

### Isolation Layers

| Layer | Mechanism |
|---|---|
| **External Bank (PostgreSQL)** | Logical — shared `ext_bank` schema, `tenant_id` column + filtered queries |
| **External Bank (MinIO)** | Path-based — `{tenant_id}/{file_type}/{loan_type}/` object key prefix |
| **ClickHouse** | Partition-based — `PARTITION BY tenant_id`, DELETE scoped by `tenant_id + loan_type` |
| **PostgreSQL (Django)** | Logical — `TenantMembership` model, FK filtering |
| **API** | Query filtering — `get_queryset()`, `IsTenantMember` permission |

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

Three authentication mechanisms are supported simultaneously:

| Method | Use Case | Header / Mechanism |
|---|---|---|
| **JWT** | Programmatic API access | `Authorization: Bearer <token>` |
| **API Key** | Machine-to-machine (M2M) | `X-API-Key: <tenant-api-key>` |
| **Session** | Web UI (dashboard, forms) | Django session cookie + CSRF |

**JWT Config:** Access token = 2 hours, Refresh token = 7 days. Old tokens are blacklisted after rotation.

**API Key:** Directly maps to a `Tenant`. Can only access its own tenant's data.

---

## 7. Validation & Normalization

### Validation Rules

**Field-Level:**

| Field | Rule | Error Type |
|---|---|---|
| `loan_id` / `payment_id` | Required, unique | `missing_required`, `duplicate` |
| `amount` | Numeric, 0 < x ≤ 100B | `invalid_type`, `range_violation` |
| `interest_rate` | Parseable, 0.0 ≤ x ≤ 1.0 | `invalid_format`, `range_violation` |
| `start_date` / `payment_date` | Parseable date | `invalid_format` |
| `status` | Recognized label | `invalid_value` |

**Cross-File Integrity:** Every payment's `loan_id` must exist in the loan dataset. Orphan payments cause the entire sync to abort.

### Normalization Rules

**Dates:** `DD/MM/YYYY`, `MM/DD/YYYY`, `YYYYMMDD`, `DD-MM-YYYY`, `DD.MM.YYYY` → `YYYY-MM-DD`

**Interest Rates:** `18.5%` → `0.185` · `1850 bps` → `0.185` · `18.5` (>1) → `0.185`

**Status:** `Active/Open/Aktif` → `ACTIVE` · `Paid/Closed/Kapalı` → `CLOSED` · `Default/Gecikme` → `DEFAULT` · `Restructured/Yapılandırılmış` → `RESTRUCTURED`

---

## 8. Atomic Replacement

ClickHouse data updates use a staging table mechanism with **tenant + loan_type isolation**:

```
1. CREATE TABLE loans_staging AS loans
2. INSERT normalized data INTO loans_staging
3. ALTER TABLE loans DELETE WHERE tenant_id = 'BANK001' AND loan_type = 'RETAIL'
4. Wait for ClickHouse mutation to complete (async DELETE)
5. INSERT INTO loans SELECT * FROM loans_staging WHERE tenant_id = 'BANK001'
6. DROP TABLE loans_staging
```

**Critical:** The DELETE in Step 3 filters by **both** `tenant_id` AND `loan_type`. This ensures that uploading COMMERCIAL data does not delete RETAIL data for the same tenant, and vice versa.

**Safety guarantees:**

| Scenario | Behavior |
|---|---|
| Staging load fails | Staging table is dropped, production unchanged |
| Swap fails | Staging cleaned up, error logged |
| Validation fails | Staging table is never created |
| Other tenants' data | Isolated by `WHERE tenant_id`, unaffected |
| Other loan_types' data | Isolated by `WHERE loan_type`, unaffected |

**Full Replacement:** 1000 records + 2000 new records = 2000 records (not 3000).

**Loan Type Coexistence:** Uploading RETAIL data, then COMMERCIAL data = both exist independently and can be queried separately.

---

## 9. Background Tasks

```
Celery Beat (scheduler)
├── Every 5 minutes: check_for_updates()
│   → Compare versions for all active tenants
│   → Skip tenants with already-running syncs (concurrency guard)
│   → If new data: dispatch run_sync_for_tenant.delay(tenant_id)
│
Celery Worker (executor)
├── run_sync_for_tenant() → SyncService.sync_tenant()
│   → Concurrency guard (prevents duplicate tasks per tenant)
│   → Downloads CSV from MinIO via presigned URL
│   → Validates, normalizes (100K batches), loads to ClickHouse
│   → Performs tenant+loan_type-scoped atomic swap
└── On failure: 2 retries with 30s delay, then marks SyncLogs as failed
```

**Concurrency Guard:** Only one sync task per tenant can run at a time. The `check_for_updates` task skips tenants with active `running` SyncLog entries, and `run_sync_for_tenant` checks for concurrent runs before proceeding.

**Version Check Logic:** Compares both version number and SHA-256 checksum from External Bank API against local `SyncState`. Triggers sync if either has changed.

---

## 10. Data Warehouse & Profiling

### ClickHouse Tables

**loans:** Full Turkish banking credit portfolio fields — `tenant_id, loan_id, loan_type, amount, outstanding_principal_balance, interest_rate, original_interest_rate, total_interest_amount, kkdf_rate, kkdf_amount, bsmv_rate, bsmv_amount, start_date, final_maturity_date, first_payment_date, loan_closing_date, original_start_date, status, original_status, loan_status_flag, days_past_due, customer_id, customer_type, total_installment_count, outstanding_installment_count, paid_installment_count, installment_frequency, grace_period_months, insurance_included, customer_district_code, customer_province_code, customer_region_code, internal_rating, external_rating, loan_product_type, sector_code, internal_credit_rating, default_probability, risk_class, customer_segment, synced_at`

**payments:** `tenant_id, payment_id, loan_id, loan_type, installment_number, payment_date, actual_payment_date, scheduled_payment_date, original_payment_date, payment_amount, principal_component, interest_component, kkdf_component, bsmv_component, installment_status, remaining_principal, remaining_interest, remaining_kkdf, remaining_bsmv, synced_at`

Both tables use `MergeTree()` engine with `PARTITION BY tenant_id` and `ORDER BY (tenant_id, loan_type, loan_id/payment_id)` for tenant isolation and fast partition-level operations.

### Profiling Metrics

| Category | Metrics |
|---|---|
| **Numeric** | Min, Max, Avg, StdDev (amount, interest_rate, days_past_due, payment_amount) |
| **Categorical** | Unique count, Mode, Distribution (status, loan_type, customer_type, installment_status) |
| **Quality** | Null date ratio, Zero rate ratio, Unknown status ratio |

Profiling data is visualized on the dashboard with Chart.js charts.

---

## 11. Monitoring

| Service | Library | Metrics |
|---|---|---|
| **Django** | `django-prometheus` | HTTP request count/duration, DB queries |
| **FastAPI** | `prometheus-fastapi-instrumentator` | HTTP request count/duration, response size, upload latency |
| **ClickHouse** | Built-in exporter (:9363) | Query count, memory usage |

Prometheus scrapes all services every 15 seconds. Grafana dashboard (`fsec-overview.json`) provides panels for:
- Service health and request rates
- Response times (P50/P90/P99)
- Error rates
- Upload endpoint latency (granular buckets: 0.1s to 120s)
- Upload file size
- Upload success/error rate
- ClickHouse metrics

---

## 12. Testing Strategy

```
tests/
├── conftest.py                    # Shared fixtures
├── unit/                          # Unit Tests
│   ├── test_normalizer.py         # Date, rate, status normalization
│   ├── test_validator.py          # Field validation, cross-file integrity
│   └── test_authentication.py     # Auth backends, permissions
└── integration/                   # Integration Tests
    ├── test_api_auth.py           # JWT, session, me/my-tenants
    ├── test_api_tenants.py        # CRUD, sync logs, dashboard data
    ├── test_tenant_isolation.py   # 🔒 Tenant leakage tests (CRITICAL)
    ├── test_registration.py       # Registration flow
    ├── test_upload_csv.py         # CSV upload (web UI + API)
    └── test_sync_pipeline.py      # Pipeline consistency & resilience
```

### Critical Test Scenarios

- **Tenant Isolation:** BANK001 user must NEVER access BANK002 data. Tested across all endpoints with JWT, API Key, and Superuser scenarios.
- **Loan Type Isolation:** Uploading RETAIL data then COMMERCIAL data — both must coexist. Uploading COMMERCIAL must NOT delete RETAIL (and vice versa).
- **Consistency:** 1000 records + 2000 records = exactly 2000 records after sync (full replacement, not append).
- **Resilience:** Invalid data or orphan payments must preserve existing production data.

---

## 13. Infrastructure

### Docker Compose Services (10 total)

| Service | Container | Ports | Description |
|---|---|---|---|
| MinIO (S3) | `minio_s3` | 9002 → 9000, 9001 | S3-compatible object storage for CSV files |
| External Bank | `external_bank_api` | 8001 → 8000 | FastAPI + PostgreSQL + MinIO |
| PostgreSQL | `postgres_db` | 5432 | Shared by Django and External Bank |
| Redis | `redis_broker` | 6379 | Celery broker/backend |
| ClickHouse | `clickhouse_dw` | 8123, 9000, 9363 | OLAP data warehouse |
| Django Adapter | `django_adapter` | 8000 | SaaS platform API + web UI |
| Celery Worker | `celery_worker` | — | Background sync tasks (4 workers) |
| Celery Beat | `celery_beat` | — | Periodic task scheduler |
| Prometheus | `prometheus` | 9090 | Metrics collection |
| Grafana | `grafana` | 3000 | Dashboards & visualization |

All services communicate over `fsec_network` (bridge driver). A shared `entrypoint.sh` handles PostgreSQL readiness check, Django migrations, and server startup (Gunicorn in production, runserver in development).

### Service Dependencies

```
MinIO ─────────────┐
PostgreSQL ────────┼──► External Bank API
                   │
MinIO ─────────────┤
PostgreSQL ────────┤
ClickHouse ────────┼──► Django Adapter / Celery Worker
Redis ─────────────┤
External Bank ─────┘
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
    ├── SyncState (file_type, loan_type, last_version, last_checksum)
    │
    └── SyncLog (file_type, loan_type, status, records_fetched/valid/invalid, error_message)
            └── ValidationErrorLog (row_number, field_name, error_type, raw_value)
```

### PostgreSQL — External Bank (ext_bank schema)

```
Loan (id, tenant_id, loan_account_number, loan_type, customer_id, customer_type,
      loan_status_code, loan_status_flag, days_past_due,
      loan_start_date, final_maturity_date, first_payment_date, loan_closing_date,
      total_installment_count, outstanding_installment_count, paid_installment_count,
      installment_frequency, grace_period_months,
      original_loan_amount, outstanding_principal_balance,
      nominal_interest_rate, total_interest_amount, kkdf_rate, kkdf_amount, bsmv_rate, bsmv_amount,
      insurance_included, customer_district_code, customer_province_code, customer_region_code,
      internal_rating, external_rating, loan_product_type, sector_code,
      internal_credit_rating, default_probability, risk_class, customer_segment,
      created_at)
    └── Unique: (tenant_id, loan_account_number, loan_type)

Payment (id, tenant_id, payment_id, loan_account_number, loan_type, installment_number,
         actual_payment_date, scheduled_payment_date,
         installment_amount, principal_component, interest_component,
         kkdf_component, bsmv_component, installment_status,
         remaining_principal, remaining_interest, remaining_kkdf, remaining_bsmv,
         created_at)
    └── Unique: (tenant_id, payment_id, loan_type)

DataVersion (id, tenant_id, file_type, loan_type, version, record_count,
             last_updated, checksum, minio_object_key)
    └── Unique: (tenant_id, file_type, loan_type)

FileUpload (id, tenant_id, file_type, loan_type, version, filename,
            minio_object_key, file_size, record_count, checksum, uploaded_at)
```

### MinIO (S3 Object Storage)

```
fsec-data/                          # Bucket
├── BANK001/
│   ├── loans/
│   │   ├── RETAIL/
│   │   │   └── v2_retail_credit.csv
│   │   └── COMMERCIAL/
│   │       └── v1_commercial_credit.csv
│   └── payments/
│       ├── RETAIL/
│       │   └── v1_retail_payments.csv
│       └── COMMERCIAL/
│           └── v1_commercial_payments.csv
├── BANK002/
│   └── ...
└── BANK003/
    └── ...
```

---

## 15. API Endpoints

### Web Pages

| Path | Description |
|---|---|
| `/` | Dashboard |
| `/login/` | Login |
| `/register/` | User + tenant registration |
| `/upload/` | CSV upload |
| `/data/` | Data Explorer |

### REST API

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/tenants/` | List tenants |
| `GET` | `/api/tenants/{id}/` | Tenant detail |
| `GET` | `/api/sync-logs/` | Sync logs |
| `POST` | `/api/trigger-sync/` | Manual sync trigger |
| `GET` | `/api/dashboard-data/` | Dashboard profiling data |
| `POST` | `/api/upload-csv/` | CSV upload (API) |
| `GET` | `/api/data-tables/` | Paginated ClickHouse data |

### Auth API

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/auth/token/` | Obtain JWT (username + password) |
| `POST` | `/api/auth/token/refresh/` | Refresh JWT |
| `GET` | `/api/auth/me/` | Current user info |
| `GET` | `/api/auth/my-tenants/` | User's tenant memberships |

### External Bank API

| Method | Path | Description |
|---|---|---|
| `POST` | `/upload` | Upload CSV → MinIO + PostgreSQL |
| `GET` | `/data` | Query records (cursor-based pagination via `after_id`) |
| `GET` | `/version` | Version info (includes `minio_object_key`) |
| `GET` | `/files/download` | Get presigned MinIO download URL |
| `GET` | `/tenants` | List tenants |
| `GET` | `/health` | Health check |

---

## 16. Error Handling & Resilience

| Scenario | System Behavior | Data State |
|---|---|---|
| External Bank API unreachable | Celery retries 2x, SyncLog → `failed` | Preserved |
| MinIO download fails | Falls back to /data API (cursor pagination) | Preserved |
| Invalid field values in CSV | Entire batch rejected, logged to `ValidationErrorLog` | Preserved |
| Orphan payments detected | Cross-file check fails, sync aborted | Preserved |
| ClickHouse staging load error | Staging table dropped | Preserved |
| ClickHouse swap error | Staging cleaned up, error logged | Preserved |
| Concurrent sync for same tenant | Second task skipped (concurrency guard) | Preserved |
| All retries exhausted | Stuck SyncLogs marked as `failed` automatically | Preserved |
| Tenant not found (CSV upload) | HTTP 404 returned | — |
| Unauthorized access | HTTP 401/403 returned | — |

**Celery Retry Policy:** `max_retries=2`, `default_retry_delay=30s`. After all retries are exhausted, any stuck `SyncLog` entries in "running" state are automatically marked as "failed".

---

> This document covers the current architecture (v3 — PostgreSQL + MinIO), design decisions, and implementation details of the FSec platform. It should be updated as the project evolves.
