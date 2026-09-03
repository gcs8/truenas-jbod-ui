FROM python:3.12-slim

ARG APP_UID=10001
ARG APP_GID=10001
ARG SOURCE_COMMIT=unknown

LABEL org.opencontainers.image.revision=$SOURCE_COMMIT

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MALLOC_ARENA_MAX=2 \
    APP_CONFIG_PATH=/app/config/config.yaml

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN apt-get update \
    && apt-get install -y --no-install-recommends 7zip \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r /app/requirements.txt

COPY app /app/app
COPY history_service /app/history_service
COPY admin_service /app/admin_service
COPY config/config.example.yaml /app/config/config.example.yaml

RUN groupadd --gid "$APP_GID" app \
    && useradd --uid "$APP_UID" --gid app --no-create-home --shell /usr/sbin/nologin app \
    && mkdir -p /app/config /app/data /app/history /app/logs /run/ssh \
    && chown -R app:app /app /run/ssh

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/livez', timeout=4)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
