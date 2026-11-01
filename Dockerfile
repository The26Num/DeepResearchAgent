# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    API_HOST=0.0.0.0 \
    API_PORT=8000 \
    RESEARCH_DB_PATH=/data/research.db

WORKDIR /app
COPY pyproject.toml README.md ./
COPY app/ ./app/
COPY evaluation/ ./evaluation/
COPY main.py evaluate.py ./
RUN python -m pip install . \
    && groupadd --gid 10001 research \
    && useradd --uid 10001 --gid research --no-create-home research \
    && mkdir -p /data \
    && chown research:research /data

USER research
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "from urllib.request import urlopen; urlopen('http://127.0.0.1:8000/api/health', timeout=3).close()"
CMD ["python", "-m", "app.api"]
