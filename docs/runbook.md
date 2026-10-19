# Runbook

## Local start

1. `cp .env.example .env` and fill the values you have. Secrets live only in `.env` (ignored by git).
2. `docker compose up -d --build`
3. Migrate once per new database: `docker compose run --rm api alembic upgrade head`
4. `docker compose ps`: `db`, `worker`, `browser-worker`, `scheduler` run. `api` starts only when `config/models.yaml` is complete
   and the three API keys are set (decision R6). Its log lists every missing value (`configuration is not usable`).

## Reference data and score

The etalon CSV is not baked into the image; mount it read-only:

```
docker compose run --rm -v "$PWD/source/etalon-50.csv:/app/source/etalon-50.csv:ro" api python -m zhol.cli score-check
```

Expected: `Совпало 50 из 50`. Running it again imports nothing new. `source/base-contacts.csv` is refused by the importer.

## Scheduler

The `scheduler` service runs `python -m zhol.cli scheduler-tick --loop 60`. One manual tick at a fixed time:

```
docker compose run --rm api python -m zhol.cli scheduler-tick --now 2026-11-02T09:00+05:00
```

A naive `--now` is UTC. Each periodic job is created once per slot; expired job leases are recovered on every tick.

## Without Docker (development)

```
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest              # needs Docker for the disposable PostgreSQL
.venv/bin/python -m zhol.cli eval --point review-engine
```

Stub mode for the API (no real models, placeholder keys):

```
ZHOL_CONFIG_DIR=tests/golden/review-engine/config ANTHROPIC_API_KEY=x OPENAI_API_KEY=x GOOGLE_API_KEY=x \
  .venv/bin/uvicorn zhol.api.main:app --port 18090
curl http://127.0.0.1:18090/health
```

## Dead job

A job that failed 3 times becomes `dead`, creates `human_tasks` with `reason = 'job_dead'` and a Telegram message (when the bot is
configured). `jobs.last_error` holds the redacted error. Fix the cause, then enqueue a new job of the same type.
