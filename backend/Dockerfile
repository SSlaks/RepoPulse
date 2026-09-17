FROM python:3.12-slim

ARG VCS_REF=local

LABEL org.opencontainers.image.revision=$VCS_REF

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/backend:/app

WORKDIR /app
COPY backend ./backend
COPY worker ./worker
RUN pip install --no-cache-dir ./backend \
    && groupadd --system --gid 10001 repopulse \
    && useradd --system --uid 10001 --gid repopulse --home-dir /nonexistent --shell /usr/sbin/nologin repopulse \
    && mkdir -p /var/cache/repopulse/avatars \
    && chown -R repopulse:repopulse /var/cache/repopulse

USER 10001:10001
