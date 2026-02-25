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
