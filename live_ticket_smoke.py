#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import sqlite3
import urllib.error
import urllib.request

from dotenv import dotenv_values


ENV = dotenv_values(".env")
BASE = ENV.get("TEST_HUB_URL") or ENV.get("PUBLIC_BASE_URL") or "http://127.0.0.1:8080"
NODE = "live-ticket-smoke"


def request(path: str, method: str = "GET", payload: dict | None = None, agent: bool = False) -> tuple[int, object]:
    body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    headers = {"Accept": "application/json", **({"Content-Type": "application/json"} if body else {})}
    headers["X-Agent-Key" if agent else "Authorization"] = ENV["AGENT_KEY"] if agent else f"Bearer {ENV['ADMIN_KEY']}"
    req = urllib.request.Request(BASE + path, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=45) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = exc.code
    return status, json.loads(raw) if raw else None


def report() -> dict:
    return {
        "node_id": NODE,
        "hostname": NODE,
        "cpu_percent": 96,
        "memory_percent": 20,
        "disk_percent": 20,
        "ports": {"22": True, "443": False},
        "errors": [],
    }


def node_tickets() -> list[dict]:
    status, rows = request("/api/tickets")
    return [row for row in rows if row.get("node_id") == NODE] if status == 200 else []


async def main() -> int:
    request("/api/report", "POST", report(), agent=True)
    await asyncio.sleep(4)
    first = node_tickets()
    if not first:
        print("FIRST_TICKET=FAIL")
        return 1
    first_id = first[0]["id"]
    request(
        f"/api/tickets/{first_id}",
        "PATCH",
        {
            "status": "resolved",
            "severity": first[0]["severity"],
            "remark": "已处理完毕，这不是计划维护，请继续正常监控后续问题。",
        },
    )
    await asyncio.sleep(5)
    request("/api/report", "POST", report(), agent=True)
    await asyncio.sleep(2)
    rows = node_tickets()

    connection = sqlite3.connect(ENV.get("DATABASE_PATH") or "data/ops_hub.sqlite3")
    with connection:
        connection.execute("DELETE FROM alerts WHERE node_id = ?", (NODE,))
        connection.execute("DELETE FROM alert_cooldowns WHERE alert_key LIKE ?", (f"%{NODE}%",))
        connection.execute("DELETE FROM tickets WHERE node_id = ?", (NODE,))
        connection.execute("DELETE FROM server_profiles WHERE node_id = ?", (NODE,))
        connection.execute("DELETE FROM servers WHERE node_id = ?", (NODE,))
    connection.close()

    checks = [
        ("ONE_ACTIVE_INITIAL", len(first) == 1, len(first)),
        (
            "OLD_TICKET_REMAINS_RESOLVED",
            any(row["id"] == first_id and row["status"] == "resolved" for row in rows),
            next((row for row in rows if row["id"] == first_id), None),
        ),
        (
            "NEW_TICKET_CREATED",
            len([row for row in rows if row["id"] != first_id]) == 1,
            [row["fingerprint"] for row in rows],
        ),
    ]
    failed = False
    for name, ok, detail in checks:
        print(f"{name}={'PASS' if ok else 'FAIL'} {json.dumps(detail, ensure_ascii=False)}")
        failed = failed or not ok
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
