from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Sequence
from typing import Any

import httpx
import nonebot

from core.settings import settings

logger = logging.getLogger("ops_hub.notifier")


async def _database_bot_configs(
    platform: str | None = None,
) -> list[dict[str, Any]]:
    from core.database import list_bot_configs

    try:
        configs = await list_bot_configs(include_secrets=True)
    except Exception:
        logger.exception("Could not load Bot configuration")
        return []
    return [
        item
        for item in configs
        if item["enabled"] and (platform is None or item["platform"] == platform)
    ]


def _targets(config: dict[str, Any]) -> list[str]:
    raw = config.get("targets", [])
    if isinstance(raw, str):
        raw = raw.replace("\n", ",").split(",")
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


def _chunks(items: Sequence[str], size: int = 4000) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    length = 0
    for item in items:
        if current and length + len(item) + 1 > size:
            chunks.append("\n".join(current))
            current = []
            length = 0
        current.append(item)
        length += len(item) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


async def _send_qq(text: str) -> None:
    database_configs = await _database_bot_configs("qq")
    group_ids = list(settings.qq_group_ids)
    for item in database_configs:
        group_ids.extend(_targets(item["config"]))
    group_ids = list(dict.fromkeys(group_ids))
    if not group_ids:
        return
    onebot_bots = [
        bot
        for bot in nonebot.get_bots().values()
        if "onebot" in bot.adapter.get_name().lower()
        or "onebot" in bot.__class__.__module__.lower()
    ]
    if not onebot_bots:
        logger.info("QQ delivery skipped: no reverse WebSocket OneBot bot is connected")
        return
    for group_id in group_ids:
        try:
            numeric_group_id = int(group_id)
        except ValueError:
            logger.warning("Ignoring non-numeric QQ group ID: %s", group_id)
            continue
        for bot in onebot_bots:
            try:
                await bot.call_api(
                    "send_group_msg", group_id=numeric_group_id, message=text
                )
            except Exception:
                logger.exception("QQ group send failed for group %s", group_id)


async def _send_telegram(client: httpx.AsyncClient, text: str) -> None:
    deliveries: list[tuple[str, str]] = []
    if settings.telegram_bot_token:
        deliveries.extend(
            (settings.telegram_bot_token, chat_id)
            for chat_id in settings.telegram_chat_ids
        )
    for item in await _database_bot_configs("telegram"):
        token = str(item["config"].get("token") or "")
        deliveries.extend((token, chat_id) for chat_id in _targets(item["config"]))
    for token, chat_id in dict.fromkeys(deliveries):
        if not token:
            continue
        response = await client.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": True,
            },
        )
        if response.is_error:
            logger.error(
                "Telegram delivery failed (%s): %s",
                response.status_code,
                response.text[:300],
            )


async def _send_discord(client: httpx.AsyncClient, text: str) -> None:
    deliveries: list[tuple[str, str]] = []
    if settings.discord_bot_token:
        deliveries.extend(
            (settings.discord_bot_token, channel_id)
            for channel_id in settings.discord_channel_ids
        )
    for item in await _database_bot_configs("discord"):
        token = str(item["config"].get("token") or "")
        deliveries.extend(
            (token, channel_id) for channel_id in _targets(item["config"])
        )
    for token, channel_id in dict.fromkeys(deliveries):
        if not token:
            continue
        headers = {
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "OpsHub/1.0",
        }
        response = await client.post(
            f"https://discord.com/api/v10/channels/{channel_id}/messages",
            headers=headers,
            json={"content": text[:2000], "allowed_mentions": {"parse": []}},
        )
        if response.is_error:
            logger.error(
                "Discord delivery failed (%s): %s",
                response.status_code,
                response.text[:300],
            )


