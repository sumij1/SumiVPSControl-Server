from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import secrets
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse

from core.database import (
    ai_review_ticket,
    create_server_profile,
    dashboard_data,
    delete_bot_config,
    delete_game_server,
    delete_server,
    get_server_connection,
    list_bot_configs,
    list_game_servers,
    list_tickets,
    save_bot_config,
    save_feedback,
    save_game_server,
    save_game_status,
    save_report,
    set_server_power_state,
    update_server_profile,
    update_ticket,
)
from core.ai_judge import infer_log_paths
from core.game import query_game_server, run_rcon_command
from core.geolocation import geolocate_server
from core.notifier import send_wechat_text, send_whatsapp_text
from core.settings import settings
from core.ssh_gateway import (
    HostKeyMismatch,
    HostKeyRequired,
    create_ssh_ticket,
    handle_ssh_websocket,
    run_power_action,
)

router = APIRouter()
FEEDBACK_RE = re.compile(r"^(?:/feedback|#反馈)\s*(.+)$", re.IGNORECASE | re.DOTALL)


def _admin_authorised(authorization: str | None) -> bool:
    if not authorization or not authorization.startswith("Bearer "):
        return False
    supplied = authorization[7:].strip()
    return secrets.compare_digest(supplied, settings.admin_key)


def _agent_authorised(key: str | None) -> bool:
    return bool(key) and secrets.compare_digest(key, settings.agent_key)


def _require_admin(authorization: str | None) -> None:
    if not _admin_authorised(authorization):
        raise HTTPException(status_code=401, detail="无效的 ADMIN_KEY")


async def _platform_config(platform: str) -> dict[str, Any]:
    configs = await list_bot_configs(include_secrets=True)
    for item in configs:
        if item["platform"] == platform and item["enabled"]:
            return item["config"]
    return {}


