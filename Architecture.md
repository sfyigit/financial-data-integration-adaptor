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
| **All-or-Nothing** | If validation fails, existing data is preserved |
| **Atomic Swap** | Staging → Production transition with zero downtime |
| **Periodic Sync** | Automated data update checks via Celery Beat |
| **Observability** | Full-stack monitoring with Prometheus + Grafana |

---

## 2. System Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                     Docker Network (fsec_network)                │
│                                                                  │
│  ┌──────────────┐    ┌──────────────┐    ┌───────────────────┐  │
│  │  External     │    │  Django       │    │  ClickHouse       │  │
│  │  Bank API     │◄───│  Adapter API  │───►│  Data Warehouse   │  │
│  │  (FastAPI)    │    │  (DRF)        │    │  (OLAP)           │  │
│  │  :8001        │    │  :8000        │    │  :8123/:9000      │  │
│  └──────────────┘    └──────┬───────┘    └───────────────────┘  │
│                             │                                    │
│                    ┌────────┴────────┐                           │
│              ┌─────▼─────┐   ┌──────▼──────┐                    │
│              │  Celery    │   │  Celery      │                   │
│              │  Worker    │   │  Beat         │                  │
│              └─────┬──────┘   └──────────────┘                  │
│              ┌─────▼──────┐   ┌───────────────┐                 │
│              │   Redis     │   │  PostgreSQL    │                │
│              │   :6379     │   │  :5432         │                │
│              └─────────────┘   └───────────────┘                │
│  ┌──────────────┐    ┌──────────────┐                           │
│  │  Prometheus   │    │  Grafana      │                          │
│  │  :9090        │    │  :3000        │                          │
│  └──────────────┘    └──────────────┘                           │
└──────────────────────────────────────────────────────────────────┘
```

---

## 3. Service Components

### 3.1 External Bank API (FastAPI)

Simulates an external banking system with per-tenant SQLite databases.

| Method | Path | Description |
|---|---|---|
| `POST` | `/upload` | Upload CSV file (loans/payments) |
| `GET` | `/data` | Return stored data as JSON |
| `GET` | `/version` | Data version info (for sync checks) |
| `GET` | `/tenants` | List existing tenants |

**Key design:** Full replacement strategy — new data completely replaces existing data for the same `loan_type`. Each upload is tracked with a version number and SHA-256 checksum.

### 3.2 Django Adapter API

Main SaaS platform providing tenant management, sync orchestration, web UI, and REST API.

### 3.3 Adapter Business Logic Layer

```
┌────────────────────────────────┐
│  Celery Tasks (sync_tasks.py)  │  ← Scheduling & triggers
├────────────────────────────────┤
│  SyncService (sync_service.py) │  ← Pipeline orchestration
├────────────────────────────────┤
│  Validator    │  Normalizer    │  ← Business rules
├────────────────────────────────┤
│  ClickHouseClient              │  ← Data warehouse operations
└────────────────────────────────┘
```

---

## 4. Data Flow

### Sync Pipeline

```
Celery Beat (every 5 min) → check_for_updates() → version comparison
    │
    ▼ (if new data detected)
run_sync_for_tenant() (async Celery task)
    │
    ├── Step 1: Fetch data from External Bank API
    ├── Step 2: Validate all records (field-level + cross-file)
    │     ├── ✅ Pass → Step 3: Normalize
    │     └── ❌ Fail → Log errors, PRESERVE existing data
    ├── Step 3: Normalize (dates, rates, statuses)
    ├── Step 4: Load to ClickHouse staging table
    ├── Step 5: Atomic swap (staging → production)
    └── Step 6: Update SyncState + compute profiling
```

### CSV Upload Flow

```
User/API → Django (upload_csv) → Tenant existence check
    ├── Tenant exists → Forward to External Bank → Return response
    └── Tenant not found → HTTP 404
