#!/bin/sh
# =============================================================================
# FSec Django Entrypoint
# Handles: PostgreSQL readiness, migrations, static files, server start
# =============================================================================

set -e

echo "============================================="
echo "  FSec Adapter - Starting..."
echo "  Environment: ${ENVIRONMENT:-development}"
echo "============================================="

# --- Wait for PostgreSQL ---
echo "Waiting for PostgreSQL at postgres_db:5432..."

MAX_RETRIES=30
RETRY_COUNT=0

while [ $RETRY_COUNT -lt $MAX_RETRIES ]; do
    python -c "import socket; s=socket.create_connection(('postgres_db', 5432), timeout=2); s.close(); print('Connected!')" 2>/dev/null
    if [ $? -eq 0 ]; then
        echo "PostgreSQL is ready!"
        break
    fi
    RETRY_COUNT=$((RETRY_COUNT + 1))
    echo "PostgreSQL not ready (attempt $RETRY_COUNT/$MAX_RETRIES) - waiting 2s..."
    sleep 2
done

if [ $RETRY_COUNT -eq $MAX_RETRIES ]; then
    echo "WARNING: Could not connect to PostgreSQL after $MAX_RETRIES attempts. Proceeding anyway..."
fi

# --- Run Migrations ---
echo "Running migrations..."
python manage.py migrate --noinput 2>&1 || echo "Migration warning (may be OK on first run)"

# --- Collect Static Files (production only) ---
if [ "${ENVIRONMENT}" = "production" ]; then
    echo "Collecting static files..."
    python manage.py collectstatic --noinput 2>&1 || echo "Static files warning"
fi

# --- Seed Data: Create default users and tenants ---
echo "Seeding default data..."
python manage.py shell -c "
from django.contrib.auth.models import User
from tenants.models import Tenant, TenantMembership

# --- user1 / BANK001 ---
if not User.objects.filter(username='user1').exists():
    user1 = User.objects.create_user(username='user1', email='user1@bank001.com', password='fsec1user123!')
    print('Created user: user1')
else:
    user1 = User.objects.get(username='user1')
    print('User user1 already exists')

if not Tenant.objects.filter(tenant_id='BANK001').exists():
    bank001 = Tenant.objects.create(tenant_id='BANK001', name='Bank 001', is_active=True, loan_types=['RETAIL', 'COMMERCIAL'])
    print('Created tenant: BANK001')
else:
    bank001 = Tenant.objects.get(tenant_id='BANK001')
    print('Tenant BANK001 already exists')

if not TenantMembership.objects.filter(user=user1, tenant=bank001).exists():
    TenantMembership.objects.create(user=user1, tenant=bank001, role='admin', is_default=True)
    print('Created membership: user1 -> BANK001 (admin)')
else:
    print('Membership user1 -> BANK001 already exists')

# --- user2 / BANK002 ---
if not User.objects.filter(username='user2').exists():
    user2 = User.objects.create_user(username='user2', email='user2@bank002.com', password='fsec2user123!')
    print('Created user: user2')
else:
    user2 = User.objects.get(username='user2')
    print('User user2 already exists')

if not Tenant.objects.filter(tenant_id='BANK002').exists():
    bank002 = Tenant.objects.create(tenant_id='BANK002', name='Bank 002', is_active=True, loan_types=['RETAIL', 'COMMERCIAL'])
    print('Created tenant: BANK002')
else:
    bank002 = Tenant.objects.get(tenant_id='BANK002')
    print('Tenant BANK002 already exists')

if not TenantMembership.objects.filter(user=user2, tenant=bank002).exists():
    TenantMembership.objects.create(user=user2, tenant=bank002, role='admin', is_default=True)
    print('Created membership: user2 -> BANK002 (admin)')
else:
    print('Membership user2 -> BANK002 already exists')

# --- user3 / BANK003 ---
if not User.objects.filter(username='user3').exists():
    user3 = User.objects.create_user(username='user3', email='user3@bank003.com', password='fsec3user123!')
    print('Created user: user3')
else:
    user3 = User.objects.get(username='user3')
    print('User user3 already exists')

if not Tenant.objects.filter(tenant_id='BANK003').exists():
    bank003 = Tenant.objects.create(tenant_id='BANK003', name='Bank 003', is_active=True, loan_types=['RETAIL', 'COMMERCIAL'])
    print('Created tenant: BANK003')
else:
    bank003 = Tenant.objects.get(tenant_id='BANK003')
    print('Tenant BANK003 already exists')

if not TenantMembership.objects.filter(user=user3, tenant=bank003).exists():
    TenantMembership.objects.create(user=user3, tenant=bank003, role='admin', is_default=True)
    print('Created membership: user3 -> BANK003 (admin)')
else:
    print('Membership user3 -> BANK003 already exists')
" 2>&1 || echo "Seed data warning (may be OK on first run)"

# --- Start Server ---
echo "Starting: $@"

# If no command is provided (default CMD), decide server based on environment
if [ "$1" = "python" ] && [ "$2" = "manage.py" ] && [ "$3" = "runserver" ]; then
    if [ "${ENVIRONMENT}" = "production" ]; then
        echo "Production mode: starting Gunicorn..."
        exec gunicorn core.wsgi:application \
            --bind 0.0.0.0:8000 \
            --workers ${GUNICORN_WORKERS:-3} \
            --threads ${GUNICORN_THREADS:-2} \
            --timeout 120 \
            --access-logfile - \
            --error-logfile - \
            --log-level info
    else
        echo "Development mode: starting Django runserver..."
        exec python manage.py runserver 0.0.0.0:8000
    fi
else
    exec "$@"
fi
