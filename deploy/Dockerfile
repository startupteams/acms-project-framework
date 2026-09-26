# ACMS production application image (plan §9).
# Runtime only: Alembic owns schema and migrations run explicitly before start
# (deploy.sh / update.sh) — the app never creates or alters tables.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml README.md alembic.ini ./
COPY acms ./acms
COPY migrations ./migrations

RUN pip install --no-cache-dir .

# Non-root runtime user (plan §9).
RUN useradd --system --uid 10001 --no-create-home acms
USER acms

EXPOSE 8000

# Container-local health check (plan §9); the reverse proxy also health-gates.
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"]

CMD ["uvicorn", "acms.main:app", "--host", "0.0.0.0", "--port", "8000"]
