"""Disposable release checks. Does not open map sites or the working database.

Measures HTTP export, migration round-trip, and the collection API on synthetic rows.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "release-local-token"
CITY_MAIN = "Алматы"
CITY_SMALL = "Астана"


def _docker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], check=True, capture_output=True, text=True)


def _alembic_config(url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.set_main_option("prepend_sys_path", str(ROOT))
    cfg.set_main_option("sqlalchemy.url", url)
    os.environ["DATABASE_URL"] = url
    return cfg


def _fingerprint(connection) -> dict:
    companies = connection.execute(
        text(
            """
            SELECT count(*)::bigint,
                   coalesce(min(id), 0),
                   coalesce(max(id), 0),
                   md5(coalesce(string_agg(name, ',' ORDER BY id), ''))
            FROM companies
            """
        )
    ).one()
    snapshots = connection.execute(text("SELECT count(*) FROM card_snapshots")).scalar_one()
    return {
        "companies": int(companies[0]),
        "min_id": int(companies[1]),
        "max_id": int(companies[2]),
        "names_md5": companies[3],
        "snapshots": int(snapshots),
    }


def _seed(connection, count: int) -> None:
    connection.execute(
        text("TRUNCATE card_snapshots, company_platform_ids, companies RESTART IDENTITY CASCADE")
    )
    connection.execute(
        text(
            """
            INSERT INTO companies (name, city, source)
            SELECT 'N' || lpad(n::text, 6, '0'),
                   CASE WHEN n > :cut THEN :small ELSE :main END,
                   'release-check'
            FROM generate_series(1, :count) AS n
            """
        ),
        {"count": count, "cut": count - 10, "main": CITY_MAIN, "small": CITY_SMALL},
    )
    connection.execute(
        text(
            """
            INSERT INTO card_snapshots (
                company_id, platform, match_status, fields, field_sources,
                purpose, collected_at, collected_by, status
            )
            SELECT id, 'yandex', 'found',
                   jsonb_build_object(
                       'name', name,
                       'address', 'улица Абая, 10',
                       'phones', jsonb_build_array('7' || lpad(id::text, 10, '0'))
                   ),
                   '{}'::jsonb,
                   'prospect', now(), 'release-check', 'collected'
            FROM companies
            """
        )
    )


def _rss(pid: int) -> int:
    result = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True)
    text_value = result.stdout.strip()
    if not text_value:
        return 0
    return int(text_value.split()[0]) * 1024


def _temp_bytes() -> int:
    total = 0
    for path in Path(tempfile.gettempdir()).glob("zhol-export-*"):
        try:
            total += path.stat().st_size
        except OSError:
            continue
    return total


class _Sampler:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.peak_rss = 0
        self.peak_temp = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self) -> "_Sampler":
        self._thread.start()
        return self

    def __exit__(self, *_args) -> None:
        self._stop.set()
        self._thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop.is_set():
            self.peak_rss = max(self.peak_rss, _rss(self.pid))
            self.peak_temp = max(self.peak_temp, _temp_bytes())
            self._stop.wait(0.2)


def _request(port: int, method: str, path: str, body: dict | None = None, timeout: float = 600) -> tuple[int, bytes, float]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    call = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, headers=headers, method=method)
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(call, timeout=timeout) as response:
            payload = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        status = exc.code
    return status, payload, time.perf_counter() - started


def _csv_rows(payload: bytes) -> list[list[str]]:
    import csv
    import io

    text_value = payload.decode("utf-8-sig")
    return list(csv.reader(io.StringIO(text_value)))


def main() -> None:
    name = f"zhol-release-{os.getpid()}"
    _docker(
        "run", "-d", "--name", name,
        "-e", "POSTGRES_USER=zhol", "-e", "POSTGRES_PASSWORD=zhol", "-e", "POSTGRES_DB=zhol_release",
        "-p", "127.0.0.1:0:5432", "postgres:16",
    )
    server: subprocess.Popen[str] | None = None
    report: dict = {"database": "zhol_release", "synthetic_companies": 100000}
    try:
        port = None
        for _ in range(40):
            ready = subprocess.run(
                ["docker", "exec", name, "pg_isready", "-h", "127.0.0.1", "-U", "zhol", "-d", "zhol_release"],
                capture_output=True, text=True,
            )
            if ready.returncode == 0:
                port = _docker("port", name, "5432").stdout.strip().rsplit(":", 1)[1]
                break
            time.sleep(0.25)
        if port is None:
            raise RuntimeError("release PostgreSQL did not become ready")
        url = f"postgresql+psycopg://zhol:zhol@127.0.0.1:{port}/zhol_release"
        if url.rsplit("/", 1)[1] == "zhol":
            raise RuntimeError("refusing the working database")
        cfg = _alembic_config(url)
        command.upgrade(cfg, "e5a9b3c20f41")
        engine = create_engine(url, future=True)
        with engine.begin() as connection:
            started = time.perf_counter()
            _seed(connection, 100000)
            report["seed_seconds"] = round(time.perf_counter() - started, 3)
            before = _fingerprint(connection)
        report["before_migration"] = before
        started = time.perf_counter()
        command.upgrade(cfg, "head")
        report["upgrade_seconds"] = round(time.perf_counter() - started, 3)
        with engine.connect() as connection:
            after = _fingerprint(connection)
            keys = int(connection.execute(text("SELECT count(*) FROM company_match_keys")).scalar_one())
            orphans = int(
                connection.execute(
                    text(
                        """
                        SELECT count(*)
                        FROM company_match_keys AS keys
                        LEFT JOIN companies ON companies.id = keys.company_id
                        WHERE companies.id IS NULL
                        """
                    )
                ).scalar_one()
            )
            version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            plan = "\n".join(
                connection.execute(
                    text(
                        """
                        EXPLAIN
                        SELECT company_id FROM company_match_keys
                        WHERE city = 'Алматы' AND kind = 'phone' AND platform = 'yandex' AND value = '70000000001'
                        """
                    )
                ).scalars()
            )
        report["after_upgrade"] = after
        report["keys"] = keys
        report["orphan_keys"] = orphans
        report["alembic_version"] = version
        report["match_index_plan"] = plan
        report["fingerprint_unchanged"] = before == after
        started = time.perf_counter()
        command.downgrade(cfg, "e5a9b3c20f41")
        report["downgrade_seconds"] = round(time.perf_counter() - started, 3)
        with engine.connect() as connection:
            rolled = _fingerprint(connection)
            table_gone = connection.execute(text("SELECT to_regclass('public.company_match_keys')")).scalar_one() is None
            version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        report["after_downgrade"] = rolled
        report["downgrade_keeps_rows"] = rolled == before and table_gone and version == "e5a9b3c20f41"
        command.upgrade(cfg, "head")
        with engine.connect() as connection:
            restored = _fingerprint(connection)
            keys_again = int(connection.execute(text("SELECT count(*) FROM company_match_keys")).scalar_one())
        report["after_second_upgrade"] = restored
        report["second_upgrade_keys"] = keys_again
        report["second_upgrade_keeps_rows"] = restored == before and keys_again == keys
        (ROOT / "output").mkdir(exist_ok=True)
        (ROOT / "output" / "release-http.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        api_port = sock.getsockname()[1]
        sock.close()
        env = os.environ.copy()
        env["DATABASE_URL"] = url
        env["ZHOL_ROOT"] = str(ROOT)
        env["ZHOL_CONFIG_DIR"] = str(ROOT / "tests" / "golden" / "review-engine" / "config")
        env["ANTHROPIC_API_KEY"] = "placeholder"
        env["OPENAI_API_KEY"] = "placeholder"
        env["GOOGLE_API_KEY"] = "placeholder"
        env["ZHOL_ADMIN_TOKEN"] = TOKEN
        env["ZHOL_2GIS_BROWSER_GRANT"] = "written-browser-retention-local"
        env["ZHOL_CHROME_BINARY"] = "/usr/bin/true"
        env.pop("ZHOL_YANDEX_BROWSER_GRANT", None)
        env.pop("ZHOL_GOOGLE_BROWSER_GRANT", None)
        log_path = ROOT / "output" / "release-uvicorn.log"
        log_path.parent.mkdir(exist_ok=True)
        log_handle = log_path.open("w", encoding="utf-8")
        server = subprocess.Popen(
            [str(ROOT / ".venv/bin/python"), "-m", "uvicorn", "zhol.api.main:app", "--host", "127.0.0.1", "--port", str(api_port)],
            cwd=ROOT,
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{api_port}/health", timeout=1) as response:
                    if response.status == 200:
                        break
            except Exception:
                time.sleep(0.2)
        else:
            raise RuntimeError("API did not become ready")

        status, payload, _elapsed = _request(api_port, "GET", "/api/v1/companies", timeout=30)
        # The helper always sends the token. Check a bare call separately.
        bare = urllib.request.Request(f"http://127.0.0.1:{api_port}/api/v1/companies")
        try:
            urllib.request.urlopen(bare, timeout=10)
            unauth = 200
        except urllib.error.HTTPError as exc:
            unauth = exc.code
        report["unauthenticated_status"] = unauth

        client = [
            str(ROOT / ".venv/bin/python"),
            str(ROOT / "examples/python_client.py"),
            "--api", f"http://127.0.0.1:{api_port}",
            "--token", TOKEN,
        ]
        created = subprocess.run(client + ["create", "--platform", "2gis", "--city", "Алматы", "--query", "салон красоты", "--limit", "5", "--idempotency-key", "release-check"], capture_output=True, text=True)
        report["client_create_code"] = created.returncode
        report["client_create"] = created.stdout[-500:] or created.stderr[-500:]
        job_id = None
        if created.returncode == 0:
            job_id = json.loads(created.stdout)["id"]
            status_run = subprocess.run(client + ["status", str(job_id)], capture_output=True, text=True)
            report["client_status_code"] = status_run.returncode
            report["client_status"] = json.loads(status_run.stdout)["status"] if status_run.returncode == 0 else status_run.stderr[-300:]
            paused, paused_body, _elapsed = _request(api_port, "POST", f"/api/v1/jobs/{job_id}/pause")
            resumed, resumed_body, _elapsed = _request(api_port, "POST", f"/api/v1/jobs/{job_id}/resume")
            stopped, stopped_body, _elapsed = _request(api_port, "POST", f"/api/v1/jobs/{job_id}/stop")
            report["controls"] = {
                "pause": paused,
                "pause_status": json.loads(paused_body.decode()).get("status") if paused == 200 else paused_body.decode()[:300],
                "resume": resumed,
                "resume_status": json.loads(resumed_body.decode()).get("status") if resumed == 200 else resumed_body.decode()[:300],
                "stop": stopped,
                "stop_status": json.loads(stopped_body.decode()).get("status") if stopped == 200 else stopped_body.decode()[:300],
            }
            results = subprocess.run(client + ["results", str(job_id)], capture_output=True, text=True)
            report["client_results_code"] = results.returncode
            export_out = ROOT / "output" / "release-job.json"
            export_out.parent.mkdir(exist_ok=True)
            exported = subprocess.run(client + ["export", str(job_id), "--format", "json", "--out", str(export_out)], capture_output=True, text=True)
            report["client_export_code"] = exported.returncode
            if exported.returncode == 0:
                report["client_export_total"] = json.loads(export_out.read_text()).get("total")
                export_out.unlink(missing_ok=True)

        page_status, page_body, page_s = _request(api_port, "GET", "/api/v1/companies?limit=2&offset=0")
        next_status, next_body, next_s = _request(api_port, "GET", "/api/v1/companies?limit=2&offset=2")
        first = json.loads(page_body.decode())
        second = json.loads(next_body.decode())
        first_ids = [item["id"] for item in first.get("items", [])]
        second_ids = [item["id"] for item in second.get("items", [])]
        report["companies_page"] = {
            "status": page_status,
            "seconds": round(page_s, 3),
            "next_status": next_status,
            "next_seconds": round(next_s, 3),
            "first_ids": first_ids,
            "second_ids": second_ids,
            "descending": first_ids == sorted(first_ids, reverse=True) and first_ids > second_ids,
            "overlap": sorted(set(first_ids) & set(second_ids)),
        }

        highest = before["max_id"]
        with engine.connect() as connection:
            highest_name = connection.execute(text("SELECT name FROM companies WHERE id = :id"), {"id": highest}).scalar_one()
        exports = {}
        for fmt in ("csv", "json", "xlsx"):
            with _Sampler(server.pid) as sampler:
                status, payload, elapsed = _request(api_port, "GET", f"/api/v1/export?format={fmt}")
            entry = {
                "status": status,
                "seconds": round(elapsed, 3),
                "bytes": len(payload),
                "peak_rss_bytes": sampler.peak_rss,
                "peak_temp_bytes": sampler.peak_temp,
                "temp_after_bytes": _temp_bytes(),
            }
            if fmt == "csv" and status == 200:
                rows = _csv_rows(payload)
                entry["data_rows"] = len(rows) - 1
                entry["first_name"] = rows[1][0] if len(rows) > 1 else None
                entry["first_is_highest_id"] = entry["first_name"] == highest_name
            if fmt == "json" and status == 200:
                total = json.loads(payload.decode())["total"]
                entry["total"] = total
            if fmt == "xlsx" and status == 200:
                import zipfile
                import io
                with zipfile.ZipFile(io.BytesIO(payload)) as book:
                    sheet = book.read("xl/worksheets/sheet1.xml").decode("utf-8")
                entry["sheet_rows"] = sheet.count("<row ")
            exports[fmt] = entry
            print(fmt, json.dumps(entry), flush=True)
        report["exports"] = exports

        status, payload, elapsed = _request(api_port, "GET", "/api/v1/export?format=csv&city=" + urllib.request.quote(CITY_SMALL))
        rows = _csv_rows(payload)
        report["city_filter"] = {
            "status": status,
            "seconds": round(elapsed, 3),
            "data_rows": len(rows) - 1,
            "cities": sorted({row[4] for row in rows[1:]}),
        }
        status, payload, elapsed = _request(api_port, "GET", "/api/v1/export?format=json&platform=yandex")
        report["platform_filter"] = {"status": status, "seconds": round(elapsed, 3), "total": json.loads(payload.decode())["total"]}
        status, payload, _elapsed = _request(api_port, "GET", "/api/v1/export?format=json&city=" + urllib.request.quote("Неттакого"))
        report["empty_filter"] = {"status": status, "total": json.loads(payload.decode())["total"]}

        with engine.connect() as connection:
            before_insert = int(connection.execute(text("SELECT count(*) FROM companies")).scalar_one())

        def insert_later() -> None:
            deadline = time.time() + 30
            while time.time() < deadline and _temp_bytes() == 0:
                time.sleep(0.05)
            time.sleep(0.5)
            with engine.begin() as connection:
                connection.execute(
                    text("INSERT INTO companies (name, city, source) VALUES ('CONCURRENT-ONLY', 'Алматы', 'release-check')")
                )

        inserter = threading.Thread(target=insert_later)
        inserter.start()
        status, payload, elapsed = _request(api_port, "GET", "/api/v1/export?format=csv")
        inserter.join()
        rows = _csv_rows(payload)
        names = {row[0] for row in rows[1:]}
        report["concurrent_insert"] = {
            "status": status,
            "seconds": round(elapsed, 3),
            "data_rows": len(rows) - 1,
            "rows_before_insert": before_insert,
            "concurrent_name_present": "CONCURRENT-ONLY" in names,
            "matches_snapshot": len(rows) - 1 == before_insert,
        }

        import http.client
        raw = http.client.HTTPConnection("127.0.0.1", api_port, timeout=600)
        raw.request("GET", "/api/v1/export?format=csv", headers={"Authorization": f"Bearer {TOKEN}"})
        response = raw.getresponse()
        response.read(65536)
        raw.close()
        time.sleep(1.5)
        report["interrupted_download"] = {
            "status": response.status,
            "temp_after_bytes": _temp_bytes(),
        }
        out = ROOT / "output" / "release-http.json"
        out.parent.mkdir(exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print("wrote", out)
    finally:
        if server is not None and server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
        subprocess.run(["docker", "rm", "-f", name], check=False, capture_output=True)


if __name__ == "__main__":
    main()
