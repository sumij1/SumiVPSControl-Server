#!/usr/bin/env python3
from __future__ import annotations

import json
import sqlite3
import urllib.error
import urllib.request

from dotenv import dotenv_values


ENV = dotenv_values(".env")
BASE = ENV.get("TEST_HUB_URL") or ENV.get("PUBLIC_BASE_URL") or "http://127.0.0.1:8080"
NODE = "login-event-smoke"


def request(path: str, method: str = "GET", data: dict | None = None, agent: bool = False) -> tuple[int, object]:
    body = json.dumps(data).encode() if data is not None else None
    headers = {"Accept": "application/json", **({"Content-Type": "application/json"} if body else {})}
    headers["X-Agent-Key" if agent else "Authorization"] = ENV["AGENT_KEY"] if agent else f"Bearer {ENV['ADMIN_KEY']}"
    req = urllib.request.Request(BASE + path, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = exc.code
    return status, json.loads(raw) if raw else None


def main() -> int:
    status_report, _ = request(
        "/api/report",
        "POST",
        {
            "node_id": NODE,
            "hostname": NODE,
            "cpu_percent": 5,
            "memory_percent": 10,
            "disk_percent": 10,
            "ports": {"22": True},
            "errors": [],
            "ssh_logins": [
                {
                    "username": "root",
                    "ip": "203.0.113.77",
                    "port": "45678",
                    "method": "publickey",
                    "time": "2026-11-21T05:40:00+08:00",
                }
            ],
        },
        agent=True,
    )
    status_tickets, tickets = request("/api/tickets")
    node_tickets = [item for item in tickets if item.get("node_id") == NODE] if status_tickets == 200 else []
    status_dashboard, dashboard = request("/api/dashboard")
    login_events = [
        item for item in dashboard.get("alerts", [])
        if item.get("node_id") == NODE and item.get("alert_type") == "SSH 登录"
    ] if status_dashboard == 200 else []

    connection = sqlite3.connect(ENV.get("DATABASE_PATH") or "data/ops_hub.sqlite3")
    with connection:
        connection.execute("DELETE FROM alerts WHERE node_id = ?", (NODE,))
        connection.execute("DELETE FROM alert_cooldowns WHERE alert_key LIKE ?", (f"%{NODE}%",))
        connection.execute("DELETE FROM tickets WHERE node_id = ?", (NODE,))
        connection.execute("DELETE FROM server_profiles WHERE node_id = ?", (NODE,))
        connection.execute("DELETE FROM servers WHERE node_id = ?", (NODE,))
    connection.close()

    checks = [
        ("REPORT", status_report == 200, status_report),
        ("NO_TICKET", len(node_tickets) == 0, node_tickets),
        ("LOGIN_EVENT_VISIBLE", len(login_events) == 1, login_events),
    ]
    failed = False
    for name, ok, detail in checks:
        print(f"{name}={'PASS' if ok else 'FAIL'} {json.dumps(detail, ensure_ascii=False)}")
        failed = failed or not ok
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
