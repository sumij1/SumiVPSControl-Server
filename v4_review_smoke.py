#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import urllib.error
import urllib.request

from dotenv import dotenv_values


ENV = dotenv_values(".env")
BASE = ENV.get("TEST_HUB_URL") or ENV.get("PUBLIC_BASE_URL") or "http://127.0.0.1:8080"
NODE = "ai-review-smoke"


def request(
    path: str,
    method: str = "GET",
    payload: dict | None = None,
    agent: bool = False,
) -> tuple[int, object]:
    body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    headers = {
        "Accept": "application/json",
        **({"Content-Type": "application/json"} if body else {}),
    }
    if agent:
        headers["X-Agent-Key"] = ENV["AGENT_KEY"]
    else:
        headers["Authorization"] = f"Bearer {ENV['ADMIN_KEY']}"
    req = urllib.request.Request(BASE + path, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=45) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = exc.code
    return status, json.loads(raw) if raw else None


def incident() -> dict | None:
    status, tickets = request("/api/tickets")
    if status != 200 or not isinstance(tickets, list):
        return None
    return next((item for item in tickets if item["fingerprint"] == f"incident:{NODE}"), None)


def report_payload(cpu: float) -> dict:
    return {
        "node_id": NODE,
        "hostname": NODE,
        "cpu_percent": cpu,
        "memory_percent": 20,
        "disk_percent": 30,
        "ports": {"22": True, "443": False},
        "errors": [],
    }


async def main() -> int:
    request("/api/report", "POST", report_payload(95), agent=True)
    await asyncio.sleep(4)
    ticket = incident()
    if not ticket:
        print("INITIAL_TICKET=FAIL")
        return 1

    status, updated = request(
        f"/api/tickets/{ticket['id']}",
        "PATCH",
        {
            "status": "resolved",
            "severity": ticket["severity"],
            "remark": "计划维护：管理员正在迁移服务，预计20分钟内恢复，请暂时忽略相同端口故障。",
        },
    )
    await asyncio.sleep(5)
    reviewed = incident() or {}
    checks = [
        ("STATUS_SAVE", status == 200 and updated.get("ai_review_scheduled"), updated),
        (
            "IMMEDIATE_AI_REVIEW",
            bool(reviewed.get("ai_reviewed_at"))
            and reviewed.get("analysis_engine") == "ai",
            {
                "analysis": reviewed.get("analysis"),
                "ignore_until": reviewed.get("ai_ignore_until"),
                "ignore_reason": reviewed.get("ai_ignore_reason"),
            },
        ),
        (
            "REMARK_REWRITES_ANALYSIS",
            "计划维护" in str(reviewed.get("analysis") or "")
            or bool(reviewed.get("ai_ignore_until")),
            reviewed.get("analysis"),
        ),
    ]

    request("/api/report", "POST", report_payload(99), agent=True)
    await asyncio.sleep(1)
    after = incident() or {}
    checks.append(
        (
            "IGNORE_DURING_WINDOW",
            after.get("status") == "resolved",
            {"status": after.get("status"), "ignore_until": after.get("ai_ignore_until")},
        )
    )
    status, single = request(f"/api/tickets/{ticket['id']}/ai-check", "POST")
    checks.append(
        (
            "MANUAL_SINGLE_CHECK",
            status == 200 and single.get("ok") and single.get("ticket_id") == ticket["id"],
            single,
        )
    )

    connection = sqlite3.connect(ENV.get("DATABASE_PATH") or "data/ops_hub.sqlite3")
    with connection:
        connection.execute("DELETE FROM alerts WHERE node_id = ?", (NODE,))
        connection.execute(
            "DELETE FROM alert_cooldowns WHERE alert_key LIKE ?", (f"%{NODE}%",)
        )
        connection.execute("DELETE FROM tickets WHERE node_id = ?", (NODE,))
        connection.execute("DELETE FROM server_profiles WHERE node_id = ?", (NODE,))
        connection.execute("DELETE FROM servers WHERE node_id = ?", (NODE,))
        keys = [f"cpu:{NODE}", f"port:{NODE}:443"]
        problem_key = hashlib.sha256(
            json.dumps(sorted(keys), ensure_ascii=False).encode()
        ).hexdigest()
        connection.execute("DELETE FROM ai_cache WHERE problem_key = ?", (problem_key,))
    connection.close()

    failed = False
    for name, ok, detail in checks:
        print(f"{name}={'PASS' if ok else 'FAIL'} {json.dumps(detail, ensure_ascii=False)}")
        failed = failed or not ok
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
