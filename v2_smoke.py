#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import urllib.error
import urllib.request

import websockets
from dotenv import dotenv_values


ENV = dotenv_values(".env")
BASE = ENV.get("TEST_HUB_URL") or ENV.get("PUBLIC_BASE_URL") or "http://127.0.0.1:8080"
WS_BASE = BASE.replace("https://", "wss://").replace("http://", "ws://")
ADMIN = ENV["ADMIN_KEY"]
AGENT = ENV["AGENT_KEY"]


def request(
    path: str,
    method: str = "GET",
    payload: dict | None = None,
    agent: bool = False,
) -> tuple[int, object]:
    body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if agent:
        headers["X-Agent-Key"] = AGENT
    else:
        headers["Authorization"] = f"Bearer {ADMIN}"
    req = urllib.request.Request(
        BASE + path,
        data=body,
        method=method,
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = exc.code
    try:
        parsed = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        parsed = raw.decode("utf-8", "replace")
    return status, parsed


async def ssh_smoke() -> tuple[bool, str]:
    password = os.getenv("TEST_SSH_PASSWORD", "")
    if not password:
        return True, "SKIPPED: TEST_SSH_PASSWORD not set"
    status, first = request(
        "/api/ssh/session",
        "POST",
        {
            "node_id": "ops-hub-gcp-1",
            "host": "127.0.0.1",
            "port": 22,
            "username": "root",
        },
    )
    if status != 200 or not isinstance(first, dict):
        return False, f"ticket1={status}"
    path = first["websocket_path"]
    ws_url = WS_BASE + path
    async with websockets.connect(ws_url, open_timeout=15) as socket:
        await socket.recv()
        await socket.send(
            json.dumps(
                {
                    "type": "auth",
                    "password": password,
                    "trust_new_host": False,
                }
            )
        )
        message = json.loads(await socket.recv())
    if message.get("type") != "host_key_required":
        return False, f"unexpected first auth: {message}"

    status, second = request(
        "/api/ssh/session",
        "POST",
        {
            "node_id": "ops-hub-gcp-1",
            "host": "127.0.0.1",
            "port": 22,
            "username": "root",
        },
    )
    if status != 200 or not isinstance(second, dict):
        return False, f"ticket2={status}"
    ws_url = WS_BASE + second["websocket_path"]
    output = ""
    async with websockets.connect(ws_url, open_timeout=15) as socket:
        await socket.recv()
        await socket.send(
            json.dumps(
                {
                    "type": "auth",
                    "password": password,
                    "trust_new_host": True,
                }
            )
        )
        await asyncio.sleep(1)
        await socket.send(
            json.dumps({"type": "input", "data": "printf 'V2_SSH_OK\\n'\r"})
        )
        deadline = asyncio.get_running_loop().time() + 12
        while asyncio.get_running_loop().time() < deadline:
            try:
                message = json.loads(await asyncio.wait_for(socket.recv(), timeout=2))
            except asyncio.TimeoutError:
                break
            if message.get("type") == "output":
                output += message.get("data", "")
                if "V2_SSH_OK" in output:
                    return True, output[-300:]
            elif message.get("type") == "error":
                return False, message.get("message", "SSH error")
    return False, output[-500:]


async def main() -> int:
    checks: list[tuple[str, bool, object]] = []
    status, dashboard = request("/api/dashboard")
    checks.append(
        (
            "DASHBOARD_V2",
            status == 200
            and isinstance(dashboard, dict)
            and dashboard["servers"]
            and "memory_total_mb" in dashboard["servers"][0]["metrics"],
            dashboard["summary"] if isinstance(dashboard, dict) else dashboard,
        )
    )

    server_payload = {
        "name": "v2-smoke-node",
        "remark": "V2 自测节点",
        "ssh_host": "192.0.2.44",
        "ssh_port": 22,
        "ssh_user": "root",
        "check_ports": "22,80",
        "log_files": "/tmp/smoke.log",
    }
    status, server = request("/api/servers", "POST", server_payload)
    checks.append(
        (
            "SERVER_PROFILE",
            status == 200
            and isinstance(server, dict)
            and "installer.sh" in server.get("install_command", ""),
            server,
        )
    )

    status, bot = request(
        "/api/bots",
        "POST",
        {
            "platform": "telegram",
            "remark": "V2 禁用自测 Bot",
            "enabled": False,
            "config": {"token": "123456:TEST_ONLY", "targets": ["10001"]},
        },
    )
    checks.append(("BOT_ENCRYPTED_SAVE", status == 200 and bot.get("ok"), bot))
    status, bots = request("/api/bots")
    checks.append(
        (
            "BOT_SECRET_MASK",
            status == 200
            and isinstance(bots, list)
            and all(item["config"].get("token") != "123456:TEST_ONLY" for item in bots),
            {"count": len(bots) if isinstance(bots, list) else -1},
        )
    )

    status, report = request(
        "/api/report",
        "POST",
        {
            "node_id": "v2-smoke-node",
            "hostname": "v2-smoke-node",
            "ip": "192.0.2.44",
            "cpu_percent": 96.2,
            "memory_percent": 91.1,
            "disk_percent": 80.2,
            "report_latency_ms": 910,
            "memory_total_mb": 2048,
            "memory_used_mb": 1865,
            "disk_total_gb": 100,
            "disk_used_gb": 80.2,
            "ports": {"443": {"alive": False, "latency_ms": 20}},
            "errors": [{"message": "Exception: v2 smoke"}],
        },
        agent=True,
    )
    checks.append(("REPORT_V2", status == 200 and report.get("ok"), report))
    await asyncio.sleep(5)
    status, tickets = request("/api/tickets")
    smoke_ticket = next(
        (
            item
            for item in tickets
            if item["node_id"] == "v2-smoke-node"
        ),
        None,
    )
    checks.append(
        (
            "AUTO_TICKET_LEVEL",
            status == 200
            and smoke_ticket is not None
            and smoke_ticket.get("analysis_engine") == "ai"
            and smoke_ticket.get("severity") == 1,
            {"tickets": len(tickets) if isinstance(tickets, list) else tickets},
        )
    )
    if smoke_ticket:
        status, update = request(
            f"/api/tickets/{smoke_ticket['id']}",
            "PATCH",
            {"status": "resolved", "severity": 3, "remark": "V2 自测处理"},
        )
        checks.append(("TICKET_UPDATE", status == 200 and update.get("ok"), update))

    status, feedback = request(
        "/webhook/wechat",
        "POST",
        {
            "senderName": "AI 自测用户",
            "roomId": "ai-smoke-room",
            "content": "#反馈 登录后页面偶尔白屏，刷新后恢复",
        },
    )
    await asyncio.sleep(4)
    status_tickets, ai_feedback_tickets = request("/api/tickets")
    feedback_ticket = next(
        (
            item
            for item in ai_feedback_tickets
            if item["source"].startswith("微信/ai-smoke-room")
        ),
        None,
    )
    checks.append(
        (
            "AI_FEEDBACK_TICKET",
            status == 200
            and feedback_ticket is not None
            and feedback_ticket.get("analysis_engine") == "ai",
            feedback_ticket,
        )
    )

    status, game = request(
        "/api/game-servers",
        "POST",
        {
            "name": "V2 Smoke Game",
            "game_type": "minecraft",
            "host": "example.com",
            "query_port": 65534,
            "console_port": 0,
            "rcon_password": "",
        },
    )
    game_id = game.get("id") if isinstance(game, dict) else None
    checks.append(("GAME_SAVE", status == 200 and bool(game_id), game))
    if game_id:
        status, game_status = request(
            f"/api/game-servers/{game_id}/check", "POST", {}
        )
        checks.append(
            (
                "GAME_QUERY_OFFLINE_HANDLED",
                status == 200 and isinstance(game_status, dict) and "online" in game_status,
                game_status,
            )
        )
        status, removed = request(f"/api/game-servers/{game_id}", "DELETE")
        checks.append(("GAME_DELETE", status == 200 and removed.get("ok"), removed))

    ssh_ok, ssh_detail = await ssh_smoke()
    checks.append(("BROWSER_SSH", ssh_ok, ssh_detail))

    connection = sqlite3.connect(ENV.get("DATABASE_PATH") or "data/ops_hub.sqlite3")
    with connection:
        connection.execute(
            "DELETE FROM alerts WHERE node_id = 'v2-smoke-node'"
        )
        connection.execute(
            "DELETE FROM alert_cooldowns WHERE alert_key LIKE '%v2-smoke-node%'"
        )
        connection.execute(
            "DELETE FROM tickets WHERE node_id = 'v2-smoke-node'"
        )
        connection.execute(
            "DELETE FROM tickets WHERE source LIKE '%ai-smoke-room%'"
        )
        connection.execute(
            "DELETE FROM feedbacks WHERE session_id = 'ai-smoke-room'"
        )
        connection.execute(
            "DELETE FROM server_profiles WHERE node_id = 'v2-smoke-node'"
        )
        connection.execute("DELETE FROM servers WHERE node_id = 'v2-smoke-node'")
        if isinstance(bot, dict) and bot.get("id"):
            connection.execute("DELETE FROM bot_configs WHERE id = ?", (bot["id"],))
    connection.close()

    failed = False
    for name, ok, detail in checks:
        print(f"{name}={'PASS' if ok else 'FAIL'} {detail}")
        failed = failed or not ok
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
