FROM python:3.12-slim

WORKDIR /app

# Install build deps for packages that need compilation
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir .

COPY app/ ./app/
# Migrations ship with the image: the documented deployment runs
# `alembic upgrade` inside the container, which is impossible if the revisions
# and their config stay outside it.
COPY alembic.ini ./
COPY migrations/ ./migrations/

# Data directory for SQLite
RUN mkdir -p /data

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5000/healthz')" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "5000", "--workers", "1"]
