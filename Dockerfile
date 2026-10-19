FROM python:3.12-slim-bookworm

WORKDIR /app
ENV PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml /app/pyproject.toml
COPY zhol /app/zhol

RUN pip install --no-cache-dir --upgrade pip==25.2 \
    && pip install --no-cache-dir .

COPY alembic.ini /app/alembic.ini
COPY alembic /app/alembic
COPY config /app/config
COPY prompts /app/prompts
COPY council /app/council

CMD ["python", "-m", "zhol.idle"]
