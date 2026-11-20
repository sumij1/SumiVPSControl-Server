from __future__ import annotations

import asyncio
import json
import logging
import os

import nonebot
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from nonebot.adapters.discord import Adapter as DiscordAdapter
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter
from nonebot.adapters.telegram import Adapter as TelegramAdapter

from core.api import router as api_router
from core.bot_runtime import load_bot_runtime_env
from core.database import init_db, offline_monitor_loop
from core.settings import BASE_DIR, settings

# The Telegram and Discord adapters consume list-based environment settings.
# Build those structures from the simple, single-token placeholders in .env.
if settings.telegram_bot_token and not os.getenv("TELEGRAM_BOTS"):
    os.environ["TELEGRAM_BOTS"] = json.dumps(
        [{"token": settings.telegram_bot_token, "is_webhook": False}],
        ensure_ascii=False,
    )
if settings.discord_bot_token and not os.getenv("DISCORD_BOTS"):
    os.environ["DISCORD_BOTS"] = json.dumps(
        [
            {
                "token": settings.discord_bot_token,
                "intent": {"message_content": True},
            }
        ],
        ensure_ascii=False,
    )
load_bot_runtime_env(settings.database_path)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("ops_hub")

nonebot.init()
driver = nonebot.get_driver()
driver.register_adapter(OneBotV11Adapter)
driver.register_adapter(TelegramAdapter)
driver.register_adapter(DiscordAdapter)

# Import after NoneBot initialization so matchers are registered on the active driver.
from plugins import feedback as _feedback  # noqa: E402,F401


def _server_app() -> FastAPI:
    app = getattr(driver, "server_app", None)
    if app is None:
        app = getattr(driver, "_server_app", None)
    if app is None:
        raise RuntimeError("NoneBot FastAPI server_app is unavailable")
    return app


app = _server_app()
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Agent-Key"],
    expose_headers=["Content-Length"],
    max_age=3600,
)
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.include_router(api_router)
app.mount(
    "/vendor",
    StaticFiles(directory=BASE_DIR / "frontend" / "vendor"),
    name="vendor",
)


@app.get("/", include_in_schema=False)
@app.get("/index.html", include_in_schema=False)
async def web_client() -> FileResponse:
    index_path = BASE_DIR / "frontend" / "index.html"
    if not index_path.exists():
        raise RuntimeError("frontend/index.html is missing")
    return FileResponse(
        index_path,
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/config.js", include_in_schema=False)
async def web_config() -> Response:
    public_config = {
        "apiBase": settings.public_base_url,
        "appName": settings.app_name,
        "ownerName": settings.app_owner,
        "contactText": settings.app_contact_text,
        "contactUrl": settings.app_contact_url,
    }
    script = (
        "window.APP_CONFIG = Object.freeze("
        + json.dumps(public_config, ensure_ascii=False, separators=(",", ":"))
        + ");\n"
    )
    return Response(
        script,
        media_type="application/javascript; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )

_stop_event = asyncio.Event()
_monitor_task: asyncio.Task[None] | None = None


@driver.on_startup
async def _startup() -> None:
    global _monitor_task
    await init_db()
    _stop_event.clear()
    _monitor_task = asyncio.create_task(
        offline_monitor_loop(_stop_event), name="ops-hub-offline-monitor"
    )
    logger.info("Ops Hub started from %s", BASE_DIR)


@driver.on_shutdown
async def _shutdown() -> None:
    _stop_event.set()
    if _monitor_task and not _monitor_task.done():
        _monitor_task.cancel()
        try:
            await _monitor_task
        except asyncio.CancelledError:
            pass
    logger.info("Ops Hub stopped")
