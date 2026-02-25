FROM python:3.11-slim

WORKDIR /app

# Install system dependencies for psycopg2
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev curl && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Create static directory
RUN mkdir -p /app/staticfiles /app/static

# Make entrypoint executable
RUN chmod +x /app/entrypoint.sh

# Non-root user for production
RUN adduser --disabled-password --gecos '' appuser && \
    chown -R appuser:appuser /app
# Note: We don't switch to appuser here because entrypoint needs write access
# In production, the CMD runs as appuser via gunicorn's --user flag

EXPOSE 8000

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]
