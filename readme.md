# FSec — Financial Data Integration Platform

Multi-tenant SaaS platform that integrates with external banking systems, validates & normalizes credit portfolio data, and stores it in ClickHouse for ABS analysis.

## Tech Stack

| Layer | Technology |
|---|---|
| SaaS Platform & Frontend | Django 4.2, Django REST Framework, Tailwind CSS |
| External Bank Simulation | FastAPI, SQLite |
| Metadata DB | PostgreSQL 15 |
| Data Warehouse (OLAP) | ClickHouse |
| Task Queue | Celery + Celery Beat + Redis |
| Auth | JWT, API Key, Session |
| Monitoring | Prometheus, Grafana |
| Infrastructure | Docker, Docker Compose |

## Project Structure

```
├── external_bank/        # Simulated Bank API (FastAPI + SQLite)
├── adapter/              # Business Logic Layer
│   ├── core/             #   Normalizer & Validator
│   ├── warehouse/        #   ClickHouse client & atomic ops
│   ├── tasks/            #   Celery background tasks
│   └── sync_service.py   #   Orchestrator
├── api/                  # Django Application
│   ├── core/             #   Settings, URLs, Celery, WSGI
│   ├── tenants/          #   Models, Views, Auth, Permissions
│   ├── templates/        #   Dashboard, Login, Register, Upload, Data Explorer
│   └── tests/            #   Unit & Integration tests
├── monitoring/           # Prometheus & Grafana configs
├── docker-compose.yml    # Dev orchestration (9 services)
├── docker-compose.prod.yml  # Production overrides
└── Architecture.md       # Detailed architecture doc
```

## Quick Start

### Prerequisites

- **Docker Desktop** (v4+)
- **Git**

### 1. Clone & Configure

```bash
git clone <repository-url>
cd fsec
cp .env.example .env
```

Edit the `.env` file to set your own passwords. Default values work out of the box for local development.

### 2. Start All Services

```bash
docker compose up --build -d
```

This spins up 9 services: External Bank, PostgreSQL, Redis, ClickHouse, Django, Celery Worker, Celery Beat, Prometheus, and Grafana.

### 3. Create Superuser

```bash
docker exec -it django_adapter python manage.py createsuperuser
```

### 4. Access

| Service | URL | Credentials |
|---|---|---|
| **Dashboard** | http://localhost:8000 | Superuser credentials |
| **External Bank API** | http://localhost:8001/docs | — |
| **Grafana** | http://localhost:3000 | `admin` / `admin` |
| **Prometheus** | http://localhost:9090 | — |
| **Django Admin** | http://localhost:8000/admin/ | Superuser credentials |

## Usage

### CSV Upload

1. Navigate to the **Upload CSV** page from the dashboard
2. Select a tenant and loan type (RETAIL / COMMERCIAL)
3. Upload your CSV file (both `,` and `;` delimiters are supported)

### Data Sync

Celery Beat checks the External Bank API every 5 minutes for new data. To trigger a manual sync:

```bash
curl -X POST http://localhost:8000/api/trigger-sync/ \
  -H "Authorization: Bearer <JWT_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"tenant_id": "BANK001"}'
```

### Register New Tenant

Visit http://localhost:8000/register/ to create a new user and tenant. Tenant-user membership is managed via the Django Admin panel.

## Running Tests

All tests run inside the Django container:

```bash
# Run all tests (225 tests)
docker exec django_adapter python -m pytest tests/ -v

# Unit tests only
docker exec django_adapter python -m pytest tests/unit/ -v

# Integration tests only
docker exec django_adapter python -m pytest tests/integration/ -v

# Specific test file
docker exec django_adapter python -m pytest tests/unit/test_normalizer.py -v

# Quick summary
docker exec django_adapter python -m pytest tests/ --tb=short -q
```

### Test Coverage

| Category | Tests |
|---|---|
| **Unit — Normalizer** | Date, interest rate, status normalization + new fields |
| **Unit — Validator** | Required fields, cross-file integrity, range checks |
| **Unit — Authentication** | API Key auth, JWT helpers |
| **Integration — Auth API** | Login, token, refresh, protected endpoints |
| **Integration — Tenant CRUD** | Create, read, isolation |
| **Integration — Registration** | User + tenant creation flow |
| **Integration — CSV Upload** | File upload, tenant check, page permissions |
| **Integration — Sync Pipeline** | Consistency, resilience, atomic replacement |

## Production Deployment

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

Required `.env` overrides for production:

```env
ENVIRONMENT=production
DEBUG=False
DJANGO_SECRET_KEY=<random-64-char-string>
ALLOWED_HOSTS=your-domain.com
```

## Stopping Services

```bash
# Stop all services
docker compose down

# Stop and remove all data (clean start)
docker compose down -v
```
