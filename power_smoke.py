#!/usr/bin/env python3
from __future__ import annotations

import json
import sqlite3
import urllib.error
import urllib.request

from dotenv import dotenv_values


ENV = dotenv_values(".env")
BASE = ENV.get("TEST_HUB_URL") or ENV.get("PUBLIC_BASE_URL") or "http://127.0.0.1:8080"


def request(path: str, method: str = "GET", payload: dict | None = None) -> tuple[int, object]:
    body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path,
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {ENV['ADMIN_KEY']}",
            "Accept": "application/json",
            **({"Content-Type": "application/json"} if body else {}),
        },
    )
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
            "name": "power-smoke",
            "remark": "电源测试",
            "ssh_host": "127.0.0.1",
            "ssh_port": 22,
            "ssh_user": "root",
            "provider_url": "https://console.example.com/instance/power-smoke",
            "check_ports": "22",
            "log_files": "/var/log/syslog",
        },
    )
    status_start, started = request(
        "/api/servers/power-smoke/power", "POST", {"action": "start"}
    )
    status_fingerprint, fingerprint = request(
        "/api/servers/power-smoke/power",
        "POST",
        {
            "action": "reboot",
            "password": "intentionally-not-a-real-password",
            "trust_new_host": False,
        },
    )
    checks = [
        (
            "PROVIDER_URL_SAVE",
            status == 200
            and created.get("node_id") == "power-smoke",
            created,
        ),
        (
            "START_USES_PROVIDER",
            status_start == 200
            and started.get("provider_url") == "https://console.example.com/instance/power-smoke",
            started,
        ),
        (
            "HOST_KEY_CONFIRMATION",
            status_fingerprint == 409
            and fingerprint.get("host_key_required")
            and bool(fingerprint.get("fingerprint")),
            fingerprint,
        ),
    ]

    request("/api/servers/power-smoke", "DELETE")
    connection = sqlite3.connect(ENV.get("DATABASE_PATH") or "data/ops_hub.sqlite3")
    with connection:
        connection.execute("DELETE FROM server_removals WHERE node_id = 'power-smoke'")
    connection.close()

    failed = False
    for name, ok, detail in checks:
        print(f"{name}={'PASS' if ok else 'FAIL'} {json.dumps(detail, ensure_ascii=False)}")
        failed = failed or not ok
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
