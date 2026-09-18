FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app

# curl is the healthcheck's only runtime need; installed before the app layers so
# it stays cached across code changes.
RUN apt-get update \
    && apt-get install --no-install-recommends -y curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Run as an unprivileged user: a process that never needs to write outside its
# upload directory should not be able to. `uploads` is still chowned because the
# local storage backend writes there when object storage is not configured.
RUN useradd --create-home --uid 10001 enterai \
    && mkdir -p /app/uploads \
    && chown -R enterai:enterai /app/uploads
USER enterai

EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

# Migrations run from the image's entrypoint for a single-node deployment. With
# more than one replica, run `alembic upgrade head` once as a release step instead
# (see docker-compose.prod.yml) so replicas do not race each other to migrate.
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
