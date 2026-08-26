FROM python:3.12-slim AS base

# Pillow needs the runtime image libraries; the build tools are not kept.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libwebp7 libjpeg62-turbo zlib1g postgresql-client curl \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

WORKDIR /srv

# Dependencies first, so a code change does not reinstall them.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt gunicorn==23.0.0

COPY . .

# Not root: this process accepts uploads from other people's machines.
RUN useradd --system --create-home --uid 10001 ttcloud \
    && chown -R ttcloud:ttcloud /srv
USER ttcloud

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/healthz || exit 1

CMD ["gunicorn", "-c", "gunicorn.conf.py", "app:create_app()"]
