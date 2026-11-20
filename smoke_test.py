#!/usr/bin/env python3
from __future__ import annotations

import json
import urllib.error
import urllib.request

from dotenv import dotenv_values


ENV = dotenv_values(".env")
BASE = ENV.get("TEST_HUB_URL") or ENV.get("PUBLIC_BASE_URL") or "http://127.0.0.1:8080"


def request(
    path: str,
    method: str = "GET",
    payload: dict | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], object]:
    body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path,
        data=body,
        method=method,
        headers={"Accept": "application/json", **(headers or {})},
    )
    if body is not None:
        req.add_header("Content-Type", "application/json; charset=utf-8")
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            raw = response.read()
            status = response.status
            response_headers = dict(response.headers)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = exc.code
        response_headers = dict(exc.headers)
    try:
        parsed = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        parsed = raw.decode("utf-8", "replace")
    return status, response_headers, parsed


def main() -> int:
    admin = ENV["ADMIN_KEY"]
    agent = ENV["AGENT_KEY"]
    assert admin and agent

    checks: list[tuple[str, bool, object]] = []

    status, _, body = request("/api/dashboard")
    checks.append(("ADMIN_401", status == 401, body))

    normal = {
        "node_id": "smoke-test-normal",
        "hostname": "Smoke Test Node",
        "ip": "192.0.2.10",
        "cpu_percent": 12.3,
        "memory_percent": 34.5,
        "disk_percent": 45.6,
        "uptime_seconds": 3600,
        "ports": {"22": True},
        "errors": [],
    }
    status, _, body = request(
        "/api/report", "POST", normal, {"X-Agent-Key": agent}
    )
    checks.append(("VALID_REPORT", status == 200, body))

    alerting = {
        "node_id": "smoke-test-alert",
        "hostname": "Alert Test Node",
        "ip": "192.0.2.11",
        "cpu_percent": 95.2,
        "memory_percent": 91.1,
        "disk_percent": 90.3,
        "uptime_seconds": 7200,
        "ports": {"443": False},
        "errors": [{"message": "Exception: deployment self-test"}],
    }
    status, _, body = request(
        "/api/report", "POST", alerting, {"X-Agent-Key": agent}
    )
    checks.append(("ALERT_REPORT", status == 200, body))

    status, _, dashboard = request(
        "/api/dashboard", headers={"Authorization": f"Bearer {admin}"}
    )
    assert isinstance(dashboard, dict)
    node_ids = {item["node_id"] for item in dashboard["servers"]}
    checks.append(
        (
            "DASHBOARD_DATA",
            status == 200
            and {"smoke-test-normal", "smoke-test-alert"} <= node_ids
            and len(dashboard["alerts"]) >= 1,
            {
                "servers": len(dashboard["servers"]),
                "alerts": len(dashboard["alerts"]),
            },
        )
    )

    feedback_payload = {
        "senderName": "自测用户",
        "roomId": "self-test-room",
        "content": "#反馈 部署自测反馈",
    }
    status, _, body = request("/webhook/wechat", "POST", feedback_payload)
    checks.append(("WECHAT_WEBHOOK", status == 200, body))

    status, _, dashboard = request(
        "/api/dashboard", headers={"Authorization": f"Bearer {admin}"}
    )
    assert isinstance(dashboard, dict)
    checks.append(
        (
            "FEEDBACK_VISIBLE",
            any(item["content"] == "部署自测反馈" for item in dashboard["feedbacks"]),
            {"feedbacks": len(dashboard["feedbacks"])},
        )
    )

    test_origin = (ENV.get("TEST_CORS_ORIGIN") or "").strip()
    if test_origin:
        status, headers, body = request(
            "/api/dashboard",
            "OPTIONS",
            headers={
                "Origin": test_origin,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization",
            },
        )
        checks.append(
            (
                "CORS_PREFLIGHT",
                status == 200
                and headers.get("Access-Control-Allow-Origin") == test_origin,
                {
                    "status": status,
                    "allow_origin": headers.get("Access-Control-Allow-Origin"),
                },
            )
        )
    else:
        checks.append(
            ("CORS_PREFLIGHT", True, "SKIP: TEST_CORS_ORIGIN is empty")
        )

    failed = False
    for name, ok, detail in checks:
        print(f"{name}={'PASS' if ok else 'FAIL'} {detail}")
        failed = failed or not ok
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
