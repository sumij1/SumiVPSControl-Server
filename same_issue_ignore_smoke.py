#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime, timezone

from dotenv import dotenv_values


ENV = dotenv_values(".env")
BASE = ENV.get("TEST_HUB_URL") or ENV.get("PUBLIC_BASE_URL") or "http://127.0.0.1:8080"
NODE = "same-issue-smoke"


def request(path: str, method: str = "GET", data: dict | None = None, agent: bool = False) -> tuple[int, object]:
    body = json.dumps(data).encode() if data is not None else None
    headers = {"Accept": "application/json", **({"Content-Type": "application/json"} if body else {})}
    headers["X-Agent-Key" if agent else "Authorization"] = ENV["AGENT_KEY"] if agent else f"Bearer {ENV['ADMIN_KEY']}"
    req = urllib.request.Request(BASE + path, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=40) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = exc.code
    return status, json.loads(raw) if raw else None


def report(ports: dict) -> dict:
    return {
        "node_id": NODE,
        "hostname": NODE,
        "cpu_percent": 85,
        "memory_percent": 20,
        "disk_percent": 20,
        "ports": ports,
        "errors": [],
    }


def tickets() -> list[dict]:
    status, rows = request("/api/tickets")
    return [row for row in rows if row.get("node_id") == NODE] if status == 200 else []


async def main() -> int:
    request("/api/report", "POST", report({"22": True}), agent=True)
    await asyncio.sleep(4)
    initial = tickets()
    if len(initial) != 1:
        print("INITIAL_TICKETS=FAIL", len(initial))
        return 1
    request(
        f"/api/tickets/{initial[0]['id']}",
        "PATCH",
        {
            "status": "ignored",
            "severity": initial[0]["severity"],
            "remark": "CPU偏高属于计划维护，20分钟内请忽略相同的CPU问题；其他问题仍要报警。",
        },
    )
    await asyncio.sleep(5)
    reviewed = tickets()[0]
    request("/api/report", "POST", report({"22": True}), agent=True)
    await asyncio.sleep(1)
    after_same = tickets()
    request("/api/report", "POST", report({"22": True, "80": False}), agent=True)
    await asyncio.sleep(1)
    after_new = tickets()

    connection = sqlite3.connect(ENV.get("DATABASE_PATH") or "data/ops_hub.sqlite3")
    with connection:
        connection.execute("DELETE FROM alerts WHERE node_id = ?", (NODE,))
        connection.execute("DELETE FROM alert_cooldowns WHERE alert_key LIKE ?", (f"%{NODE}%",))
        connection.execute("DELETE FROM tickets WHERE node_id = ?", (NODE,))
        connection.execute("DELETE FROM server_profiles WHERE node_id = ?", (NODE,))
        connection.execute("DELETE FROM servers WHERE node_id = ?", (NODE,))
    connection.close()

    checks = [
        ("SAME_ISSUE_SUPPRESSED", len(after_same) == 1, len(after_same)),
        ("NEW_ISSUE_CREATES_TICKET", len(after_new) == 2, len(after_new)),
        (
            "IGNORE_AT_MOST_20_MIN",
            bool(reviewed.get("ai_ignore_until"))
            and (
                datetime.fromisoformat(reviewed["ai_ignore_until"]).astimezone(
                    timezone.utc
                )
                - datetime.now(timezone.utc)
            ).total_seconds()
            <= 1200,
            reviewed.get("ai_ignore_until"),
        ),
    ]
    failed = False
    for name, ok, detail in checks:
        print(f"{name}={'PASS' if ok else 'FAIL'} {json.dumps(detail, ensure_ascii=False)}")
        failed = failed or not ok
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
