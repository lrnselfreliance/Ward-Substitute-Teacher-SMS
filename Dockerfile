FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DB_PATH=/data/substitute.db \
    BACKUP_DIR=/data/backups

WORKDIR /srv

COPY pyproject.toml ./
COPY app ./app
COPY cli ./cli

RUN pip install --no-cache-dir . \
    && useradd --uid 10001 --create-home substitute \
    && mkdir -p /data \
    && chown -R substitute:substitute /data /srv

USER substitute
VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/health')"

# One worker, deliberately: a second would run a second scheduler and send
# every batch twice.
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
