#!/usr/bin/env python3
from __future__ import annotations

import json
import sqlite3
import urllib.error
import urllib.request

from dotenv import dotenv_values


ENV = dotenv_values(".env")
BASE = ENV.get("TEST_HUB_URL") or ENV.get("PUBLIC_BASE_URL") or "http://127.0.0.1:8080"


def request(
    path: str,
    method: str = "GET",
    payload: dict | None = None,
    agent: bool = False,
) -> tuple[int, object]:
    body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    headers = {"Accept": "application/json", **({"Content-Type": "application/json"} if body else {})}
    if agent:
        headers["X-Agent-Key"] = ENV["AGENT_KEY"]
    else:
        headers["Authorization"] = f"Bearer {ENV['ADMIN_KEY']}"
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
    status, created = request(
        "/api/servers",
        "POST",
        {
            "name": "delete-smoke",
            "remark": "删除测试",
            "ssh_host": "192.0.2.50",
            "ssh_port": 22,
            "ssh_user": "root",
            "check_ports": "22",
            "log_files": "/var/log/syslog",
        },
    )
    status_delete, deleted = request("/api/servers/delete-smoke", "DELETE")
    status_report, report = request(
        "/api/report",
        "POST",
        {
            "node_id": "delete-smoke",
            "hostname": "delete-smoke",
            "cpu_percent": 10,
            "memory_percent": 10,
            "disk_percent": 10,
            "ports": {"22": True},
            "errors": [],
        },
        agent=True,
    )
    status_dash, dashboard = request("/api/dashboard")
    still_visible = any(
        item["node_id"] == "delete-smoke" for item in dashboard.get("servers", [])
    )

    connection = sqlite3.connect(ENV.get("DATABASE_PATH") or "data/ops_hub.sqlite3")
    with connection:
        connection.execute("DELETE FROM server_removals WHERE node_id = 'delete-smoke'")
        connection.execute("DELETE FROM servers WHERE node_id = 'delete-smoke'")
    connection.close()

    checks = [
        ("CREATE", status == 200 and created.get("node_id") == "delete-smoke", created),
        ("DELETE", status_delete == 200 and deleted.get("ok"), deleted),
        ("IGNORE_OLD_AGENT", status_report == 200 and report.get("alerts_triggered") == 0, report),
        ("HIDDEN_FROM_DASHBOARD", status_dash == 200 and not still_visible, {"visible": still_visible}),
    ]
    failed = False
    for name, ok, detail in checks:
        print(f"{name}={'PASS' if ok else 'FAIL'} {json.dumps(detail, ensure_ascii=False)}")
        failed = failed or not ok
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
