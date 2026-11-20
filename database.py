from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from core.notifier import broadcast_alerts
from core.secrets import decrypt_json, encrypt_json
from core.settings import settings

logger = logging.getLogger("ops_hub.database")
_write_lock = asyncio.Lock()
_ai_lock = asyncio.Lock()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


@asynccontextmanager
async def connect() -> AsyncIterator[aiosqlite.Connection]:
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(settings.database_path)
    try:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
        await db.execute("PRAGMA busy_timeout=5000")
        await db.execute("PRAGMA foreign_keys=ON")
        yield db
    finally:
        await db.close()


async def init_db() -> None:
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    async with connect() as db:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS servers (
                node_id TEXT PRIMARY KEY,
                hostname TEXT NOT NULL,
                ip TEXT NOT NULL DEFAULT '',
                cpu_percent REAL NOT NULL DEFAULT 0,
                memory_percent REAL NOT NULL DEFAULT 0,
                disk_percent REAL NOT NULL DEFAULT 0,
                uptime_seconds INTEGER NOT NULL DEFAULT 0,
                last_seen TEXT NOT NULL,
                report_json TEXT NOT NULL DEFAULT '{}',
                report_latency_ms REAL NOT NULL DEFAULT 0,
                memory_total_mb REAL NOT NULL DEFAULT 0,
                memory_used_mb REAL NOT NULL DEFAULT 0,
                swap_total_mb REAL NOT NULL DEFAULT 0,
                swap_used_mb REAL NOT NULL DEFAULT 0,
                disk_total_gb REAL NOT NULL DEFAULT 0,
                disk_used_gb REAL NOT NULL DEFAULT 0,
                disk_free_gb REAL NOT NULL DEFAULT 0,
                disks_json TEXT NOT NULL DEFAULT '[]',
                gpu_available INTEGER NOT NULL DEFAULT 0,
                gpu_percent REAL NOT NULL DEFAULT 0,
                gpus_json TEXT NOT NULL DEFAULT '[]',
                network_upload_bps REAL NOT NULL DEFAULT 0,
                network_download_bps REAL NOT NULL DEFAULT 0,
                network_upload_total_bytes INTEGER NOT NULL DEFAULT 0,
                network_download_total_bytes INTEGER NOT NULL DEFAULT 0,
                cpu_cores INTEGER NOT NULL DEFAULT 0,
                cpu_model TEXT NOT NULL DEFAULT '',
                load1 REAL NOT NULL DEFAULT 0,
                load5 REAL NOT NULL DEFAULT 0,
                load15 REAL NOT NULL DEFAULT 0,
                os_info TEXT NOT NULL DEFAULT '',
                ssh_host TEXT NOT NULL DEFAULT '',
                ssh_port INTEGER NOT NULL DEFAULT 22,
                ssh_user TEXT NOT NULL DEFAULT 'root',
                power_state TEXT NOT NULL DEFAULT '',
                host_key_fingerprint TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS port_status (
                node_id TEXT NOT NULL,
                port INTEGER NOT NULL,
                alive INTEGER NOT NULL,
                checked_at TEXT NOT NULL,
                latency_ms REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (node_id, port),
                FOREIGN KEY (node_id) REFERENCES servers(node_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS error_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                node_id TEXT NOT NULL,
                message TEXT NOT NULL,
                logged_at TEXT NOT NULL,
                received_at TEXT NOT NULL,
                FOREIGN KEY (node_id) REFERENCES servers(node_id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_error_logs_node_time
                ON error_logs(node_id, received_at DESC);

            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                node_id TEXT NOT NULL,
                alert_type TEXT NOT NULL,
                severity TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL,
                fault_level INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_alerts_time ON alerts(created_at DESC);

            CREATE TABLE IF NOT EXISTS alert_cooldowns (
                alert_key TEXT PRIMARY KEY,
                last_sent REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS feedbacks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL,
                session_id TEXT NOT NULL,
                sender_name TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_feedbacks_time ON feedbacks(created_at DESC);

            CREATE TABLE IF NOT EXISTS server_profiles (
                node_id TEXT PRIMARY KEY,
                remark TEXT NOT NULL DEFAULT '',
                ssh_host TEXT NOT NULL DEFAULT '',
                ssh_port INTEGER NOT NULL DEFAULT 22,
                ssh_user TEXT NOT NULL DEFAULT 'root',
                provider_url TEXT NOT NULL DEFAULT '',
                lianlian_id TEXT NOT NULL DEFAULT '',
                lianlian_password_enc TEXT NOT NULL DEFAULT '',
                location_name TEXT NOT NULL DEFAULT '',
                latitude REAL,
                longitude REAL,
                host_key_fingerprint TEXT NOT NULL DEFAULT '',
                check_ports TEXT NOT NULL DEFAULT '',
                log_files TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS server_removals (
                node_id TEXT PRIMARY KEY,
                removed_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS bot_configs (
                id TEXT PRIMARY KEY,
                platform TEXT NOT NULL,
                remark TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                config_enc TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fingerprint TEXT NOT NULL UNIQUE,
                node_id TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'system',
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                severity INTEGER,
                status TEXT NOT NULL DEFAULT 'unhandled',
                analysis TEXT NOT NULL DEFAULT '',
                analysis_engine TEXT NOT NULL DEFAULT 'rules',
                ai_ignore_until TEXT,
                ai_ignore_reason TEXT NOT NULL DEFAULT '',
                ai_reviewed_at TEXT,
                problem_keys_json TEXT NOT NULL DEFAULT '[]',
                remark TEXT NOT NULL DEFAULT '',
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                handled_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status, severity);
            CREATE INDEX IF NOT EXISTS idx_tickets_time ON tickets(last_seen DESC);

            CREATE TABLE IF NOT EXISTS game_servers (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                game_type TEXT NOT NULL,
                host TEXT NOT NULL,
                query_port INTEGER NOT NULL,
                console_port INTEGER NOT NULL DEFAULT 0,
                remark TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                secret_enc TEXT NOT NULL DEFAULT '',
                status_json TEXT NOT NULL DEFAULT '{}',
                last_checked TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS network_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                node_id TEXT NOT NULL,
                upload_total_bytes INTEGER NOT NULL,
                download_total_bytes INTEGER NOT NULL,
                sampled_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_network_samples_time
                ON network_samples(node_id, sampled_at);

            CREATE TABLE IF NOT EXISTS server_locations (
                ip TEXT PRIMARY KEY,
                location_name TEXT NOT NULL DEFAULT '',
                country TEXT NOT NULL DEFAULT '',
                region TEXT NOT NULL DEFAULT '',
                city TEXT NOT NULL DEFAULT '',
                latitude REAL NOT NULL,
                longitude REAL NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS ai_cache (
                problem_key TEXT PRIMARY KEY,
                verdict_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_called REAL NOT NULL
            );
            """
        )
        await _ensure_columns(
            db,
            "servers",
            {
                "report_latency_ms": "REAL NOT NULL DEFAULT 0",
                "memory_total_mb": "REAL NOT NULL DEFAULT 0",
                "memory_used_mb": "REAL NOT NULL DEFAULT 0",
                "swap_total_mb": "REAL NOT NULL DEFAULT 0",
                "swap_used_mb": "REAL NOT NULL DEFAULT 0",
                "disk_total_gb": "REAL NOT NULL DEFAULT 0",
                "disk_used_gb": "REAL NOT NULL DEFAULT 0",
                "disk_free_gb": "REAL NOT NULL DEFAULT 0",
                "disks_json": "TEXT NOT NULL DEFAULT '[]'",
                "gpu_available": "INTEGER NOT NULL DEFAULT 0",
                "gpu_percent": "REAL NOT NULL DEFAULT 0",
                "gpus_json": "TEXT NOT NULL DEFAULT '[]'",
                "network_upload_bps": "REAL NOT NULL DEFAULT 0",
                "network_download_bps": "REAL NOT NULL DEFAULT 0",
                "network_upload_total_bytes": "INTEGER NOT NULL DEFAULT 0",
                "network_download_total_bytes": "INTEGER NOT NULL DEFAULT 0",
                "cpu_cores": "INTEGER NOT NULL DEFAULT 0",
                "cpu_model": "TEXT NOT NULL DEFAULT ''",
                "load1": "REAL NOT NULL DEFAULT 0",
                "load5": "REAL NOT NULL DEFAULT 0",
                "load15": "REAL NOT NULL DEFAULT 0",
                "os_info": "TEXT NOT NULL DEFAULT ''",
                "ssh_host": "TEXT NOT NULL DEFAULT ''",
                "ssh_port": "INTEGER NOT NULL DEFAULT 22",
                "ssh_user": "TEXT NOT NULL DEFAULT 'root'",
                "power_state": "TEXT NOT NULL DEFAULT ''",
                "host_key_fingerprint": "TEXT NOT NULL DEFAULT ''",
            },
        )
        await _ensure_columns(
            db,
            "port_status",
            {"latency_ms": "REAL NOT NULL DEFAULT 0"},
        )
        await _ensure_columns(
            db,
            "server_profiles",
            {
                "provider_url": "TEXT NOT NULL DEFAULT ''",
                "lianlian_id": "TEXT NOT NULL DEFAULT ''",
                "lianlian_password_enc": "TEXT NOT NULL DEFAULT ''",
                "location_name": "TEXT NOT NULL DEFAULT ''",
                "latitude": "REAL",
                "longitude": "REAL",
            },
        )
        await _ensure_columns(
            db,
            "alerts",
            {"fault_level": "INTEGER"},
        )
        await _ensure_columns(
            db,
            "tickets",
            {
                "analysis_engine": "TEXT NOT NULL DEFAULT 'rules'",
                "ai_ignore_until": "TEXT",
                "ai_ignore_reason": "TEXT NOT NULL DEFAULT ''",
                "ai_reviewed_at": "TEXT",
                "problem_keys_json": "TEXT NOT NULL DEFAULT '[]'",
            },
        )
        await _backfill_alert_tickets(db)
        await _normalise_existing_ssh_endpoints(db)
        await _remove_login_only_tickets(db)
        await db.commit()


async def _ensure_columns(
    db: aiosqlite.Connection,
    table: str,
    columns: dict[str, str],
) -> None:
    cursor = await db.execute(f"PRAGMA table_info({table})")
    existing = {row["name"] for row in await cursor.fetchall()}
    for name, definition in columns.items():
        if name not in existing:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


async def _backfill_alert_tickets(db: aiosqlite.Connection) -> None:
    cursor = await db.execute(
        """
        SELECT node_id, MAX(created_at) AS latest,
               MIN(COALESCE(fault_level, 3)) AS highest
        FROM alerts WHERE alert_type != 'SSH 登录' GROUP BY node_id
        """
    )
    for summary in await cursor.fetchall():
        node_id = summary["node_id"]
        existing_cursor = await db.execute(
            "SELECT id, status, last_seen FROM tickets WHERE node_id = ? "
            "ORDER BY updated_at DESC, id DESC LIMIT 1",
            (node_id,),
        )
        existing = await existing_cursor.fetchone()
        if existing and str(existing["last_seen"]) >= str(summary["latest"]):
            continue
        alert_cursor = await db.execute(
            """
            SELECT alert_type, message, created_at, COALESCE(fault_level, 3)
                   AS fault_level
            FROM alerts WHERE node_id = ? AND alert_type != 'SSH 登录'
            ORDER BY created_at DESC, id DESC LIMIT 20
            """,
            (node_id,),
        )
        rows = await alert_cursor.fetchall()
        if not rows:
            continue
        highest = min(int(row["fault_level"]) for row in rows)
        issues = "\n".join(
            f"• [{row['fault_level']}级] {row['alert_type']}：{row['message']}"
            for row in rows
        )
        now_iso = utc_now()
        if existing and existing["status"] in {"unhandled", "in_progress"}:
            await db.execute(
                """
                UPDATE tickets SET
                    description = ?,
                    severity = ?,
                    last_seen = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (issues, highest, summary["latest"], now_iso, existing["id"]),
            )
        else:
            await db.execute(
                """
                INSERT INTO tickets(
                    fingerprint, node_id, source, title, description, severity,
                    status, analysis, analysis_engine, first_seen, last_seen, updated_at
                ) VALUES (?, ?, 'system', ?, ?, ?, 'unhandled', ?, 'rules', ?, ?, ?)
                """,
                (
                    f"incident:{node_id}:{int(time.time())}:{uuid.uuid4().hex[:8]}",
                    node_id,
                    f"{node_id} 实时故障检测",
                    issues,
                    highest,
                    "历史告警已自动补建为实时故障工单。",
                    now_iso,
                    summary["latest"],
                    now_iso,
                ),
            )


async def _normalise_existing_ssh_endpoints(db: aiosqlite.Connection) -> None:
    for table in ("server_profiles", "servers"):
        cursor = await db.execute(f"SELECT node_id, ssh_host, ssh_port FROM {table}")
        for row in await cursor.fetchall():
            host, port = _split_ssh_endpoint(row["ssh_host"], row["ssh_port"] or 22)
            if host != row["ssh_host"] or port != int(row["ssh_port"] or 22):
                await db.execute(
                    f"UPDATE {table} SET ssh_host = ?, ssh_port = ? WHERE node_id = ?",
                    (host, port, row["node_id"]),
                )


async def _remove_login_only_tickets(db: aiosqlite.Connection) -> None:
    cursor = await db.execute(
        "SELECT id, description, problem_keys_json FROM tickets "
        "WHERE description LIKE '%SSH 登录%'"
    )
    for row in await cursor.fetchall():
        lines = [line for line in row["description"].splitlines() if line.strip()]
        remaining = [line for line in lines if "[2级] SSH 登录：" not in line]
        if not remaining:
            await db.execute("DELETE FROM tickets WHERE id = ?", (row["id"],))
            continue
        try:
            keys = json.loads(row["problem_keys_json"])
        except (json.JSONDecodeError, TypeError):
            keys = []
        keys = [
            key for key in keys
            if not str(key).startswith("ssh-login:")
        ]
        await db.execute(
            "UPDATE tickets SET description = ?, problem_keys_json = ? WHERE id = ?",
            ("\n".join(remaining), json.dumps(keys, ensure_ascii=False), row["id"]),
        )


def _normalise_ports(raw: Any) -> list[dict[str, Any]]:
    ports: list[dict[str, Any]] = []
    if isinstance(raw, dict):
        for port, alive in raw.items():
            entry = alive if isinstance(alive, dict) else {}
            value = entry.get("alive", alive)
            try:
                ports.append(
                    {
                        "port": int(port),
                        "alive": bool(value),
                        "latency_ms": max(0.0, float(entry.get("latency_ms") or 0)),
                    }
                )
            except (TypeError, ValueError):
                continue
    elif isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, dict):
                try:
                    ports.append(
                        {
                            "port": int(entry.get("port")),
                            "alive": bool(entry.get("alive", False)),
                            "latency_ms": max(
                                0.0, float(entry.get("latency_ms") or 0)
                            ),
                        }
                    )
                except (TypeError, ValueError):
                    continue
            else:
                try:
                    ports.append(
                        {"port": int(entry), "alive": True, "latency_ms": 0}
                    )
                except (TypeError, ValueError):
                    continue
    return sorted(ports, key=lambda item: item["port"])


def _normalise_errors(raw: Any) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    if not isinstance(raw, list):
        return errors
    for entry in raw[:100]:
        if isinstance(entry, dict):
            message = str(
                entry.get("message")
                or entry.get("text")
                or entry.get("error")
                or ""
            ).strip()
            logged_at = str(
                entry.get("timestamp")
                or entry.get("time")
                or entry.get("logged_at")
                or utc_now()
            )
        else:
            message = str(entry).strip()
            logged_at = utc_now()
        if message:
            errors.append({"message": message[:4000], "logged_at": logged_at})
    return errors


def _normalise_disks(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    excluded_fs = {"squashfs", "tmpfs", "devtmpfs", "proc", "sysfs", "cgroup2"}
    by_device: dict[str, dict[str, Any]] = {}
    for entry in raw[:100]:
        if not isinstance(entry, dict):
            continue
        device = str(entry.get("device") or "")
        filesystem = str(entry.get("fstype") or "")
        if (
            filesystem in excluded_fs
            or device.startswith("/dev/loop")
            or not str(entry.get("mount") or "").startswith("/")
        ):
            continue
        try:
            item = {
                "mount": str(entry.get("mount") or "")[:255],
                "device": device[:255],
                "fstype": filesystem[:40],
                "total_gb": max(0.0, float(entry.get("total_gb") or 0)),
                "used_gb": max(0.0, float(entry.get("used_gb") or 0)),
                "free_gb": max(0.0, float(entry.get("free_gb") or 0)),
                "percent": max(
                    0.0, min(100.0, float(entry.get("percent") or 0))
                ),
            }
        except (TypeError, ValueError):
            continue
        key = device or item["mount"]
        previous = by_device.get(key)
        if previous is None or item["total_gb"] > previous["total_gb"]:
            by_device[key] = item
    return sorted(by_device.values(), key=lambda item: item["mount"])


def _split_ssh_endpoint(host_value: Any, port_value: Any = 22) -> tuple[str, int]:
    host = str(host_value or "").strip()
    port = max(1, min(65535, int(port_value or 22)))
    if host.count(":") == 1:
        candidate_host, candidate_port = host.rsplit(":", 1)
        if candidate_port.isdigit():
            host = candidate_host.strip()
            port = max(1, min(65535, int(candidate_port)))
    return host, port


def _clean_provider_url(value: Any) -> str:
    url = str(value or "").strip()
    if not url:
        return ""
    if not url.startswith(("https://", "http://")) or len(url) > 1000:
        raise ValueError("供应商管理链接必须是 http:// 或 https:// 地址")
    return url


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("经纬度必须是数字") from exc


def normalise_report(payload: dict[str, Any]) -> dict[str, Any]:
    node_id = str(
        payload.get("node_id")
        or payload.get("server_id")
        or payload.get("id")
        or ""
    ).strip()
    if not node_id:
        raise ValueError("node_id is required")
    disks = _normalise_disks(payload.get("disks", payload.get("disk_usage", [])))
    if disks:
        disk_total_gb = sum(item["total_gb"] for item in disks)
        disk_used_gb = sum(item["used_gb"] for item in disks)
        disk_free_gb = sum(item["free_gb"] for item in disks)
        disk_percent = (
            disk_used_gb / disk_total_gb * 100 if disk_total_gb else 0
        )
    else:
        disk_total_gb = max(0.0, float(payload.get("disk_total_gb") or 0))
        disk_used_gb = max(0.0, float(payload.get("disk_used_gb") or 0))
        disk_free_gb = max(0.0, float(payload.get("disk_free_gb") or 0))
        disk_percent = max(
            0.0, min(100.0, float(payload.get("disk_percent") or 0))
        )
    ssh_host = str(payload.get("ssh_host") or payload.get("ip") or "").strip()
    ssh_port = max(1, min(65535, int(payload.get("ssh_port") or 22)))
    if ssh_host.count(":") == 1:
        candidate_host, candidate_port = ssh_host.rsplit(":", 1)
        if candidate_port.isdigit():
            ssh_host = candidate_host
            ssh_port = max(1, min(65535, int(candidate_port)))
    gpus = payload.get("gpus", [])
    if not isinstance(gpus, list):
        gpus = []
    ssh_logins = payload.get("ssh_logins", [])
    if not isinstance(ssh_logins, list):
        ssh_logins = []
    return {
        "node_id": node_id[:128],
        "hostname": str(
            payload.get("hostname")
            or payload.get("host_name")
            or payload.get("server_name")
            or node_id
        )[:255],
        "ip": str(payload.get("ip") or payload.get("host_ip") or "")[:255],
        "cpu_percent": max(
            0.0, min(100.0, float(payload.get("cpu_percent") or 0))
        ),
        "memory_percent": max(
            0.0, min(100.0, float(payload.get("memory_percent") or 0))
        ),
        "disk_percent": min(100.0, disk_percent),
        "uptime_seconds": max(0, int(payload.get("uptime_seconds") or 0)),
        "report_latency_ms": max(
            0.0, float(payload.get("report_latency_ms") or 0)
        ),
        "memory_total_mb": max(
            0.0, float(payload.get("memory_total_mb") or 0)
        ),
        "memory_used_mb": max(
            0.0, float(payload.get("memory_used_mb") or 0)
        ),
        "swap_total_mb": max(0.0, float(payload.get("swap_total_mb") or 0)),
        "swap_used_mb": max(0.0, float(payload.get("swap_used_mb") or 0)),
        "disk_total_gb": disk_total_gb,
        "disk_used_gb": disk_used_gb,
        "disk_free_gb": disk_free_gb,
        "disks": disks,
        "gpu_available": bool(payload.get("gpu_available")) and bool(gpus),
        "gpu_percent": max(
            0.0, min(100.0, float(payload.get("gpu_percent") or 0))
        ),
        "gpus": gpus[:16],
        "network_upload_bps": max(
            0.0, float(payload.get("network_upload_bps") or 0)
        ),
        "network_download_bps": max(
            0.0, float(payload.get("network_download_bps") or 0)
        ),
        "network_upload_total_bytes": max(
            0, int(payload.get("network_upload_total_bytes") or 0)
        ),
        "network_download_total_bytes": max(
            0, int(payload.get("network_download_total_bytes") or 0)
        ),
        "cpu_cores": max(0, int(payload.get("cpu_cores") or 0)),
        "cpu_model": str(payload.get("cpu_model") or "")[:255],
        "load1": max(0.0, float(payload.get("load1") or 0)),
        "load5": max(0.0, float(payload.get("load5") or 0)),
        "load15": max(0.0, float(payload.get("load15") or 0)),
        "os_info": str(payload.get("os_info") or "")[:255],
        "ssh_host": ssh_host[:255],
        "ssh_port": ssh_port,
        "ssh_user": str(payload.get("ssh_user") or "root")[:80],
        "ports": _normalise_ports(
            payload.get("ports", payload.get("port_status", {}))
        ),
        "errors": _normalise_errors(
            payload.get("errors", payload.get("program_logs", []))
        ),
        "ssh_logins": [
            {
                "username": str(item.get("username") or "")[:80],
                "ip": str(item.get("ip") or "")[:128],
                "port": str(item.get("port") or "")[:16],
                "method": str(item.get("method") or "")[:40],
                "time": str(item.get("time") or utc_now())[:64],
            }
            for item in ssh_logins[:20]
            if isinstance(item, dict)
        ],
        "received_at": utc_now(),
        "raw": payload,
    }


async def _alert_is_due(db: aiosqlite.Connection, alert_key: str) -> bool:
    cursor = await db.execute(
        "SELECT last_sent FROM alert_cooldowns WHERE alert_key = ?", (alert_key,)
    )
    row = await cursor.fetchone()
    now = time.time()
    return not row or now - float(row["last_sent"]) >= settings.alert_cooldown_seconds


async def _persist_alerts(
    db: aiosqlite.Connection, alerts: list[dict[str, Any]]
) -> list[str]:
    due_texts: list[str] = []
    now_iso = utc_now()
    now_epoch = time.time()
    allowed_alerts: list[dict[str, Any]] = []
    for node_id in {alert["node_id"] for alert in alerts}:
        node_alerts = [alert for alert in alerts if alert["node_id"] == node_id]
        cursor = await db.execute(
            "SELECT ai_ignore_until, problem_keys_json FROM tickets "
            "WHERE node_id = ? "
            "ORDER BY updated_at DESC, id DESC LIMIT 1",
            (node_id,),
        )
        row = await cursor.fetchone()
        ignore_until = parse_utc(row["ai_ignore_until"]) if row else None
        try:
            ignored_keys = set(json.loads(row["problem_keys_json"])) if row else set()
        except (json.JSONDecodeError, TypeError):
            ignored_keys = set()
        ignore_active = bool(
            ignore_until and ignore_until.timestamp() > now_epoch
        )
        for alert in node_alerts:
            if (
                ignore_active
                and int(alert["fault_level"]) != 2
                and alert["key"] in ignored_keys
            ):
                continue
            allowed_alerts.append(alert)
    alerts = allowed_alerts
    if not alerts:
        return []
    for alert in alerts:
        if not await _alert_is_due(db, alert["key"]):
            continue
        await db.execute(
            """
            INSERT INTO alerts(
                node_id, alert_type, severity, message, created_at, fault_level
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                alert["node_id"],
                alert["type"],
                alert["severity"],
                alert["message"],
                now_iso,
                alert["fault_level"],
            ),
        )
        await db.execute(
            """
            INSERT INTO alert_cooldowns(alert_key, last_sent) VALUES (?, ?)
            ON CONFLICT(alert_key) DO UPDATE SET last_sent = excluded.last_sent
            """,
            (alert["key"], now_epoch),
        )
        due_texts.append(
            f"[故障{alert['fault_level']}级] {alert['node_id']} | "
            f"{alert['type']} | {alert['message']}"
        )
    grouped: dict[str, list[dict[str, Any]]] = {}
    for alert in (item for item in alerts if not item.get("skip_ticket")):
        grouped.setdefault(alert["node_id"], []).append(alert)
    for node_id, node_alerts in grouped.items():
        highest_level = min(int(alert["fault_level"]) for alert in node_alerts)
        issue_lines = "\n".join(
            f"• [{alert['fault_level']}级] {alert['type']}：{alert['message']}"
            for alert in node_alerts
        )
        primary = min(node_alerts, key=lambda item: int(item["fault_level"]))
        analysis = (
            f"同一时间检测到 {len(node_alerts)} 个问题，工单按最高故障等级"
            f"（{highest_level}级）处理。"
            f"{primary.get('analysis') or _analysis_for(primary['type'], primary['message'], highest_level)}"
        )[:4000]
        problem_keys_json = json.dumps(
            sorted(alert["key"] for alert in node_alerts), ensure_ascii=False
        )
        active_cursor = await db.execute(
            """
            SELECT id FROM tickets
            WHERE node_id = ? AND status IN ('unhandled', 'in_progress')
            ORDER BY id DESC LIMIT 1
            """,
            (node_id,),
        )
        active = await active_cursor.fetchone()
        if active:
            await db.execute(
                """
                UPDATE tickets SET
                    severity = ?,
                    title = ?,
                    description = ?,
                    analysis = ?,
                    analysis_engine = 'rules',
                    ai_ignore_until = CASE WHEN ? <= 2 THEN NULL ELSE ai_ignore_until END,
                    ai_ignore_reason = CASE WHEN ? <= 2 THEN '' ELSE ai_ignore_reason END,
                    problem_keys_json = ?,
                    last_seen = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    highest_level,
                    f"{node_id} 实时故障检测",
                    issue_lines,
                    analysis,
                    highest_level,
                    highest_level,
                    problem_keys_json,
                    now_iso,
                    now_iso,
                    active["id"],
                ),
            )
        else:
            await db.execute(
                """
                INSERT INTO tickets(
                    fingerprint, node_id, source, title, description, severity,
                    status, analysis, analysis_engine, problem_keys_json,
                    first_seen, last_seen, updated_at
                ) VALUES (?, ?, 'system', ?, ?, ?, 'unhandled', ?, 'rules', ?, ?, ?, ?)
                """,
                (
                    f"incident:{node_id}:{int(time.time())}:{uuid.uuid4().hex[:8]}",
                    node_id,
                    f"{node_id} 实时故障检测",
                    issue_lines,
                    highest_level,
                    analysis,
                    problem_keys_json,
                    now_iso,
                    now_iso,
                    now_iso,
                ),
            )
    return due_texts


def _analysis_for(fault_type: str, message: str, level: int) -> str:
    if "离线" in fault_type:
        return "检查目标主机的电源、公网连通性、SSH 和探针服务；确认云厂商状态页。"
    if "端口" in fault_type:
        return "检查对应服务进程、监听地址、防火墙/安全组和上游反向代理。"
    if "CPU" in fault_type:
        return "检查高占用进程、请求突增和定时任务；必要时限制并发或扩容。"
    if "内存" in fault_type:
        return "检查进程泄漏、缓存占用和 Swap；必要时重启异常进程并评估扩容。"
    if "磁盘" in fault_type:
        return "检查大文件、日志轮转和数据库增长；立即清理或扩容存储。"
    if "日志" in fault_type or "报错" in fault_type:
        return "根据日志堆栈定位异常模块，检查近期发布、依赖和外部 API 状态。"
    if "延迟" in fault_type:
        return "检查网络路径、丢包、上游负载和探针到中枢的链路质量。"
    return "收集更多指标和日志后重新分析，必要时由管理员手动定级。"


def _usage_level(value: float) -> int | None:
    if value >= 95:
        return 2
    if value >= 90:
        return 3
    if value >= 80:
        return 4
    return None


def _level_severity(level: int) -> str:
    return "critical" if level <= 2 else "warning"


async def save_report(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    report = normalise_report(payload)
    alerts: list[dict[str, Any]] = []
    node_id = report["node_id"]
    async with connect() as db:
        cursor = await db.execute(
            "SELECT 1 FROM server_removals WHERE node_id = ?", (node_id,)
        )
        if await cursor.fetchone():
            return report, []

    resource_checks = (
        ("cpu", "CPU 使用率偏高", report["cpu_percent"]),
        ("memory", "内存使用率偏高", report["memory_percent"]),
        ("disk", "磁盘使用率偏高", report["disk_percent"]),
    )
    for resource_key, title, value in resource_checks:
        level = _usage_level(value)
        if level is not None:
            alerts.append(
                {
                    "key": f"{resource_key}:{node_id}",
                    "node_id": node_id,
                    "type": title,
                    "severity": _level_severity(level),
                    "fault_level": level,
                    "message": f"{title}：{value:.1f}%",
                    "analysis": _analysis_for(title, "", level),
                }
            )
    for port in report["ports"]:
        if not port["alive"]:
            level = 1 if port["port"] in {22, 80, 443, 25565} else 2
            alerts.append(
                {
                    "key": f"port:{node_id}:{port['port']}",
                    "node_id": node_id,
                    "type": "端口失联",
                    "severity": _level_severity(level),
                    "fault_level": level,
                    "message": f"端口 {port['port']} 不可达",
                    "analysis": _analysis_for("端口失联", "", level),
                }
            )
    error_level = 2 if len(report["errors"]) >= 3 else 3
    for index, error in enumerate(report["errors"]):
        message = error["message"]
        alerts.append(
            {
                "key": f"error:{node_id}:{index}:{message[:120]}",
                "node_id": node_id,
                "type": "程序报错",
                "severity": _level_severity(error_level),
                "fault_level": error_level,
                "message": message[:500],
                "analysis": _analysis_for("程序报错", message, error_level),
            }
        )
    for login in report.get("ssh_logins", []):
        alerts.append(
            {
                "key": (
                    f"ssh-login:{node_id}:{login['ip']}:{login['username']}:"
                    f"{login['method']}"
                ),
                "node_id": node_id,
                "type": "SSH 登录",
                "severity": "critical",
                "fault_level": 2,
                "message": (
                    f"用户 {login['username']} 从 {login['ip']}:{login['port']} "
                    f"通过 {login['method']} 登录"
                ),
                "analysis": (
                    "这是一条登录审计事件。请确认来源 IP、登录用户和认证方式；"
                    "2级安全事件不会被备注忽略。"
                ),
                "skip_ai": True,
                "skip_ticket": True,
            }
        )
    latency = report["report_latency_ms"]
    if latency >= 300:
        level = 3 if latency >= 1500 else 4 if latency >= 800 else 5
        alerts.append(
            {
                "key": f"latency:{node_id}",
                "node_id": node_id,
                "type": "连接延迟偏高",
                "severity": _level_severity(level),
                "fault_level": level,
                "message": f"探针到中枢上报耗时 {latency:.1f} ms",
                "analysis": _analysis_for("连接延迟偏高", "", level),
            }
        )

    async with _write_lock:
        async with connect() as db:
            await db.execute(
                """
                INSERT INTO servers(
                    node_id, hostname, ip, cpu_percent, memory_percent,
                    disk_percent, uptime_seconds, last_seen, report_json,
                    report_latency_ms, memory_total_mb, memory_used_mb,
                    swap_total_mb, swap_used_mb, disk_total_gb,
                    disk_used_gb, disk_free_gb, disks_json,
                    gpu_available, gpu_percent, gpus_json,
                    network_upload_bps, network_download_bps,
                    network_upload_total_bytes, network_download_total_bytes,
                    cpu_cores,
                    cpu_model, load1, load5, load15, os_info,
                    ssh_host, ssh_port, ssh_user
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                ON CONFLICT(node_id) DO UPDATE SET
                    hostname=excluded.hostname,
                    ip=excluded.ip,
                    cpu_percent=excluded.cpu_percent,
                    memory_percent=excluded.memory_percent,
                    disk_percent=excluded.disk_percent,
                    uptime_seconds=excluded.uptime_seconds,
                    last_seen=excluded.last_seen,
                    power_state='online',
                    report_json=excluded.report_json,
                    report_latency_ms=excluded.report_latency_ms,
                    memory_total_mb=excluded.memory_total_mb,
                    memory_used_mb=excluded.memory_used_mb,
                    swap_total_mb=excluded.swap_total_mb,
                    swap_used_mb=excluded.swap_used_mb,
                    disk_total_gb=excluded.disk_total_gb,
                    disk_used_gb=excluded.disk_used_gb,
                    disk_free_gb=excluded.disk_free_gb,
                    disks_json=excluded.disks_json,
                    gpu_available=excluded.gpu_available,
                    gpu_percent=excluded.gpu_percent,
                    gpus_json=excluded.gpus_json,
                    network_upload_bps=excluded.network_upload_bps,
                    network_download_bps=excluded.network_download_bps,
                    network_upload_total_bytes=excluded.network_upload_total_bytes,
                    network_download_total_bytes=excluded.network_download_total_bytes,
                    cpu_cores=excluded.cpu_cores,
                    cpu_model=excluded.cpu_model,
                    load1=excluded.load1,
                    load5=excluded.load5,
                    load15=excluded.load15,
                    os_info=excluded.os_info,
                    ssh_host=CASE
                        WHEN servers.ssh_host = '' THEN excluded.ssh_host
                        ELSE servers.ssh_host
                    END,
                    ssh_port=CASE
                        WHEN servers.ssh_host = '' THEN excluded.ssh_port
                        ELSE servers.ssh_port
                    END,
                    ssh_user=CASE
                        WHEN servers.ssh_host = '' THEN excluded.ssh_user
                        ELSE servers.ssh_user
                    END
                """,
                (
                    node_id,
                    report["hostname"],
                    report["ip"],
                    report["cpu_percent"],
                    report["memory_percent"],
                    report["disk_percent"],
                    report["uptime_seconds"],
                    report["received_at"],
                    json.dumps(report["raw"], ensure_ascii=False)[:50000],
                    report["report_latency_ms"],
                    report["memory_total_mb"],
                    report["memory_used_mb"],
                    report["swap_total_mb"],
                    report["swap_used_mb"],
                    report["disk_total_gb"],
                    report["disk_used_gb"],
                    report["disk_free_gb"],
                    json.dumps(report["disks"], ensure_ascii=False)[:20000],
                    1 if report["gpu_available"] else 0,
                    report["gpu_percent"],
                    json.dumps(report["gpus"], ensure_ascii=False)[:12000],
                    report["network_upload_bps"],
                    report["network_download_bps"],
                    report["network_upload_total_bytes"],
                    report["network_download_total_bytes"],
                    report["cpu_cores"],
                    report["cpu_model"],
                    report["load1"],
                    report["load5"],
                    report["load15"],
                    report["os_info"],
                    report["ssh_host"],
                    report["ssh_port"],
                    report["ssh_user"],
                ),
            )
            if (
                report["network_upload_total_bytes"] > 0
                or report["network_download_total_bytes"] > 0
            ):
                await db.execute(
                    """
                    INSERT INTO network_samples(
                        node_id, upload_total_bytes, download_total_bytes, sampled_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        node_id,
                        report["network_upload_total_bytes"],
                        report["network_download_total_bytes"],
                        report["received_at"],
                    ),
                )
                await db.execute(
                    """
                    DELETE FROM network_samples
                    WHERE node_id = ?
                      AND julianday(sampled_at) < julianday('now', '-35 days')
                    """,
                    (node_id,),
                )
            await db.execute("DELETE FROM port_status WHERE node_id = ?", (node_id,))
            for port in report["ports"]:
                await db.execute(
                    """
                    INSERT INTO port_status(
                        node_id, port, alive, checked_at, latency_ms
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        node_id,
                        port["port"],
                        1 if port["alive"] else 0,
                        report["received_at"],
                        port["latency_ms"],
                    ),
                )
            for error in report["errors"]:
                await db.execute(
                    """
                    INSERT INTO error_logs(node_id, message, logged_at, received_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        node_id,
                        error["message"],
                        error["logged_at"],
                        report["received_at"],
                    ),
                )
            due_texts = await _persist_alerts(db, alerts)
            await db.commit()

    if due_texts:
        asyncio.create_task(broadcast_alerts(due_texts), name="broadcast-new-alerts")
    ai_alerts = [alert for alert in alerts if not alert.get("skip_ai")]
    if ai_alerts:
        asyncio.create_task(
            ai_classify_report(
                node_id,
                [alert["key"] for alert in ai_alerts],
                report,
                [f"{alert['type']}: {alert['message']}" for alert in ai_alerts],
            ),
            name="ai-classify-report",
        )
    return report, due_texts


async def ai_classify_report(
    node_id: str,
    fingerprints: list[str],
    report: dict[str, Any],
    events: list[str],
) -> None:
    if not fingerprints:
        return
    problem_key = hashlib.sha256(
        json.dumps(sorted(fingerprints), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    try:
        async with _ai_lock:
            async with connect() as db:
                cursor = await db.execute(
                    "SELECT verdict_json, last_called FROM ai_cache "
                    "WHERE problem_key = ?",
                    (problem_key,),
                )
                cached = await cursor.fetchone()
            verdict: dict[str, Any] | None = None
            if cached and time.time() - float(cached["last_called"]) < (
                settings.ai_classify_cooldown_seconds
            ):
                verdict = json.loads(cached["verdict_json"])
            else:
                from core.ai_judge import classify_telemetry

                verdict = await classify_telemetry(report, events)
                if verdict:
                    async with connect() as db:
                        await db.execute(
                            """
                            INSERT INTO ai_cache(
                                problem_key, verdict_json, created_at, last_called
                            ) VALUES (?, ?, ?, ?)
                            ON CONFLICT(problem_key) DO UPDATE SET
                                verdict_json=excluded.verdict_json,
                                created_at=excluded.created_at,
                                last_called=excluded.last_called
                            """,
                            (
                                problem_key,
                                json.dumps(verdict, ensure_ascii=False),
                                utc_now(),
                                time.time(),
                            ),
                        )
                        await db.commit()
        if not verdict:
            return
        async with _write_lock:
            async with connect() as db:
                await db.execute(
                    """
                    UPDATE tickets SET
                        severity = CASE
                            WHEN severity IS NULL OR severity > ? THEN ?
                            ELSE severity
                        END,
                        analysis = ?,
                        analysis_engine = 'ai',
                        updated_at = ?
                    WHERE node_id = ?
                      AND status IN ('unhandled', 'in_progress')
                    """,
                    (
                        int(verdict["level"]),
                        int(verdict["level"]),
                        str(verdict["analysis"])[:100],
                        utc_now(),
                        node_id,
                    ),
                )
                await db.commit()
    except Exception:
        logger.exception("Could not apply AI classification")


async def ai_classify_feedback(
    feedback_id: int,
    platform: str,
    content: str,
) -> None:
    try:
        from core.ai_judge import classify_feedback

        verdict = await classify_feedback(content, platform)
        if not verdict:
            return
        async with _write_lock:
            async with connect() as db:
                await db.execute(
                    """
                    UPDATE tickets SET
                        severity = ?,
                        analysis = ?,
                        analysis_engine = 'ai',
                        updated_at = ?
                    WHERE fingerprint = ?
                    """,
                    (
                        int(verdict["level"]),
                        str(verdict["analysis"])[:100],
                        utc_now(),
                        f"feedback:{feedback_id}",
                    ),
                )
                await db.commit()
    except Exception:
        logger.exception("Could not classify feedback with AI")


async def check_offline_nodes() -> list[str]:
    cutoff = time.time() - settings.heartbeat_timeout_seconds
    alerts: list[dict[str, Any]] = []
    async with connect() as db:
        cursor = await db.execute(
            "SELECT node_id, hostname, last_seen FROM servers ORDER BY node_id"
        )
        rows = await cursor.fetchall()
        for row in rows:
            seen = parse_utc(row["last_seen"])
            if seen and seen.timestamp() < cutoff:
                age = int(time.time() - seen.timestamp())
                alerts.append(
                    {
                        "key": f"offline:{row['node_id']}",
                        "node_id": row["node_id"],
                        "type": "心跳离线",
                        "severity": "critical",
                        "fault_level": 1,
                        "message": f"已 {age} 秒未收到心跳",
                        "analysis": _analysis_for("心跳离线", "", 1),
                    }
                )
    if not alerts:
        return []
    async with _write_lock:
        async with connect() as db:
            due_texts = await _persist_alerts(db, alerts)
            await db.commit()
    if due_texts:
        asyncio.create_task(
            broadcast_alerts(due_texts), name="broadcast-offline-alerts"
        )
    grouped: dict[str, list[dict[str, Any]]] = {}
    for alert in (item for item in alerts if not item.get("skip_ticket")):
        grouped.setdefault(alert["node_id"], []).append(alert)
    for node_id, node_alerts in grouped.items():
        asyncio.create_task(
            ai_classify_report(
                node_id,
                [alert["key"] for alert in node_alerts],
                {
                    "cpu_percent": 0,
                    "memory_percent": 0,
                    "disk_percent": 0,
                    "report_latency_ms": 0,
                    "ports": [],
                    "errors": [],
                },
                [f"{alert['type']}: {alert['message']}" for alert in node_alerts],
            ),
            name="ai-classify-offline",
        )
    return due_texts


async def offline_monitor_loop(stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            await check_offline_nodes()
        except Exception:
            logger.exception("Offline monitor iteration failed")
        try:
            await asyncio.wait_for(
                stop_event.wait(),
                timeout=settings.offline_check_interval_seconds,
            )
        except asyncio.TimeoutError:
            pass


async def save_feedback(
    platform: str,
    session_id: str,
    sender_name: str,
    content: str,
) -> int:
    async with _write_lock:
        async with connect() as db:
            now_iso = utc_now()
            cursor = await db.execute(
                """
                INSERT INTO feedbacks(platform, session_id, sender_name, content, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    platform[:30],
                    session_id[:255],
                    sender_name[:255],
                    content[:10000],
                    now_iso,
                ),
            )
            feedback_id = int(cursor.lastrowid)
            await db.execute(
                """
                INSERT INTO tickets(
                    fingerprint, node_id, source, title, description, severity,
                    status, analysis, first_seen, last_seen, updated_at
                ) VALUES (?, '', ?, ?, ?, NULL, 'unhandled', ?, ?, ?, ?)
                """,
                (
                    f"feedback:{feedback_id}",
                    f"{platform}/{session_id}",
                    f"{platform} 用户反馈待分类",
                    f"发送者：{sender_name}\n会话：{session_id}\n内容：{content}",
                    "反馈内容没有匹配到明确的自动故障规则，请管理员人工判断等级和处理方式。",
                    now_iso,
                    now_iso,
                    now_iso,
                ),
            )
            await db.commit()
            asyncio.create_task(
                ai_classify_feedback(feedback_id, platform, content),
                name="ai-classify-feedback",
            )
            return feedback_id


async def _network_usage_30d(
    db: aiosqlite.Connection, node_id: str
) -> dict[str, int]:
    cursor = await db.execute(
        """
        SELECT upload_total_bytes, download_total_bytes, sampled_at
        FROM network_samples
        WHERE node_id = ?
          AND julianday(sampled_at) >= julianday('now', '-30 days')
        ORDER BY sampled_at, id
        """,
        (node_id,),
    )
    upload_usage = 0
    download_usage = 0
    previous_upload: int | None = None
    previous_download: int | None = None
    for row in await cursor.fetchall():
        upload = int(row["upload_total_bytes"])
        download = int(row["download_total_bytes"])
        if previous_upload is None:
            pass
        else:
            upload_usage += (
                upload - previous_upload if upload >= previous_upload else upload
            )
            download_usage += (
                download - previous_download
                if previous_download is not None and download >= previous_download
                else download
            )
        previous_upload = upload
        previous_download = download
    return {
        "upload_bytes": max(0, upload_usage),
        "download_bytes": max(0, download_usage),
    }


async def dashboard_data() -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    async with connect() as db:
        profile_cursor = await db.execute("SELECT * FROM server_profiles")
        profiles = {
            row["node_id"]: dict(row) for row in await profile_cursor.fetchall()
        }
        server_cursor = await db.execute(
            "SELECT * FROM servers ORDER BY node_id COLLATE NOCASE"
        )
        server_rows = {
            row["node_id"]: row for row in await server_cursor.fetchall()
        }
        node_ids = sorted(
            set(profiles) | set(server_rows),
            key=lambda value: (
                (profiles.get(value, {}).get("remark") or value).lower(),
                value,
            ),
        )
        servers: list[dict[str, Any]] = []
        online_count = 0
        for node_id in node_ids:
            row = server_rows.get(node_id)
            profile = profiles.get(node_id, {})
            power_state = row["power_state"] if row else ""
            seen = parse_utc(row["last_seen"]) if row else None
            age = max(0, int((now - seen).total_seconds())) if seen else None
            online = bool(
                age is not None and age <= settings.heartbeat_timeout_seconds
                and power_state not in {"off", "rebooting"}
            )
            online_count += int(online)
            port_cursor = await db.execute(
                "SELECT port, alive, checked_at, latency_ms FROM port_status "
                "WHERE node_id = ? ORDER BY port",
                (node_id,),
            )
            ports = [
                {
                    "port": port["port"],
                    "alive": bool(port["alive"]),
                    "checked_at": port["checked_at"],
                    "latency_ms": round(float(port["latency_ms"]), 2),
                }
                for port in await port_cursor.fetchall()
            ]
            error_cursor = await db.execute(
                """
                SELECT message, logged_at, received_at FROM error_logs
                WHERE node_id = ? ORDER BY received_at DESC, id DESC LIMIT ?
                """,
                (node_id, settings.dashboard_error_limit),
            )
            errors = [dict(error) for error in await error_cursor.fetchall()]
            try:
                disks = json.loads(row["disks_json"]) if row else []
            except (json.JSONDecodeError, TypeError):
                disks = []
            try:
                gpus = json.loads(row["gpus_json"]) if row else []
            except (json.JSONDecodeError, TypeError):
                gpus = []
            network_usage = await _network_usage_30d(db, node_id)
            remark = profile.get("remark") or ""
            ssh_host = (
                profile.get("ssh_host")
                or (row["ssh_host"] if row else "")
                or (row["ip"] if row else "")
            )
            ssh_port = int(
                profile.get("ssh_port") or (row["ssh_port"] if row else 22) or 22
            )
            ssh_user = (
                profile.get("ssh_user")
                or (row["ssh_user"] if row else "")
                or "root"
            )
            servers.append(
                {
                    "node_id": node_id,
                    "hostname": (row["hostname"] if row else node_id),
                    "remark": remark,
                    "provider_url": profile.get("provider_url") or "",
                    "display_name": remark or (row["hostname"] if row else node_id),
                    "ip": (row["ip"] if row else ""),
                    "online": online,
                    "power_state": power_state,
                    "last_seen": (row["last_seen"] if row else None),
                    "heartbeat_age_seconds": age,
                    "metrics": {
                        "cpu_percent": round(
                            float(row["cpu_percent"] if row else 0), 2
                        ),
                        "memory_percent": round(
                            float(row["memory_percent"] if row else 0), 2
                        ),
                        "disk_percent": round(
                            float(row["disk_percent"] if row else 0), 2
                        ),
                        "uptime_seconds": (row["uptime_seconds"] if row else 0),
                        "report_latency_ms": round(
                            float(row["report_latency_ms"] if row else 0), 2
                        ),
                        "memory_total_mb": round(
                            float(row["memory_total_mb"] if row else 0), 2
                        ),
                        "memory_used_mb": round(
                            float(row["memory_used_mb"] if row else 0), 2
                        ),
                        "swap_total_mb": round(
                            float(row["swap_total_mb"] if row else 0), 2
                        ),
                        "swap_used_mb": round(
                            float(row["swap_used_mb"] if row else 0), 2
                        ),
                        "disk_total_gb": round(
                            float(row["disk_total_gb"] if row else 0), 2
                        ),
                        "disk_used_gb": round(
                            float(row["disk_used_gb"] if row else 0), 2
                        ),
                        "disk_free_gb": round(
                            float(row["disk_free_gb"] if row else 0), 2
                        ),
                        "gpu_available": bool(row["gpu_available"] if row else False),
                        "gpu_percent": round(
                            float(row["gpu_percent"] if row else 0), 2
                        ),
                        "network_upload_bps": round(
                            float(row["network_upload_bps"] if row else 0), 2
                        ),
                        "network_download_bps": round(
                            float(row["network_download_bps"] if row else 0), 2
                        ),
                        "network_upload_total_bytes": (
                            row["network_upload_total_bytes"] if row else 0
                        ),
                        "network_download_total_bytes": (
                            row["network_download_total_bytes"] if row else 0
                        ),
                        "cpu_cores": (row["cpu_cores"] if row else 0),
                        "cpu_model": (row["cpu_model"] if row else ""),
                        "load1": round(float(row["load1"] if row else 0), 2),
                        "load5": round(float(row["load5"] if row else 0), 2),
                        "load15": round(float(row["load15"] if row else 0), 2),
                        "os_info": (row["os_info"] if row else ""),
                    },
                    "disks": disks,
                    "gpus": gpus,
                    "network_usage_30d": network_usage,
                    "ssh": {
                        "host": ssh_host,
                        "port": ssh_port,
                        "user": ssh_user,
                        "fingerprint": (
                            profile.get("host_key_fingerprint")
                            or (row["host_key_fingerprint"] if row else "")
                            or ""
                        ),
                    },
                    "asklink": {
                        "id": profile.get("lianlian_id") or "",
                        "password_configured": bool(
                            profile.get("lianlian_password_enc")
                        ),
                    },
                    "location": {
                        "name": profile.get("location_name") or "",
                        "latitude": profile.get("latitude"),
                        "longitude": profile.get("longitude"),
                    },
                    "ports": ports,
                    "errors": errors,
                }
            )
        alert_cursor = await db.execute(
            """
            SELECT id, node_id, alert_type, severity, message, created_at,
                   fault_level
            FROM alerts ORDER BY created_at DESC, id DESC LIMIT ?
            """,
            (settings.dashboard_alert_limit,),
        )
        alerts = [dict(alert) for alert in await alert_cursor.fetchall()]
        feedback_cursor = await db.execute(
            """
            SELECT id, platform, session_id, sender_name, content, created_at
            FROM feedbacks ORDER BY created_at DESC, id DESC LIMIT ?
            """,
            (settings.dashboard_feedback_limit,),
        )
        feedbacks = [dict(item) for item in await feedback_cursor.fetchall()]
        ticket_cursor = await db.execute(
            """
            SELECT id, fingerprint, node_id, source, title, description,
                   severity, status, analysis, analysis_engine, remark,
                   ai_ignore_until, ai_ignore_reason, ai_reviewed_at,
                   first_seen, last_seen, updated_at, handled_at
            FROM tickets
            ORDER BY COALESCE(severity, 99), last_seen DESC
            LIMIT ?
            """,
            (settings.dashboard_ticket_limit,),
        )
        tickets = [dict(item) for item in await ticket_cursor.fetchall()]
        ticket_counts_cursor = await db.execute(
            """
            SELECT status, severity, COUNT(*) AS count
            FROM tickets GROUP BY status, severity
            """
        )
        ticket_counts = [dict(item) for item in await ticket_counts_cursor.fetchall()]
        unresolved = sum(
            item["count"]
            for item in ticket_counts
            if item["status"] in ("unhandled", "in_progress")
        )

    return {
        "now": now.isoformat(timespec="seconds"),
        "summary": {
            "total_servers": len(servers),
            "online_servers": online_count,
            "offline_servers": len(servers) - online_count,
            "recent_alerts": len(alerts),
            "feedbacks": len(feedbacks),
            "unresolved_tickets": unresolved,
        },
        "thresholds": {
            "cpu_percent": settings.cpu_threshold,
            "memory_percent": settings.memory_threshold,
            "disk_percent": settings.disk_threshold,
            "heartbeat_timeout_seconds": settings.heartbeat_timeout_seconds,
        },
        "servers": servers,
        "alerts": alerts,
        "feedbacks": feedbacks,
        "tickets": tickets,
        "ticket_counts": ticket_counts,
    }


async def create_server_profile(payload: dict[str, Any]) -> dict[str, Any]:
    name = str(payload.get("name") or payload.get("server_name") or "").strip()
    ssh_host, ssh_port = _split_ssh_endpoint(
        payload.get("ssh_host"), payload.get("ssh_port") or 22
    )
    if not name:
        raise ValueError("服务器名称不能为空")
    if not ssh_host:
        raise ValueError("SSH 地址不能为空")
    provider_url = _clean_provider_url(payload.get("provider_url"))
    location_name = str(payload.get("location_name") or "").strip()[:255]
    latitude = _optional_float(payload.get("latitude"))
    longitude = _optional_float(payload.get("longitude"))
    base_id = "".join(
        char if char.isalnum() or char in "-_." else "-" for char in name
    ).strip("-._") or "server"
    node_id = base_id[:80]
    now_iso = utc_now()
    async with _write_lock:
        async with connect() as db:
            for attempt in range(1, 100):
                candidate = node_id if attempt == 1 else f"{node_id}-{attempt}"
                exists = await db.execute(
                    "SELECT 1 FROM server_profiles WHERE node_id = ? "
                    "UNION SELECT 1 FROM servers WHERE node_id = ? LIMIT 1",
                    (candidate, candidate),
                )
                if not await exists.fetchone():
                    node_id = candidate
                    break
            await db.execute(
                """
                INSERT INTO server_profiles(
                    node_id, remark, ssh_host, ssh_port, ssh_user,
                    provider_url, lianlian_id, lianlian_password_enc,
                    location_name, latitude, longitude,
                    check_ports, log_files, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    node_id,
                    str(payload.get("remark") or name)[:255],
                    ssh_host[:255],
                    ssh_port,
                    str(payload.get("ssh_user") or "root")[:80],
                    provider_url,
                    str(payload.get("lianlian_id") or "")[:500],
                    encrypt_json({"value": str(payload.get("lianlian_password") or "")})
                    if payload.get("lianlian_password")
                    else "",
                    location_name,
                    latitude,
                    longitude,
                    str(payload.get("check_ports") or "22,80,443")[:500],
                    str(payload.get("log_files") or "")[:2000],
                    now_iso,
                    now_iso,
                ),
            )
            await db.execute(
                "DELETE FROM server_removals WHERE node_id = ?", (node_id,)
            )
            await db.commit()
    return {"node_id": node_id, "remark": str(payload.get("remark") or name)}


async def update_server_profile(node_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "remark",
        "ssh_host",
        "ssh_port",
        "ssh_user",
        "provider_url",
        "lianlian_id",
        "location_name",
        "latitude",
        "longitude",
        "check_ports",
        "log_files",
    }
    updates = {key: value for key, value in payload.items() if key in allowed}
    if not updates:
        raise ValueError("没有可更新的字段")
    if "ssh_port" in updates:
        updates["ssh_port"] = max(1, min(65535, int(updates["ssh_port"] or 22)))
    if "lianlian_id" in updates:
        updates["lianlian_id"] = str(updates["lianlian_id"] or "")[:500]
    if payload.get("lianlian_password"):
        updates["lianlian_password_enc"] = encrypt_json(
            {"value": str(payload["lianlian_password"])}
        )
    if "location_name" in updates:
        updates["location_name"] = str(updates["location_name"] or "").strip()[:255]
    if "latitude" in updates:
        updates["latitude"] = _optional_float(updates["latitude"])
    if "longitude" in updates:
        updates["longitude"] = _optional_float(updates["longitude"])
    if "ssh_host" in updates:
        parsed_host, parsed_port = _split_ssh_endpoint(
            updates["ssh_host"], updates.get("ssh_port", payload.get("ssh_port") or 22)
        )
        updates["ssh_host"] = parsed_host[:255]
        updates["ssh_port"] = parsed_port
    if "provider_url" in updates:
        updates["provider_url"] = _clean_provider_url(updates["provider_url"])
    updates["updated_at"] = utc_now()
    assignments = ", ".join(f"{key} = ?" for key in updates)
    async with _write_lock:
        async with connect() as db:
            cursor = await db.execute(
                f"UPDATE server_profiles SET {assignments} WHERE node_id = ?",
                (*updates.values(), node_id),
            )
            if cursor.rowcount == 0:
                values = {
                    "remark": payload.get("remark") or node_id,
                    "ssh_host": payload.get("ssh_host") or "",
                    "ssh_port": int(payload.get("ssh_port") or 22),
                    "ssh_user": payload.get("ssh_user") or "root",
                    "provider_url": _clean_provider_url(
                        payload.get("provider_url") or ""
                    ),
                    "lianlian_id": str(payload.get("lianlian_id") or "")[:500],
                    "lianlian_password_enc": (
                        encrypt_json(
                            {"value": str(payload.get("lianlian_password") or "")}
                        )
                        if payload.get("lianlian_password")
                        else ""
                    ),
                    "location_name": str(payload.get("location_name") or "")[:255],
                    "latitude": _optional_float(payload.get("latitude")),
                    "longitude": _optional_float(payload.get("longitude")),
                    "check_ports": payload.get("check_ports") or "",
                    "log_files": payload.get("log_files") or "",
                }
                now_iso = utc_now()
                await db.execute(
                    """
                    INSERT INTO server_profiles(
                        node_id, remark, ssh_host, ssh_port, ssh_user,
                        provider_url, lianlian_id, lianlian_password_enc,
                        location_name, latitude, longitude,
                        check_ports, log_files, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        node_id,
                        str(values["remark"])[:255],
                        str(values["ssh_host"])[:255],
                        values["ssh_port"],
                        str(values["ssh_user"])[:80],
                        str(values["provider_url"])[:1000],
                        str(values["lianlian_id"])[:500],
                        values["lianlian_password_enc"],
                        str(values["location_name"])[:255],
                        values["latitude"],
                        values["longitude"],
                        str(values["check_ports"])[:500],
                        str(values["log_files"])[:2000],
                        now_iso,
                        now_iso,
                    ),
                )
            await db.commit()
    return {"node_id": node_id, **updates}


async def delete_server(node_id: str) -> None:
    now_iso = utc_now()
    async with _write_lock:
        async with connect() as db:
            await db.execute("DELETE FROM tickets WHERE node_id = ?", (node_id,))
            await db.execute("DELETE FROM alerts WHERE node_id = ?", (node_id,))
            await db.execute(
                "DELETE FROM alert_cooldowns WHERE alert_key LIKE ?",
                (f"%{node_id}%",),
            )
            await db.execute("DELETE FROM server_profiles WHERE node_id = ?", (node_id,))
            await db.execute("DELETE FROM servers WHERE node_id = ?", (node_id,))
            await db.execute(
                """
                INSERT INTO server_removals(node_id, removed_at) VALUES (?, ?)
                ON CONFLICT(node_id) DO UPDATE SET removed_at = excluded.removed_at
                """,
                (node_id, now_iso),
            )
            await db.commit()


async def set_server_power_state(node_id: str, state: str) -> None:
    if state not in {"online", "rebooting", "off"}:
        raise ValueError("无效的电源状态")
    async with _write_lock:
        async with connect() as db:
            await db.execute(
                "UPDATE servers SET power_state = ? WHERE node_id = ?",
                (state, node_id),
            )
            await db.commit()


async def pin_server_host_key(node_id: str, fingerprint: str) -> None:
    now_iso = utc_now()
    async with _write_lock:
        async with connect() as db:
            cursor = await db.execute(
                "UPDATE server_profiles SET host_key_fingerprint = ?, updated_at = ? "
                "WHERE node_id = ?",
                (fingerprint, now_iso, node_id),
            )
            if cursor.rowcount == 0:
                await db.execute(
                    """
                    INSERT INTO server_profiles(
                        node_id, remark, host_key_fingerprint, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (node_id, node_id, fingerprint, now_iso, now_iso),
                )
            await db.execute(
                "UPDATE servers SET host_key_fingerprint = ? WHERE node_id = ?",
                (fingerprint, node_id),
            )
            await db.commit()


async def get_server_host_key(node_id: str) -> str:
    async with connect() as db:
        cursor = await db.execute(
            "SELECT COALESCE(p.host_key_fingerprint, s.host_key_fingerprint, '') "
            "FROM server_profiles p LEFT JOIN servers s USING(node_id) "
            "WHERE p.node_id = ?",
            (node_id,),
        )
        row = await cursor.fetchone()
        if row:
            return row[0]
        cursor = await db.execute(
            "SELECT host_key_fingerprint FROM servers WHERE node_id = ?", (node_id,)
        )
        row = await cursor.fetchone()
        return row[0] if row else ""


async def get_server_connection(node_id: str) -> dict[str, Any]:
    async with connect() as db:
        cursor = await db.execute(
            """
            SELECT
                COALESCE(NULLIF(p.ssh_host, ''), s.ssh_host, s.ip, '') AS host,
                COALESCE(NULLIF(p.ssh_port, 0), s.ssh_port, 22) AS port,
                COALESCE(NULLIF(p.ssh_user, ''), s.ssh_user, 'root') AS username,
                COALESCE(p.provider_url, '') AS provider_url,
                COALESCE(p.remark, s.hostname, p.node_id, ?) AS name
            FROM server_profiles p
            LEFT JOIN servers s USING(node_id)
            WHERE p.node_id = ?
            UNION ALL
            SELECT s.ssh_host, s.ssh_port, s.ssh_user, '', s.hostname
            FROM servers s
            WHERE s.node_id = ? AND NOT EXISTS (
                SELECT 1 FROM server_profiles p WHERE p.node_id = s.node_id
            )
            LIMIT 1
            """,
            (node_id, node_id, node_id),
        )
        row = await cursor.fetchone()
        if not row:
            raise ValueError("服务器连接不存在")
        host, port = _split_ssh_endpoint(row["host"], row["port"] or 22)
        return {
            "node_id": node_id,
            "host": host,
            "port": port,
            "username": row["username"] or "root",
            "provider_url": row["provider_url"] or "",
            "name": row["name"] or node_id,
        }


async def save_bot_config(payload: dict[str, Any], bot_id: str | None = None) -> str:
    platform = str(payload.get("platform") or "").strip().lower()
    if platform not in {"qq", "telegram", "discord", "wechat", "whatsapp"}:
        raise ValueError("不支持的 Bot 平台")
    config = payload.get("config")
    if not isinstance(config, dict):
        config = {}
    bot_id = bot_id or uuid.uuid4().hex[:12]
    now_iso = utc_now()
    async with _write_lock:
        async with connect() as db:
            if bot_id:
                existing_cursor = await db.execute(
                    "SELECT config_enc FROM bot_configs WHERE id = ?", (bot_id,)
                )
                existing = await existing_cursor.fetchone()
            else:
                existing = None
            final_config = config
            if existing:
                old = decrypt_json(existing["config_enc"])
                for key, value in config.items():
                    if value not in ("", None, "••••"):
                        old[key] = value
                final_config = old
            await db.execute(
                """
                INSERT INTO bot_configs(
                    id, platform, remark, enabled, config_enc, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    platform=excluded.platform,
                    remark=excluded.remark,
                    enabled=excluded.enabled,
                    config_enc=excluded.config_enc,
                    updated_at=excluded.updated_at
                """,
                (
                    bot_id,
                    platform,
                    str(payload.get("remark") or platform.upper())[:255],
                    1 if payload.get("enabled", True) else 0,
                    encrypt_json(final_config),
                    now_iso,
                    now_iso,
                ),
            )
            await db.commit()
    return bot_id


async def list_bot_configs(include_secrets: bool = False) -> list[dict[str, Any]]:
    async with connect() as db:
        cursor = await db.execute(
            "SELECT * FROM bot_configs ORDER BY created_at DESC"
        )
        rows = await cursor.fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        config = decrypt_json(row["config_enc"])
        if not include_secrets:
            config = {
                key: (
                    "••••"
                    if "token" in key.lower()
                    or "secret" in key.lower()
                    or "password" in key.lower()
                    else value
                )
                for key, value in config.items()
            }
        result.append(
            {
                "id": row["id"],
                "platform": row["platform"],
                "remark": row["remark"],
                "enabled": bool(row["enabled"]),
                "config": config,
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        )
    return result


async def delete_bot_config(bot_id: str) -> None:
    async with _write_lock:
        async with connect() as db:
            await db.execute("DELETE FROM bot_configs WHERE id = ?", (bot_id,))
            await db.commit()


async def list_tickets(
    status: str | None = None,
    severity: str | None = None,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    values: list[Any] = []
    if status:
        clauses.append("status = ?")
        values.append(status)
    if severity == "unclassified":
        clauses.append("severity IS NULL")
    elif severity and severity.isdigit():
        clauses.append("severity = ?")
        values.append(int(severity))
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    async with connect() as db:
        cursor = await db.execute(
            f"SELECT * FROM tickets{where} ORDER BY "
            "COALESCE(severity, 99), last_seen DESC LIMIT 1000",
            tuple(values),
        )
        return [dict(row) for row in await cursor.fetchall()]


async def update_ticket(ticket_id: int, payload: dict[str, Any]) -> None:
    updates: dict[str, Any] = {}
    if "status" in payload:
        status = str(payload["status"])
        if status not in {"unhandled", "in_progress", "resolved", "ignored"}:
            raise ValueError("无效的工单状态")
        updates["status"] = status
        updates["handled_at"] = utc_now() if status in {"resolved", "ignored"} else None
        if status in {"resolved", "ignored"}:
            updates["ai_ignore_until"] = (
                datetime.now(timezone.utc)
                + timedelta(seconds=settings.ai_classify_cooldown_seconds)
            ).isoformat(timespec="seconds")
            updates["ai_ignore_reason"] = "等待 AI 读取人员处理备注"
        else:
            updates["ai_ignore_until"] = None
            updates["ai_ignore_reason"] = ""
    if "severity" in payload:
        severity = payload["severity"]
        if severity in (None, "", "unclassified"):
            updates["severity"] = None
        else:
            parsed = int(severity)
            if parsed < 1 or parsed > 5:
                raise ValueError("故障等级必须为 1 到 5")
            updates["severity"] = parsed
    if "remark" in payload:
        updates["remark"] = str(payload["remark"] or "")[:4000]
    if not updates:
        return
    updates["updated_at"] = utc_now()
    assignments = ", ".join(f"{key} = ?" for key in updates)
    async with _write_lock:
        async with connect() as db:
            await db.execute(
                f"UPDATE tickets SET {assignments} WHERE id = ?",
                (*updates.values(), ticket_id),
            )
            await db.commit()


async def ai_review_ticket(ticket_id: int) -> dict[str, Any]:
    async with connect() as db:
        cursor = await db.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,))
        row = await cursor.fetchone()
    if not row:
        raise ValueError("工单不存在")
    ticket = dict(row)
    from core.ai_judge import review_ticket_content

    verdict = await review_ticket_content(ticket)
    if not verdict:
        raise RuntimeError("AI 复核失败")
    now = datetime.now(timezone.utc)
    ignore_until = (
        (now + timedelta(minutes=int(verdict["ignore_minutes"]))).isoformat(
            timespec="seconds"
        )
        if verdict.get("ignore")
        else None
    )
    async with _write_lock:
        async with connect() as db:
            await db.execute(
                """
                UPDATE tickets SET
                    severity = CASE
                        WHEN severity IS NULL OR severity > ? THEN ?
                        ELSE severity
                    END,
                    analysis = ?,
                    analysis_engine = 'ai',
                    ai_ignore_until = ?,
                    ai_ignore_reason = ?,
                    ai_reviewed_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    int(verdict["level"]),
                    int(verdict["level"]),
                    str(verdict["analysis"])[:100],
                    ignore_until,
                    "AI 复核：人员备注解释了当前故障"
                    if verdict.get("ignore")
                    else "",
                    now.isoformat(timespec="seconds"),
                    now.isoformat(timespec="seconds"),
                    ticket_id,
                ),
            )
            await db.commit()
    return {
        "ok": True,
        "ticket_id": ticket_id,
        **verdict,
        "ignore_until": ignore_until,
    }


async def save_game_server(payload: dict[str, Any], game_id: str | None = None) -> str:
    game_type = str(payload.get("game_type") or "").lower()
    if game_type not in {"minecraft", "gmod"}:
        raise ValueError("支持的游戏类型为 minecraft 或 gmod")
    host = str(payload.get("host") or "").strip()
    if not host:
        raise ValueError("游戏服务器地址不能为空")
    game_id = game_id or uuid.uuid4().hex[:12]
    secret = {"rcon_password": str(payload.get("rcon_password") or "")}
    now_iso = utc_now()
    async with _write_lock:
        async with connect() as db:
            if game_id:
                cursor = await db.execute(
                    "SELECT secret_enc FROM game_servers WHERE id = ?", (game_id,)
                )
                existing = await cursor.fetchone()
                if existing and not secret["rcon_password"]:
                    secret = decrypt_json(existing["secret_enc"])
            await db.execute(
                """
                INSERT INTO game_servers(
                    id, name, game_type, host, query_port, console_port,
                    remark, enabled, secret_enc, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name,
                    game_type=excluded.game_type,
                    host=excluded.host,
                    query_port=excluded.query_port,
                    console_port=excluded.console_port,
                    remark=excluded.remark,
                    enabled=excluded.enabled,
                    secret_enc=excluded.secret_enc,
                    updated_at=excluded.updated_at
                """,
                (
                    game_id,
                    str(payload.get("name") or host)[:255],
                    game_type,
                    host[:255],
                    max(1, min(65535, int(payload.get("query_port") or (25565 if game_type == "minecraft" else 27015)))),
                    max(0, min(65535, int(payload.get("console_port") or 0))),
                    str(payload.get("remark") or "")[:1000],
                    1 if payload.get("enabled", True) else 0,
                    encrypt_json(secret),
                    now_iso,
                    now_iso,
                ),
            )
            await db.commit()
    return game_id


async def list_game_servers(include_secret: bool = False) -> list[dict[str, Any]]:
    async with connect() as db:
        cursor = await db.execute(
            "SELECT * FROM game_servers ORDER BY created_at DESC"
        )
        rows = await cursor.fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        secret = decrypt_json(row["secret_enc"])
        try:
            status = json.loads(row["status_json"])
        except (json.JSONDecodeError, TypeError):
            status = {}
        result.append(
            {
                "id": row["id"],
                "name": row["name"],
                "game_type": row["game_type"],
                "host": row["host"],
                "query_port": row["query_port"],
                "console_port": row["console_port"],
                "remark": row["remark"],
                "enabled": bool(row["enabled"]),
                "rcon_password": secret.get("rcon_password", "")
                if include_secret
                else ("••••" if secret.get("rcon_password") else ""),
                "status": status,
                "last_checked": row["last_checked"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        )
    return result


async def delete_game_server(game_id: str) -> None:
    async with _write_lock:
        async with connect() as db:
            await db.execute("DELETE FROM game_servers WHERE id = ?", (game_id,))
            await db.commit()


async def save_game_status(game_id: str, status: dict[str, Any]) -> None:
    now_iso = utc_now()
    async with _write_lock:
        async with connect() as db:
            await db.execute(
                "UPDATE game_servers SET status_json = ?, last_checked = ?, "
                "updated_at = ? WHERE id = ?",
                (json.dumps(status, ensure_ascii=False), now_iso, now_iso, game_id),
            )
            await db.commit()
