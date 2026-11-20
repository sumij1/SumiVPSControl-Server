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
        with urllib.request.urlopen(req, timeout=45) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = exc.code
    return status, json.loads(raw) if raw else None


def main() -> int:
    status, dashboard = request("/api/dashboard")
    server = (dashboard.get("servers") or [{}])[0]
    metrics = server.get("metrics", {})
    checks = [
        (
            "GPU_FIELD",
            "gpu_available" in metrics and "gpu_percent" in metrics,
            {
                "gpu_available": metrics.get("gpu_available"),
                "gpu_percent": metrics.get("gpu_percent"),
                "gpus": server.get("gpus"),
            },
        ),
        (
            "DISK_INVENTORY",
            "disks" in server
            and isinstance(server.get("disks"), list)
            and float(metrics.get("disk_total_gb") or 0) > 0,
            {
                "disk_total_gb": metrics.get("disk_total_gb"),
                "disks": server.get("disks"),
            },
        ),
    ]

    status, created = request(
        "/api/servers",
        "POST",
        {
            "name": "ai-log-smoke",
            "remark": "Minecraft Java 服务器，使用 Nginx",
            "ssh_host": "minecraft.example.com",
            "ssh_port": 22,
            "ssh_user": "root",
            "check_ports": "22,80,25565",
            "log_files": "",
            "allow_ai_web_search": False,
        },
    )
    checks.append(
        (
            "AI_LOG_INFERENCE",
            status == 200
            and isinstance(created.get("log_inference"), dict)
            and bool(created.get("log_files")),
            {
                "log_files": created.get("log_files") if isinstance(created, dict) else None,
                "analysis": created.get("log_inference", {}).get("analysis")
                if isinstance(created, dict)
                else None,
            },
        )
    )

    connection = sqlite3.connect(ENV.get("DATABASE_PATH") or "data/ops_hub.sqlite3")
    with connection:
        connection.execute("DELETE FROM server_profiles WHERE node_id = 'ai-log-smoke'")
    connection.close()

    failed = False
    for name, ok, detail in checks:
        print(f"{name}={'PASS' if ok else 'FAIL'} {json.dumps(detail, ensure_ascii=False)}")
        failed = failed or not ok
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
