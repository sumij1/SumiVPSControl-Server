#!/usr/bin/env python3
from __future__ import annotations

import json
import sqlite3
import time
import urllib.error
import urllib.request

from dotenv import dotenv_values


ENV = dotenv_values(".env")
BASE = ENV.get("TEST_HUB_URL") or ENV.get("PUBLIC_BASE_URL") or "http://127.0.0.1:8080"
NODE = "traffic-smoke"


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


def report(upload: int, download: int) -> dict:
    return {
        "node_id": NODE,
        "hostname": NODE,
        "cpu_percent": 5,
        "memory_percent": 10,
        "disk_percent": 10,
        "ports": {"22": True},
        "errors": [],
        "network_upload_bps": 1200,
        "network_download_bps": 3400,
        "network_upload_total_bytes": upload,
        "network_download_total_bytes": download,
    }


def main() -> int:
    status_create, created = request(
        "/api/servers",
        "POST",
        {
            "name": NODE,
            "remark": "流量测试",
            "ssh_host": "192.0.2.60",
            "ssh_port": 22,
            "ssh_user": "root",
            "lianlian_id": "LL-123456",
            "lianlian_password": "test-password-123",
            "check_ports": "22",
            "log_files": "/var/log/syslog",
        },
    )
    request("/api/report", "POST", report(1_000_000, 2_000_000), agent=True)
    time.sleep(1)
    request("/api/report", "POST", report(1_000_500, 2_001_200), agent=True)
    time.sleep(1)
    status_dash, dashboard = request("/api/dashboard")
    server = next((item for item in dashboard.get("servers", []) if item["node_id"] == NODE), {})
    usage = server.get("network_usage_30d", {})
    asklink = server.get("asklink", {})
    metrics = server.get("metrics", {})
    status_patch, _ = request(
        f"/api/servers/{NODE}",
        "PATCH",
        {"remark": "流量测试已更新", "lianlian_id": "LL-123456", "lianlian_password": ""},
    )
    status_dash2, dashboard2 = request("/api/dashboard")
    server2 = next((item for item in dashboard2.get("servers", []) if item["node_id"] == NODE), {})

    connection = sqlite3.connect(ENV.get("DATABASE_PATH") or "data/ops_hub.sqlite3")
    encrypted = connection.execute(
        "SELECT lianlian_password_enc FROM server_profiles WHERE node_id = ?", (NODE,)
    ).fetchone()
    connection.close()

    request(f"/api/servers/{NODE}", "DELETE")
    connection = sqlite3.connect(ENV.get("DATABASE_PATH") or "data/ops_hub.sqlite3")
    with connection:
        connection.execute("DELETE FROM server_removals WHERE node_id = ?", (NODE,))
        connection.execute("DELETE FROM network_samples WHERE node_id = ?", (NODE,))
    connection.close()

    checks = [
        ("SERVER_SAVE", status_create == 200 and created.get("node_id") == NODE, created),
        (
            "ASKLINK_MASKED",
            status_dash == 200
            and asklink.get("id") == "LL-123456"
            and asklink.get("password_configured") is True
            and "password" not in asklink,
            asklink,
        ),
        (
            "ASKLINK_PASSWORD_ENCRYPTED",
            bool(encrypted and encrypted[0])
            and "test-password-123" not in str(encrypted[0]),
            {"encrypted_present": bool(encrypted and encrypted[0])},
        ),
        (
            "ASKLINK_PASSWORD_PRESERVED",
            status_patch == 200
            and server2.get("asklink", {}).get("password_configured") is True,
            server2.get("asklink"),
        ),
        (
            "LIVE_TRAFFIC",
            metrics.get("network_upload_bps") == 1200
            and metrics.get("network_download_bps") == 3400,
            metrics,
        ),
        (
            "TRAFFIC_30D",
            usage.get("upload_bytes", 0) >= 500
            and usage.get("download_bytes", 0) >= 1200,
            usage,
        ),
    ]
    failed = False
    for name, ok, detail in checks:
        print(f"{name}={'PASS' if ok else 'FAIL'} {json.dumps(detail, ensure_ascii=False)}")
        failed = failed or not ok
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