async def send_wechat_text(
    text: str, recipient: str, client: httpx.AsyncClient | None = None
) -> dict[str, Any]:
    send_url = settings.wechat_send_url
    api_token = settings.wechat_api_token
    app_id = settings.wechat_app_id
    provider = settings.wechat_provider
    if not send_url:
        configs = await _database_bot_configs("wechat")
        if configs:
            config = configs[0]["config"]
            send_url = str(config.get("send_url") or "")
            api_token = str(config.get("api_token") or "")
            app_id = str(config.get("app_id") or "")
            provider = str(config.get("provider") or "generic")
    if not send_url or not recipient:
        return {"sent": False, "reason": "WECHAT_SEND_URL or recipient is empty"}
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=15.0)
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if api_token:
        headers["Authorization"] = f"Bearer {api_token}"
        headers["X-Auth-Token"] = api_token
    payload = {
        "provider": provider,
        "appId": app_id,
        "app_id": app_id,
        "toWxid": recipient,
        "receiver": recipient,
        "to": recipient,
        "chat_id": recipient,
        "content": text,
        "text": text,
        "msg": text,
    }
    try:
        response = await client.post(send_url, headers=headers, json=payload)
        if response.is_error:
            logger.error(
                "WeChat delivery failed (%s): %s",
                response.status_code,
                response.text[:300],
            )
            return {"sent": False, "status": response.status_code}
        return {"sent": True, "status": response.status_code}
    except Exception as exc:
        logger.exception("WeChat delivery request failed")
        return {"sent": False, "reason": str(exc)}
    finally:
        if owns_client:
            await client.aclose()


async def _send_wechat(client: httpx.AsyncClient, text: str) -> None:
    recipients = list(settings.wechat_receive_ids)
    for item in await _database_bot_configs("wechat"):
        recipients.extend(_targets(item["config"]))
    for recipient in dict.fromkeys(recipients):
        await send_wechat_text(text, recipient, client)


async def send_whatsapp_text(
    text: str, recipient: str, client: httpx.AsyncClient | None = None
) -> dict[str, Any]:
    token = settings.whatsapp_token
    phone_number_id = settings.whatsapp_phone_number_id
    graph_version = settings.whatsapp_graph_version
    if not token or not phone_number_id:
        configs = await _database_bot_configs("whatsapp")
        if configs:
            config = configs[0]["config"]
            token = str(config.get("token") or "")
            phone_number_id = str(config.get("phone_number_id") or "")
            graph_version = str(config.get("graph_version") or "v23.0")
    if not token or not phone_number_id or not recipient:
        return {"sent": False, "reason": "WhatsApp credentials or recipient is empty"}
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=15.0)
    url = (
        "https://graph.facebook.com/"
        f"{graph_version}/"
        f"{phone_number_id}/messages"
    )
    try:
        response = await client.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": recipient,
                "type": "text",
                "text": {"preview_url": False, "body": text},
            },
        )
        if response.is_error:
            logger.error(
                "WhatsApp delivery failed (%s): %s",
                response.status_code,
                response.text[:300],
            )
            return {"sent": False, "status": response.status_code}
        return {"sent": True, "status": response.status_code}
    except Exception as exc:
        logger.exception("WhatsApp delivery request failed")
        return {"sent": False, "reason": str(exc)}
    finally:
        if owns_client:
            await client.aclose()


async def _send_whatsapp(client: httpx.AsyncClient, text: str) -> None:
    recipients = list(settings.whatsapp_recipients)
    for item in await _database_bot_configs("whatsapp"):
        recipients.extend(_targets(item["config"]))
    for recipient in dict.fromkeys(recipients):
        await send_whatsapp_text(text, recipient, client)


async def broadcast_text(text: str) -> None:
    text = text.strip()
    if not text:
        return
    async with httpx.AsyncClient(timeout=15.0) as client:
        tasks = [
            _send_qq(text),
            _send_telegram(client, text),
            _send_discord(client, text),
            _send_wechat(client, text),
            _send_whatsapp(client, text),
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
    for result in results:
        if isinstance(result, Exception):
            logger.error("A notification channel failed", exc_info=result)


async def broadcast_alerts(alert_texts: Sequence[str]) -> None:
    for text in _chunks(list(alert_texts)):
        await broadcast_text(text)


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
