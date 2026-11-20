from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import secrets
import socket
import time
from typing import Any

import paramiko
from fastapi import WebSocket, WebSocketDisconnect

from core.database import get_server_host_key, pin_server_host_key

logger = logging.getLogger("ops_hub.ssh")

_sessions: dict[str, dict[str, Any]] = {}
_session_lock = asyncio.Lock()
_active_limit = asyncio.Semaphore(2)


class HostKeyRequired(RuntimeError):
    def __init__(self, fingerprint: str) -> None:
        super().__init__("首次连接需要确认 SSH 主机指纹")
        self.fingerprint = fingerprint


class HostKeyMismatch(RuntimeError):
    pass


def _fingerprint(key: paramiko.PKey) -> str:
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


async def create_ssh_ticket(
    node_id: str,
    host: str,
    port: int,
    username: str,
) -> str:
    if not host or len(host) > 255:
        raise ValueError("SSH 主机地址无效")
    if port < 1 or port > 65535:
        raise ValueError("SSH 端口无效")
    if not username or len(username) > 80:
        raise ValueError("SSH 用户名无效")
    token = secrets.token_urlsafe(32)
    async with _session_lock:
        now = time.time()
        for current, session in list(_sessions.items()):
            if session["expires_at"] <= now:
                _sessions.pop(current, None)
        _sessions[token] = {
            "node_id": node_id,
            "host": host,
            "port": port,
            "username": username,
            "expires_at": now + 60,
        }
    return token


async def _connect_target(
    websocket: WebSocket,
    session: dict[str, Any],
    password: str,
    trust_new_host: bool,
) -> tuple[paramiko.Transport, paramiko.Channel] | None:
    host = session["host"]
    port = session["port"]
    username = session["username"]
    node_id = session["node_id"]

    def open_transport() -> paramiko.Transport:
        sock = socket.create_connection((host, port), timeout=12)
        transport = paramiko.Transport(sock)
        transport.banner_timeout = 12
        transport.auth_timeout = 12
        transport.start_client(timeout=12)
        return transport

    try:
        transport = await asyncio.to_thread(open_transport)
    except Exception as exc:
        await websocket.send_json(
            {"type": "error", "message": f"无法连接 SSH：{exc}"}
        )
        return None


