"""External collection client. Standard library only, no Zhol import.

Example:
  python examples/python_client.py --api http://127.0.0.1:8765 create --platform 2gis --city Алматы --query "салон красоты" --limit 5
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def main() -> int:
    parser = argparse.ArgumentParser(description="Клиент API сбора Жол")
    parser.add_argument("--api", default="http://127.0.0.1:18080")
    parser.add_argument("--token", default=os.environ.get("ZHOL_ADMIN_TOKEN", ""))
    commands = parser.add_subparsers(dest="command", required=True)
    created = commands.add_parser("create")
    created.add_argument("--platform", required=True)
    created.add_argument("--city", required=True)
    created.add_argument("--query", required=True)
    created.add_argument("--limit", type=int, default=20)
    created.add_argument("--idempotency-key", default=None)
    status = commands.add_parser("status")
    status.add_argument("job_id", type=int)
    results = commands.add_parser("results")
    results.add_argument("job_id", type=int)
    exported = commands.add_parser("export")
    exported.add_argument("job_id", type=int)
    exported.add_argument("--format", default="json", choices=("xlsx", "csv", "json"))
    exported.add_argument("--out", required=True)
    args = parser.parse_args()
    try:
        if args.command == "create":
            body = {
                "platform": args.platform,
                "city": args.city,
                "query": args.query,
                "limit": args.limit,
            }
            if args.idempotency_key:
                body["idempotency_key"] = args.idempotency_key
            print(json.dumps(request(args, "POST", "/api/v1/jobs", body), ensure_ascii=False, indent=2))
        elif args.command == "status":
            print(json.dumps(request(args, "GET", f"/api/v1/jobs/{args.job_id}"), ensure_ascii=False, indent=2))
        elif args.command == "results":
            print(json.dumps(request(args, "GET", f"/api/v1/jobs/{args.job_id}/results"), ensure_ascii=False, indent=2))
        else:
            raw = request(args, "GET", f"/api/v1/jobs/{args.job_id}/export?format={args.format}", decode=False)
            with open(args.out, "wb") as handle:
                handle.write(raw)
            print(args.out)
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 2
    return 0


def request(args: argparse.Namespace, method: str, path: str, body: dict | None = None, decode: bool = True):
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Accept": "application/json"}
    if args.token:
        headers["Authorization"] = f"Bearer {args.token}"
    if data is not None:
        headers["Content-Type"] = "application/json"
    call = Request(args.api.rstrip("/") + path, data=data, headers=headers, method=method)
    try:
        with urlopen(call, timeout=30) as response:
            raw = response.read()
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(detail or exc.reason) from exc
    if not decode:
        return raw
    return json.loads(raw.decode("utf-8"))


if __name__ == "__main__":
    raise SystemExit(main())
