"""Synthetic scale measurements. Does not open map sites or the working database."""

from __future__ import annotations

import json
import os
import resource
import statistics
import subprocess
import time
import tracemalloc
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import sessionmaker

from zhol.agents.scout.identity import Profile, classify_links, relate
from zhol.agents.scout.identity_store import relate_stored
from zhol.agents.scout.match_index import rebuild_match_keys
from zhol.db.models import CardSnapshot, Company
from zhol.integration.live import iter_companies
from zhol.integration.schema import export_file

SIZES = (1000, 10000, 50000, 100000)
CITY = "Алматы"
ROOT = Path(__file__).resolve().parents[1]


def _alembic_config(url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.set_main_option("prepend_sys_path", str(ROOT))
    cfg.set_main_option("sqlalchemy.url", url)
    os.environ["DATABASE_URL"] = url
    return cfg


def _docker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], check=True, capture_output=True, text=True)


def _legacy_find(session) -> str | None:
    """The previous city-wide scan: every company, then one snapshot query each."""
    incoming = Profile("North", "улица Абая, 10", ("77010000001",), ())
    companies = session.scalars(select(Company).where(Company.city == CITY)).all()
    same = 0
    ambiguous = False
    for company in companies:
        snap = session.scalar(
            select(CardSnapshot)
            .where(CardSnapshot.company_id == company.id, CardSnapshot.platform == "yandex")
            .order_by(CardSnapshot.collected_at.desc(), CardSnapshot.id.desc())
        )
        fields = snap.fields if snap is not None and isinstance(snap.fields, dict) else {}
        address = fields.get("address") if isinstance(fields.get("address"), str) else None
        phones = tuple(str(item) for item in fields.get("phones") or []) if isinstance(fields.get("phones"), list) else ()
        relation = relate(incoming, Profile(company.name, address, phones, ()))
        if relation.kind == "same":
            same += 1
        elif relation.kind == "ambiguous":
            ambiguous = True
    if same == 1 and not ambiguous:
        return "same"
    if same or ambiguous:
        return "ambiguous"
    return None


def _optimized_find(session) -> str:
    action, company, _phone = classify_links(
        relate_stored(session, CITY, "yandex", Profile("North", "улица Абая, 10", ("77010000001",), ()))
    )
    if action != "attach" or company is None or company.name != "North":
        raise RuntimeError(f"optimized match failed: {action}")
    return action


def _seed(connection, count: int) -> None:
    connection.execute(
        text("TRUNCATE company_match_keys, card_snapshots, company_platform_ids, companies RESTART IDENTITY CASCADE")
    )
    batch = 2000
    for start in range(0, count, batch):
        rows = []
        snaps = []
        stop = min(start + batch, count)
        for number in range(start, stop):
            if number == 0:
                name, phone = "North", "77010000001"
            else:
                name, phone = f"Salon {number}", f"7{number:010d}"
            rows.append({"name": name, "city": CITY, "source": "bench", "id_2gis": f"bench-{number}"})
        connection.execute(
            text(
                "INSERT INTO companies (name, city, source, id_2gis) "
                "VALUES (:name, :city, :source, :id_2gis)"
            ),
            rows,
        )
        result = connection.execute(
            text("SELECT id, id_2gis FROM companies WHERE id_2gis = ANY(:ids)"),
            {"ids": [f"bench-{number}" for number in range(start, stop)]},
        ).all()
        for company_id, id_2gis in result:
            number = int(str(id_2gis).split("-", 1)[1])
            phone = "77010000001" if number == 0 else f"7{number:010d}"
            name = "North" if number == 0 else f"Salon {number}"
            snaps.append(
                {
                    "company_id": company_id,
                    "platform": "yandex",
                    "match_status": "found",
                    "url": f"https://example.test/{id_2gis}",
                    "fields": json.dumps(
                        {"name": name, "address": "улица Абая, 10" if number == 0 else f"улица Другая, {number}", "phones": [phone]},
                        ensure_ascii=False,
                    ),
                    "field_sources": "{}",
                    "purpose": "prospect",
                    "collected_at": "2026-10-25T12:00:00+05",
                    "collected_by": "bench",
                    "status": "collected",
                }
            )
        connection.execute(
            text(
                """
                INSERT INTO card_snapshots (
                    company_id, platform, match_status, url, fields, field_sources,
                    purpose, collected_at, collected_by, status
                )
                VALUES (
                    :company_id, :platform, :match_status, :url, CAST(:fields AS jsonb),
                    CAST(:field_sources AS jsonb), :purpose, :collected_at, :collected_by, :status
                )
                """
            ),
            snaps,
        )


def _time_samples(function, samples: int) -> dict:
    timings = []
    for _ in range(samples):
        started = time.perf_counter()
        function()
        timings.append((time.perf_counter() - started) * 1000)
    timings.sort()
    p95_index = max(0, min(len(timings) - 1, int(len(timings) * 0.95) - 1)) if len(timings) >= 20 else None
    return {
        "samples": len(timings),
        "avg_ms": round(statistics.fmean(timings), 2),
        "p95_ms": None if p95_index is None else round(timings[p95_index], 2),
    }