async def run_power_action(
    node_id: str,
    host: str,
    port: int,
    username: str,
    password: str,
    action: str,
    trust_new_host: bool,
) -> dict[str, Any]:
    if action not in {"reboot", "shutdown"}:
        raise ValueError("不支持的电源操作")

    def connect() -> paramiko.Transport:
        sock = socket.create_connection((host, port), timeout=12)
        transport = paramiko.Transport(sock)
        transport.banner_timeout = 12
        transport.auth_timeout = 12
        transport.start_client(timeout=12)
        return transport

    transport = await asyncio.to_thread(connect)
    try:
        fingerprint = _fingerprint(transport.get_remote_server_key())
        expected = await get_server_host_key(node_id)
        if expected and not secrets.compare_digest(expected, fingerprint):
            raise HostKeyMismatch(
                "SSH 主机指纹发生变化，电源操作已阻止"
            )
        if not expected and not trust_new_host:
            raise HostKeyRequired(fingerprint)
        if not expected and trust_new_host:
            await pin_server_host_key(node_id, fingerprint)
        await asyncio.to_thread(transport.auth_password, username, password)
        if not transport.is_authenticated():
            raise PermissionError("SSH 密码认证失败")
        command = (
            "nohup sh -c 'sleep 2; systemctl reboot' "
            ">/dev/null 2>&1 &"
            if action == "reboot"
            else "nohup sh -c 'sleep 2; systemctl poweroff' "
            ">/dev/null 2>&1 &"
        )
        channel = transport.open_session()
        channel.exec_command(command)
        return {
            "ok": True,
            "action": action,
            "node_id": node_id,
            "fingerprint": fingerprint,
            "message": "服务器将在约 2 秒后重启"
            if action == "reboot"
            else "服务器将在约 2 秒后关机",
        }
    finally:
        transport.close()

    try:
        remote_key = transport.get_remote_server_key()
        fingerprint = _fingerprint(remote_key)
        expected = await get_server_host_key(node_id)
        if expected and not secrets.compare_digest(expected, fingerprint):
            await websocket.send_json(
                {
                    "type": "error",
                    "message": "SSH 主机指纹已变化，连接已阻止。请先核实服务器是否重装或遭到篡改。",
                    "fingerprint": fingerprint,
                    "expected": expected,
                }
            )
            transport.close()
            return None
        if not expected and not trust_new_host:
            await websocket.send_json(
                {
                    "type": "host_key_required",
                    "message": "首次连接需要确认 SSH 主机指纹。",
                    "fingerprint": fingerprint,
                }
            )
            transport.close()
            return None
        if not expected and trust_new_host:
            await pin_server_host_key(node_id, fingerprint)
            await websocket.send_json(
                {"type": "host_key_pinned", "fingerprint": fingerprint}
            )
        if not transport.is_authenticated():
            await asyncio.to_thread(transport.auth_password, username, password)
        if not transport.is_authenticated():
            await websocket.send_json(
                {"type": "error", "message": "SSH 密码认证失败"}
            )
            transport.close()
            return None
        channel = await asyncio.to_thread(transport.open_session)
        await asyncio.to_thread(
            channel.get_pty,
            term="xterm-256color",
            width=110,
            height=32,
        )
        await asyncio.to_thread(channel.invoke_shell)
        channel.settimeout(0.5)
        await websocket.send_json(
            {
                "type": "connected",
                "message": f"已连接 {username}@{host}:{port}",
                "fingerprint": fingerprint,
            }
        )
        return transport, channel
    except Exception as exc:
        transport.close()
        await websocket.send_json({"type": "error", "message": f"SSH 认证失败：{exc}"})
        return None


async def handle_ssh_websocket(websocket: WebSocket, token: str) -> None:
    async with _session_lock:
        session = _sessions.pop(token, None)
    if not session or session["expires_at"] < time.time():
        await websocket.close(code=4401)
        return
    if _active_limit.locked():
        await websocket.accept()
        await websocket.send_json({"type": "error", "message": "当前 SSH 会话已满"})
        await websocket.close(code=4429)
        return
    await _active_limit.acquire()
    await websocket.accept()
    try:
        await websocket.send_json(
            {"type": "auth_required", "message": "请输入目标服务器密码"}
        )
        first = await websocket.receive_json()
        if first.get("type") != "auth":
            await websocket.send_json({"type": "error", "message": "缺少 SSH 认证信息"})
            return
        password = str(first.get("password") or "")
        trust_new_host = bool(first.get("trust_new_host"))
        connected = await _connect_target(websocket, session, password, trust_new_host)
        if connected is None:
            return
        transport, channel = connected

        async def pump_output() -> None:
            while transport.is_active() and not channel.closed:
                try:
                    data = await asyncio.to_thread(channel.recv, 8192)
                except socket.timeout:
                    continue
                except Exception:
                    break
                if not data:
                    break
                await websocket.send_json(
                    {
                        "type": "output",
                        "data": data.decode("utf-8", "replace"),
                    }
                )

        output_task = asyncio.create_task(pump_output(), name="ssh-terminal-output")
        while not output_task.done():
            message = await websocket.receive_json()
            message_type = message.get("type")
            if message_type == "input":
                channel.send(str(message.get("data") or ""))
            elif message_type == "resize":
                cols = max(20, min(300, int(message.get("cols") or 110)))
                rows = max(8, min(120, int(message.get("rows") or 32)))
                channel.resize_pty(width=cols, height=rows)
            elif message_type == "close":
                break
        output_task.cancel()
        try:
            await output_task
        except asyncio.CancelledError:
            pass
        channel.close()
        transport.close()
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.exception("SSH websocket failed")
        try:
            await websocket.send_json({"type": "error", "message": str(exc)})
        except Exception:
            pass
    finally:
        _active_limit.release()
        try:
            await websocket.close()
        except Exception:
            pass