```

---

## 5. Multi-Tenancy

### Isolation Layers

| Layer | Mechanism |
|---|---|
| **External Bank** | Physical isolation — separate SQLite file per tenant |
| **ClickHouse** | Partition-based — `PARTITION BY tenant_id` |
| **PostgreSQL** | Logical — `TenantMembership` model, FK filtering |
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

ClickHouse data updates use a staging table mechanism:

```
1. CREATE TABLE loans_staging AS loans
2. INSERT normalized data INTO loans_staging
3. ALTER TABLE loans DELETE WHERE tenant_id = 'BANK001'
4. INSERT INTO loans SELECT * FROM loans_staging WHERE tenant_id = 'BANK001'
5. DROP TABLE loans_staging
```

**Safety guarantees:**

| Scenario | Behavior |
|---|---|
| Staging load fails | Staging table is dropped, production unchanged |
| Swap fails | Staging cleaned up, error logged |
| Validation fails | Staging table is never created |
| Other tenants' data | Isolated by `WHERE tenant_id`, unaffected |

**Full Replacement:** 1000 records + 2000 new records = 2000 records (not 3000).

---

## 9. Background Tasks

```
Celery Beat (scheduler)
├── Every 5 minutes: check_for_updates()
│   → Compare versions for all active tenants
│   → If new data: dispatch run_sync_for_tenant.delay(tenant_id)
│
Celery Worker (executor)
├── run_sync_for_tenant() → SyncService.sync_tenant()
│   → Runs the full pipeline
└── On failure: 3 retries with 60s delay
```

**Version Check Logic:** Compares both version number and SHA-256 checksum from External Bank API against local `SyncState`. Triggers sync if either has changed.

---

## 10. Data Warehouse & Profiling

### ClickHouse Tables

**loans:** `tenant_id, loan_id, customer_id, customer_name, customer_segment, loan_type, amount, interest_rate, start_date, end_date, loan_term, status, original_*, synced_at`

**payments:** `tenant_id, payment_id, loan_id, loan_type, payment_amount, payment_date, due_date, payment_status, payment_channel, transaction_id, transaction_date, transaction_amount, fee_amount, penalty_amount, total_paid, original_*, synced_at`

Both tables use `MergeTree()` engine with `PARTITION BY tenant_id` for tenant isolation and fast partition-level deletes. Original values are preserved in `original_*` columns for audit purposes.

### Profiling Metrics

| Category | Metrics |
|---|---|
| **Numeric** | Min, Max, Avg, StdDev |
| **Categorical** | Unique count, Mode, Distribution |
| **Quality** | Null ratio, Zero ratio, Unknown status ratio |

Profiling data is visualized on the dashboard with Chart.js charts.

---

## 11. Monitoring

| Service | Library | Metrics |
|---|---|---|
| **Django** | `django-prometheus` | HTTP request count/duration, DB queries |
| **FastAPI** | `prometheus-fastapi-instrumentator` | HTTP request count/duration, response size |
| **ClickHouse** | Built-in exporter (:9363) | Query count, memory usage |

Prometheus scrapes all services every 15 seconds. Grafana dashboard (`fsec-overview.json`) provides panels for service health, request rates, response times (P50/P90/P99), error rates, and ClickHouse metrics.

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

- **Tenant Leakage:** BANK001 user must NEVER access BANK002 data. Tested across all endpoints with JWT, API Key, and Superuser scenarios.
- **Consistency:** 1000 records + 2000 records = exactly 2000 records after sync.
- **Resilience:** Invalid data or orphan payments must preserve existing production data.

---

## 13. Infrastructure

### Docker Compose Services (9 total)

| Service | Container | Ports |
|---|---|---|
| External Bank | `external_bank_api` | 8001 → 8000 |
| PostgreSQL | `postgres_db` | 5432 |
| Redis | `redis_broker` | 6379 |
| ClickHouse | `clickhouse_dw` | 8123, 9000, 9363 |
| Django Adapter | `django_adapter` | 8000 |
| Celery Worker | `celery_worker` | — |
| Celery Beat | `celery_beat` | — |
| Prometheus | `prometheus` | 9090 |
| Grafana | `grafana` | 3000 |

All services communicate over `fsec_network` (bridge driver). A shared `entrypoint.sh` handles PostgreSQL readiness check, Django migrations, and server startup (Gunicorn in production, runserver in development).

---

## 14. Database Schema

### PostgreSQL (Metadata)

```
Tenant (tenant_id, name, api_key, is_active, loan_types, last_sync_at)
    │
    ├── TenantMembership (user_id FK, tenant_id FK, role, is_default)
    │       └── User (Django built-in)
    │
    ├── SyncState (file_type, loan_type, last_version, last_checksum)
    │
    └── SyncLog (file_type, status, records_fetched/valid/invalid, error_message)
            └── ValidationErrorLog (row_number, field_name, error_type, raw_value)
```

### External Bank SQLite (per tenant)

`loans` (loan_id, loan_type, customer_*, amount, interest_rate, start_date, end_date, loan_term, status)
`payments` (payment_id, loan_id, loan_type, payment_amount, payment_date, due_date, payment_status, payment_channel, transaction_*, fee_amount, penalty_amount, total_paid)
`data_versions` (file_type, loan_type, version, record_count, checksum)

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

---

## 16. Error Handling & Resilience

| Scenario | System Behavior | Data State |
|---|---|---|
| External Bank API unreachable | Celery retries 3x, SyncLog → `failed` | Preserved |
| Invalid field values in CSV | Entire batch rejected, logged to `ValidationErrorLog` | Preserved |
| Orphan payments detected | Cross-file check fails, sync aborted | Preserved |
| ClickHouse staging load error | Staging table dropped | Preserved |
| ClickHouse swap error | Staging cleaned up, error logged | Preserved |
| Tenant not found (CSV upload) | HTTP 404 returned | — |
| Unauthorized access | HTTP 401/403 returned | — |

**Celery Retry Policy:** `max_retries=3`, `default_retry_delay=60s`

---

> This document covers the current architecture, design decisions, and implementation details of the FSec platform. It should be updated as the project evolves.
