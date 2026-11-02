# LiqGuard: Telegram bot (long polling) + web dashboard in one process.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY liqguard ./liqguard
RUN pip install . \
    && useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin liqguard \
    && mkdir -p /data \
    && chown liqguard:liqguard /data \
    && chmod 700 /data

# All state (SQLite DB, master key generated on first boot, audit log) lives in /data.
ENV LIQGUARD_DATA_DIR=/data \
    LIQGUARD_DASHBOARD=true \
    LIQGUARD_DASHBOARD_HOST=0.0.0.0 \
    PORT=8765

USER liqguard
VOLUME ["/data"]
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT', '8765'), timeout=4)" || exit 1

CMD ["liqguard"]
