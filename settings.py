from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _csv(name: str) -> tuple[str, ...]:
    return tuple(
        item.strip()
        for item in _env(name).replace("\n", ",").split(",")
        if item.strip()
    )


@dataclass(frozen=True)
class Settings:
    base_dir: Path = BASE_DIR
    admin_key: str = field(default_factory=lambda: _env("ADMIN_KEY"))
    agent_key: str = field(default_factory=lambda: _env("AGENT_KEY"))
    config_master_key: str = field(
        default_factory=lambda: _env("CONFIG_MASTER_KEY")
    )
    app_name: str = field(default_factory=lambda: _env("APP_NAME"))
    app_owner: str = field(default_factory=lambda: _env("APP_OWNER"))
    app_contact_text: str = field(
        default_factory=lambda: _env("APP_CONTACT_TEXT")
    )
    app_contact_url: str = field(
        default_factory=lambda: _env("APP_CONTACT_URL")
    )
    database_path: Path = field(
        default_factory=lambda: Path(
            _env("DATABASE_PATH", str(BASE_DIR / "data" / "ops_hub.sqlite3"))
        )
    )
    bind_host: str = field(default_factory=lambda: _env("BIND_HOST", "127.0.0.1"))
    bind_port: int = field(default_factory=lambda: _int("BIND_PORT", 8080))
    public_base_url: str = field(
        default_factory=lambda: _env("PUBLIC_BASE_URL").rstrip("/")
    )
    cors_origins: tuple[str, ...] = field(
        default_factory=lambda: _csv("CORS_ORIGINS")
    )
    service_name: str = field(default_factory=lambda: _env("SERVICE_NAME"))
    heartbeat_timeout_seconds: int = field(
        default_factory=lambda: _int("HEARTBEAT_TIMEOUT_SECONDS", 120)
    )
    alert_cooldown_seconds: int = field(
        default_factory=lambda: _int("ALERT_COOLDOWN_SECONDS", 600)
    )
    cpu_threshold: float = field(
        default_factory=lambda: _float("CPU_THRESHOLD", 90)
    )
    memory_threshold: float = field(
        default_factory=lambda: _float("MEMORY_THRESHOLD", 90)
    )
    disk_threshold: float = field(
        default_factory=lambda: _float("DISK_THRESHOLD", 90)
    )
    offline_check_interval_seconds: int = field(
        default_factory=lambda: _int("OFFLINE_CHECK_INTERVAL_SECONDS", 30)
    )
    dashboard_feedback_limit: int = field(
        default_factory=lambda: _int("DASHBOARD_FEEDBACK_LIMIT", 200)
    )
    dashboard_alert_limit: int = field(
        default_factory=lambda: _int("DASHBOARD_ALERT_LIMIT", 100)
    )
    dashboard_ticket_limit: int = field(
        default_factory=lambda: _int("DASHBOARD_TICKET_LIMIT", 300)
    )
    dashboard_error_limit: int = field(
        default_factory=lambda: _int("DASHBOARD_ERROR_LIMIT", 20)
    )
    ai_api_key: str = field(default_factory=lambda: _env("AI_API_KEY"))
    ai_api_base: str = field(
        default_factory=lambda: _env("AI_API_BASE").rstrip("/")
    )
    ai_model: str = field(default_factory=lambda: _env("AI_MODEL"))
    ai_timeout_seconds: float = field(
        default_factory=lambda: _float("AI_TIMEOUT_SECONDS", 10)
    )
    ai_classify_cooldown_seconds: int = field(
        default_factory=lambda: _int("AI_CLASSIFY_COOLDOWN_SECONDS", 1200)
    )

    # Social platform credentials and destinations. These remain blank by default.
    qq_group_ids: tuple[str, ...] = field(default_factory=lambda: _csv("QQ_GROUP_IDS"))
    telegram_bot_token: str = field(default_factory=lambda: _env("TELEGRAM_BOT_TOKEN"))
    telegram_chat_ids: tuple[str, ...] = field(
        default_factory=lambda: _csv("TELEGRAM_CHAT_IDS")
    )
    discord_bot_token: str = field(
        default_factory=lambda: _env("DISCORD_BOT_TOKEN")
    )
    discord_channel_ids: tuple[str, ...] = field(
        default_factory=lambda: _csv("DISCORD_CHANNEL_IDS")
    )
    wechat_provider: str = field(
        default_factory=lambda: _env("WECHAT_PROVIDER", "generic")
    )
    wechat_send_url: str = field(default_factory=lambda: _env("WECHAT_SEND_URL"))
    wechat_api_token: str = field(
        default_factory=lambda: _env("WECHAT_API_TOKEN")
    )
    wechat_app_id: str = field(default_factory=lambda: _env("WECHAT_APP_ID"))
    wechat_receive_ids: tuple[str, ...] = field(
        default_factory=lambda: _csv("WECHAT_RECEIVE_IDS")
    )
    wechat_webhook_secret: str = field(
        default_factory=lambda: _env("WECHAT_WEBHOOK_SECRET")
    )
    whatsapp_token: str = field(default_factory=lambda: _env("WHATSAPP_TOKEN"))
    whatsapp_phone_number_id: str = field(
        default_factory=lambda: _env("WHATSAPP_PHONE_NUMBER_ID")
    )
    whatsapp_recipients: tuple[str, ...] = field(
        default_factory=lambda: _csv("WHATSAPP_RECIPIENTS")
    )
    whatsapp_verify_token: str = field(
        default_factory=lambda: _env("WHATSAPP_VERIFY_TOKEN")
    )
    whatsapp_app_secret: str = field(
        default_factory=lambda: _env("WHATSAPP_APP_SECRET")
    )
    whatsapp_graph_version: str = field(
        default_factory=lambda: _env("WHATSAPP_GRAPH_VERSION", "v23.0")
    )


settings = Settings()

if not settings.admin_key:
    raise RuntimeError(f"ADMIN_KEY is missing from {BASE_DIR / '.env'}")
if not settings.agent_key:
    raise RuntimeError(f"AGENT_KEY is missing from {BASE_DIR / '.env'}")
if not settings.config_master_key:
    raise RuntimeError(f"CONFIG_MASTER_KEY is missing from {BASE_DIR / '.env'}")