def _queries(engine, function) -> int:
    count = {"n": 0}

    def record(*_args) -> None:
        count["n"] += 1

    event.listen(engine, "before_cursor_execute", record)
    try:
        function()
    finally:
        event.remove(engine, "before_cursor_execute", record)
    return count["n"]


def _rss() -> int:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value if os.uname().sysname == "Darwin" else value * 1024


def main() -> None:
    name = f"zhol-bench-{os.getpid()}"
    _docker(
        "run", "-d", "--name", name,
        "-e", "POSTGRES_USER=zhol", "-e", "POSTGRES_PASSWORD=zhol", "-e", "POSTGRES_DB=zhol_bench",
        "-p", "127.0.0.1:0:5432", "postgres:16",
    )
    results = []
    try:
        port = None
        for _ in range(40):
            ready = subprocess.run(
                ["docker", "exec", name, "pg_isready", "-h", "127.0.0.1", "-U", "zhol", "-d", "zhol_bench"],
                capture_output=True, text=True,
            )
            if ready.returncode == 0:
                port = _docker("port", name, "5432").stdout.strip().rsplit(":", 1)[1]
                break
            time.sleep(0.25)
        if port is None:
            raise RuntimeError("benchmark PostgreSQL did not become ready")
        url = f"postgresql+psycopg://zhol:zhol@127.0.0.1:{port}/zhol_bench"
        if url.rsplit("/", 1)[1] == "zhol":
            raise RuntimeError("refusing the working database")
        command.upgrade(_alembic_config(url), "head")
        engine = create_engine(url, future=True)
        factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
        for count in SIZES:
            with engine.begin() as connection:
                started = time.perf_counter()
                _seed(connection, count)
                seed_s = time.perf_counter() - started
                started = time.perf_counter()
                keys = rebuild_match_keys(connection)
                key_s = time.perf_counter() - started
            samples = 20 if count <= 10000 else 5
            legacy_samples = 5 if count <= 10000 else 1

            def optimized() -> None:
                with factory() as session:
                    _optimized_find(session)

            def legacy() -> None:
                with factory() as session:
                    if _legacy_find(session) != "same":
                        raise RuntimeError("legacy match failed")

            tracemalloc.start()
            optimized_time = _time_samples(optimized, samples)
            _opt_current, opt_peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            tracemalloc.start()
            legacy_time = _time_samples(legacy, legacy_samples)
            _legacy_current, legacy_peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            export_times = {}
            export_peak = {}
            for fmt in ("csv", "json", "xlsx"):
                tracemalloc.start()
                started = time.perf_counter()
                with factory() as session:
                    path, _media, _filename = export_file(
                        fmt, iter_companies(session, q="", platform=None, city=CITY), notes=("bench",)
                    )
                elapsed = time.perf_counter() - started
                _current, peak = tracemalloc.get_traced_memory()
                tracemalloc.stop()
                export_times[fmt] = round(elapsed, 3)
                export_peak[fmt] = peak
                path.unlink(missing_ok=True)
            with engine.connect() as connection:
                plan = connection.execute(
                    text(
                        "EXPLAIN (ANALYZE, BUFFERS) "
                        "SELECT company_id FROM company_match_keys "
                        "WHERE city = :city AND kind = 'phone' AND platform = 'yandex' AND value = '77010000001'"
                    ),
                    {"city": CITY},
                ).scalars().all()
                city_plan = connection.execute(
                    text("EXPLAIN (ANALYZE, BUFFERS) SELECT id FROM companies WHERE city = :city"),
                    {"city": CITY},
                ).scalars().all()
                db_size = connection.execute(text("SELECT pg_database_size(current_database())")).scalar_one()
                index_size = connection.execute(
                    text(
                        "SELECT coalesce(sum(pg_relation_size(indexrelid)), 0) "
                        "FROM pg_index JOIN pg_class ON pg_class.oid = pg_index.indexrelid"
                    )
                ).scalar_one()
            row = {
                "companies": count,
                "seed_seconds": round(seed_s, 3),
                "seed_rows_per_second": round(count / seed_s, 1),
                "rebuild_keys_seconds": round(key_s, 3),
                "keys": keys,
                "optimized": optimized_time,
                "optimized_queries": _queries(engine, optimized),
                "optimized_tracemalloc_peak": opt_peak,
                "legacy": legacy_time,
                "legacy_queries": _queries(engine, legacy),
                "legacy_tracemalloc_peak": legacy_peak,
                "export_seconds": export_times,
                "export_tracemalloc_peak": export_peak,
                "database_bytes": int(db_size),
                "index_bytes": int(index_size),
                "match_plan": list(plan),
                "city_plan": list(city_plan),
                "rss_bytes": _rss(),
            }
            results.append(row)
            print(json.dumps({k: row[k] for k in row if k not in {"match_plan", "city_plan"}}, ensure_ascii=False), flush=True)
        destination = ROOT / "output" / "benchmark-scale.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"wrote {destination}")
    finally:
        subprocess.run(["docker", "rm", "-f", name], check=False, capture_output=True)


if __name__ == "__main__":
    main()