async def _schedule_restart() -> None:
    await asyncio.sleep(1.5)
    if not settings.service_name:
        return
    try:
        await asyncio.to_thread(
            subprocess.Popen,
            [
                "systemctl",
                "--no-block",
                "restart",
                settings.service_name,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def _extract_feedback(text: str) -> str | None:
    match = FEEDBACK_RE.match(text.strip())
    return match.group(1).strip() if match else None


def _first(payload: dict[str, Any], names: tuple[str, ...], default: str = "") -> str:
    for name in names:
        value = payload.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return default


@router.get("/api/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "service": "fault-hub",
        "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


@router.post("/api/report")
async def report(
    request: Request, x_agent_key: str | None = Header(default=None)
) -> dict[str, Any]:
    if not _agent_authorised(x_agent_key):
        raise HTTPException(status_code=401, detail="无效的 AGENT_KEY")
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="请求体必须是 JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="请求体必须是 JSON 对象")
    try:
        saved, alerts = await save_report(payload)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "ok": True,
        "node_id": saved["node_id"],
        "received_at": saved["received_at"],
        "alerts_triggered": len(alerts),
    }


@router.get("/api/dashboard")
async def dashboard(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    if not _admin_authorised(authorization):
        raise HTTPException(status_code=401, detail="无效的 ADMIN_KEY")
    return await dashboard_data()


@router.get("/api/world-map")
async def world_map(
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_admin(authorization)
    dashboard = await dashboard_data()
    tickets_by_node: dict[str, list[dict[str, Any]]] = {}
    for ticket in dashboard.get("tickets", []):
        if ticket.get("status") in {"unhandled", "in_progress"}:
            tickets_by_node.setdefault(ticket.get("node_id") or "", []).append(ticket)
    nodes: list[dict[str, Any]] = []
    for server in dashboard.get("servers", []):
        node_id = server["node_id"]
        location = server.get("location") or {}
        latitude = location.get("latitude")
        longitude = location.get("longitude")
        location_name = location.get("name") or ""
        source = "manual" if latitude is not None and longitude is not None else ""
        if latitude is None or longitude is None:
            resolved = await geolocate_server(server.get("ssh", {}).get("host") or "")
            if resolved:
                latitude = resolved["latitude"]
                longitude = resolved["longitude"]
                location_name = resolved["location_name"]
                source = resolved["source"]
        unresolved = tickets_by_node.get(node_id, [])
        critical = [
            ticket
            for ticket in unresolved
            if ticket.get("severity") in {1, 2}
        ]
        if critical:
            marker_state = "critical"
        elif not server.get("online"):
            marker_state = "offline"
        elif unresolved:
            marker_state = "warning"
        else:
            marker_state = "online"
        nodes.append(
            {
                "node_id": node_id,
                "name": server.get("display_name") or server.get("hostname"),
                "ip": server.get("ssh", {}).get("host") or "",
                "online": bool(server.get("online")),
                "power_state": server.get("power_state") or "",
                "location_name": location_name or "位置未知",
                "latitude": latitude,
                "longitude": longitude,
                "location_source": source,
                "marker_state": marker_state,
                "unresolved_tickets": len(unresolved),
                "critical_tickets": len(critical),
                "highest_level": min(
                    (
                        int(ticket["severity"])
                        for ticket in unresolved
                        if ticket.get("severity") is not None
                    ),
                    default=None,
                ),
            }
        )
    return {"nodes": nodes, "generated_at": dashboard.get("now")}


@router.post("/api/servers")
async def add_server(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_admin(authorization)
    payload = await request.json()
    resolved_payload = dict(payload)
    log_inference = None
    if not str(payload.get("log_files") or "").strip():
        log_inference = await infer_log_paths(
            payload,
            allow_web_search=bool(payload.get("allow_ai_web_search")),
        )
        if log_inference:
            resolved_payload["log_files"] = ",".join(log_inference["paths"])
    try:
        profile = await create_server_profile(resolved_payload)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    environment = {
        "SERVER_ID": profile["node_id"],
        "SERVER_NAME": str(payload.get("name") or profile["node_id"]),
        "SSH_HOST": str(payload.get("ssh_host") or ""),
        "SSH_PORT": str(payload.get("ssh_port") or 22),
        "SSH_USER": str(payload.get("ssh_user") or "root"),
        "AGENT_KEY": settings.agent_key,
        "HUB_URL": (
            settings.public_base_url
            or str(request.base_url).rstrip("/")
        )
        + "/api/report",
        "CHECK_PORTS": str(payload.get("check_ports") or "22,80,443"),
        "LOG_FILES": str(resolved_payload.get("log_files") or ""),
    }
    installer_base_url = (
        settings.public_base_url or str(request.base_url).rstrip("/")
    )
    prefix = " ".join(
        f"{key}={shlex.quote(value)}" for key, value in environment.items()
    )
    installer_url = f"{installer_base_url}/api/agent/installer.sh"
    install_command = (
        f"sudo env {prefix} bash -c "
        f"\"$(curl -fsSL {shlex.quote(installer_url)})\""
    )
    return {
        **profile,
        "install_command": install_command,
        "log_files": str(resolved_payload.get("log_files") or ""),
        "log_inference": log_inference,
        "inference_notice": (
            "日志目录未填写，以上路径由 AI 推断，可能不准确，请在目标服务器核实。"
            if log_inference
            else None
        ),
    }


@router.patch("/api/servers/{node_id}")
async def edit_server(
    node_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_admin(authorization)
    payload = await request.json()
    try:
        return await update_server_profile(node_id, payload)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/api/servers/{node_id}/power")
async def control_server_power(
    node_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
) -> Any:
    _require_admin(authorization)
    payload = await request.json()
    action = str(payload.get("action") or "")
    try:
        connection = await get_server_connection(node_id)
        if action == "start":
            if not connection["provider_url"]:
                raise HTTPException(
                    status_code=422,
                    detail="未配置 VPS 供应商管理链接，无法远程开机",
                )
            return {
                "ok": True,
                "action": "start",
                "provider_url": connection["provider_url"],
            }
        result = await run_power_action(
            node_id,
            connection["host"],
            connection["port"],
            connection["username"],
            str(payload.get("password") or ""),
            action,
            bool(payload.get("trust_new_host")),
        )
        await set_server_power_state(
            node_id, "rebooting" if action == "reboot" else "off"
        )
        return result
    except HostKeyRequired as exc:
        return JSONResponse(
            status_code=409,
            content={
                "detail": "首次连接需要确认主机指纹",
                "host_key_required": True,
                "fingerprint": exc.fingerprint,
            },
        )
    except HostKeyMismatch as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except HTTPException:
        raise
    except (ValueError, PermissionError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"电源操作失败：{exc}") from exc


@router.delete("/api/servers/{node_id}")
async def remove_server(
    node_id: str,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_admin(authorization)
    await delete_server(node_id)
    return {
        "ok": True,
        "node_id": node_id,
        "message": "服务器、SSH 连接、关联工单和告警已删除",
    }


@router.get("/api/bots")
async def get_bots(
    authorization: str | None = Header(default=None),
) -> list[dict[str, Any]]:
    _require_admin(authorization)
    return await list_bot_configs(include_secrets=False)


@router.post("/api/bots")
async def add_bot(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_admin(authorization)
    payload = await request.json()
    try:
        bot_id = await save_bot_config(payload)
    except (ValueError, TypeError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if payload.get("enabled", True):
        asyncio.create_task(_schedule_restart(), name="restart-after-bot-change")
    return {"ok": True, "id": bot_id, "restart_scheduled": True}


@router.put("/api/bots/{bot_id}")
async def edit_bot(
    bot_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_admin(authorization)
    payload = await request.json()
    try:
        saved_id = await save_bot_config(payload, bot_id)
    except (ValueError, TypeError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if payload.get("enabled", True):
        asyncio.create_task(_schedule_restart(), name="restart-after-bot-change")
    return {"ok": True, "id": saved_id, "restart_scheduled": True}


@router.delete("/api/bots/{bot_id}")
async def remove_bot(
    bot_id: str,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_admin(authorization)
    await delete_bot_config(bot_id)
    asyncio.create_task(_schedule_restart(), name="restart-after-bot-change")
    return {"ok": True, "restart_scheduled": True}


@router.get("/api/tickets")
async def get_tickets(
    status: str | None = Query(default=None),
    severity: str | None = Query(default=None),
    authorization: str | None = Header(default=None),
) -> list[dict[str, Any]]:
    _require_admin(authorization)
    return await list_tickets(status=status, severity=severity)


@router.patch("/api/tickets/{ticket_id}")
async def edit_ticket(
    ticket_id: int,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_admin(authorization)
    payload = await request.json()
    try:
        await update_ticket(ticket_id, payload)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if payload.get("status") in {"resolved", "ignored"}:
        asyncio.create_task(
            ai_review_ticket(ticket_id), name=f"ai-review-ticket-{ticket_id}"
        )
    return {
        "ok": True,
        "ai_review_scheduled": payload.get("status") in {"resolved", "ignored"},
    }


@router.post("/api/tickets/{ticket_id}/ai-check")
async def ai_check_ticket(
    ticket_id: int,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_admin(authorization)
    try:
        return await ai_review_ticket(ticket_id)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/api/tickets/ai-check-all")
async def ai_check_all_tickets(
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_admin(authorization)
    tickets = await list_tickets()
    semaphore = asyncio.Semaphore(3)

    async def review(ticket_id: int) -> dict[str, Any]:
        async with semaphore:
            try:
                return await ai_review_ticket(ticket_id)
            except Exception as exc:
                return {"ok": False, "ticket_id": ticket_id, "error": str(exc)}

    results = await asyncio.gather(*(review(int(item["id"])) for item in tickets))
    return {
        "ok": True,
        "checked": len(results),
        "succeeded": sum(1 for item in results if item.get("ok")),
        "results": results,
    }


@router.get("/api/game-servers")
async def get_game_servers(
    authorization: str | None = Header(default=None),
) -> list[dict[str, Any]]:
    _require_admin(authorization)
    return await list_game_servers(include_secret=False)


@router.post("/api/game-servers")
async def add_game_server(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_admin(authorization)
    payload = await request.json()
    try:
        game_id = await save_game_server(payload)
    except (ValueError, TypeError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"ok": True, "id": game_id}


@router.put("/api/game-servers/{game_id}")
async def edit_game_server(
    game_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_admin(authorization)
    payload = await request.json()
    try:
        saved_id = await save_game_server(payload, game_id)
    except (ValueError, TypeError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"ok": True, "id": saved_id}


@router.delete("/api/game-servers/{game_id}")
async def remove_game_server(
    game_id: str,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_admin(authorization)
    await delete_game_server(game_id)
    return {"ok": True}


async def _find_game_server(game_id: str) -> dict[str, Any]:
    for game in await list_game_servers(include_secret=True):
        if game["id"] == game_id:
            return game
    raise HTTPException(status_code=404, detail="游戏服务器不存在")


@router.post("/api/game-servers/{game_id}/check")
async def check_game_server(
    game_id: str,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_admin(authorization)
    game = await _find_game_server(game_id)
    status = await asyncio.to_thread(
        query_game_server,
        game["game_type"],
        game["host"],
        int(game["query_port"]),
    )
    await save_game_status(game_id, status)
    return status


@router.post("/api/game-servers/{game_id}/command")
async def send_game_command(
    game_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_admin(authorization)
    game = await _find_game_server(game_id)
    payload = await request.json()
    password = str(payload.get("rcon_password") or game.get("rcon_password") or "")
    command = str(payload.get("command") or "")
    if game["console_port"] <= 0:
        raise HTTPException(status_code=422, detail="未配置 RCON 控制台端口")
    try:
        output = await asyncio.to_thread(
            run_rcon_command,
            game["game_type"],
            game["host"],
            int(game["console_port"]),
            password,
            command,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"RCON 执行失败：{exc}") from exc
    return {"ok": True, "output": output}


@router.post("/api/ssh/session")
async def create_ssh_session(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_admin(authorization)
    payload = await request.json()
    try:
        token = await create_ssh_ticket(
            str(payload.get("node_id") or "unknown"),
            str(payload.get("host") or ""),
            int(payload.get("port") or 22),
            str(payload.get("username") or "root"),
        )
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "ticket": token,
        "expires_in": 60,
        "websocket_path": f"/api/ssh/ws/{token}",
    }


@router.websocket("/api/ssh/ws/{ticket}")
async def ssh_websocket(websocket: WebSocket, ticket: str) -> None:
    await handle_ssh_websocket(websocket, ticket)


@router.get("/api/agent/reporter.py")
async def download_agent() -> FileResponse:
    path = settings.base_dir / "agent" / "reporter.py"
    if not path.exists():
        raise HTTPException(status_code=404, detail="探针脚本不存在")
    return FileResponse(
        path,
        media_type="text/x-python; charset=utf-8",
        filename="reporter.py",
    )


@router.get("/api/agent/installer.sh")
async def download_agent_installer() -> FileResponse:
    path = settings.base_dir / "scripts" / "install_agent.sh"
    if not path.exists():
        raise HTTPException(status_code=404, detail="探针安装脚本不存在")
    return FileResponse(
        path,
        media_type="text/x-shellscript; charset=utf-8",
        filename="install_agent.sh",
    )


@router.post("/webhook/wechat")
async def wechat_webhook(
    request: Request,
    x_webhook_secret: str | None = Header(default=None),
    x_auth_token: str | None = Header(default=None),
) -> dict[str, Any]:
    wechat_config = await _platform_config("wechat")
    expected_secret = (
        str(wechat_config.get("webhook_secret") or "")
        or settings.wechat_webhook_secret
    )
    if expected_secret:
        supplied = x_webhook_secret or x_auth_token or ""
        if not secrets.compare_digest(supplied, expected_secret):
            raise HTTPException(status_code=401, detail="微信 Webhook 鉴权失败")
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="请求体必须是 JSON") from exc
    if not isinstance(payload, dict):
        return {"ok": True, "ignored": True}

    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    text = _first(
        data,
        ("content", "text", "msg", "message", "body", "messageText"),
    )
    if isinstance(data.get("message"), dict):
        text = _first(data["message"], ("content", "text", "msg", "body"), text)
    content = _extract_feedback(text)
    if not content:
        return {"ok": True, "ignored": True}

    session_id = _first(
        data,
        ("roomId", "roomid", "groupId", "group_id", "chat_id", "chatId", "from", "to"),
        "unknown",
    )
    sender = _first(
        data,
        ("senderName", "sender_name", "nickname", "fromName", "sender", "from"),
        "微信用户",
    )
    feedback_id = await save_feedback("微信", session_id, sender, content)
    if session_id != "unknown":
        await send_wechat_text("已收到反馈", session_id)
    return {"ok": True, "feedback_id": feedback_id, "reply": "已收到反馈"}


def _verify_whatsapp_signature(
    body: bytes, signature: str | None, app_secret: str
) -> bool:
    if not app_secret:
        return True
    if not signature or not signature.startswith("sha256="):
        return False
    expected = hmac.new(
        app_secret.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()
    return secrets.compare_digest(signature[7:], expected)


@router.get("/webhook/whatsapp")
async def whatsapp_verify(
    mode: str = Query(default="", alias="hub.mode"),
    token: str = Query(default="", alias="hub.verify_token"),
    challenge: str = Query(default="", alias="hub.challenge"),
) -> Any:
    config = await _platform_config("whatsapp")
    expected_token = (
        str(config.get("verify_token") or "") or settings.whatsapp_verify_token
    )
    if (
        mode == "subscribe"
        and expected_token
        and secrets.compare_digest(token, expected_token)
    ):
        return JSONResponse(content=int(challenge) if challenge.isdigit() else challenge)
    raise HTTPException(status_code=403, detail="WhatsApp Webhook 验证失败")


@router.post("/webhook/whatsapp")
async def whatsapp_webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(default=None),
) -> dict[str, Any]:
    raw = await request.body()
    config = await _platform_config("whatsapp")
    app_secret = str(config.get("app_secret") or "") or settings.whatsapp_app_secret
    if not _verify_whatsapp_signature(raw, x_hub_signature_256, app_secret):
        raise HTTPException(status_code=401, detail="WhatsApp 签名校验失败")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="请求体必须是 JSON") from exc

    saved = 0
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            contacts = value.get("contacts", [])
            names = {
                contact.get("wa_id"): contact.get("profile", {}).get("name")
                for contact in contacts
                if isinstance(contact, dict)
            }
            for message in value.get("messages", []):
                if not isinstance(message, dict):
                    continue
                text = str(message.get("text", {}).get("body", ""))
                content = _extract_feedback(text)
                if not content:
                    continue
                sender_id = str(message.get("from", "unknown"))
                sender = str(names.get(sender_id) or sender_id)
                await save_feedback("WhatsApp", sender_id, sender, content)
                await send_whatsapp_text("已收到反馈", sender_id)
                saved += 1
    return {"ok": True, "saved": saved}
