from __future__ import annotations

import re
from typing import Any

from nonebot import on_message
from nonebot.adapters import Bot, Event

from core.database import save_feedback

feedback_matcher = on_message(priority=20, block=False)
FEEDBACK_RE = re.compile(r"^(?:/feedback|#反馈)\s*(.+)$", re.IGNORECASE | re.DOTALL)


def _platform_name(bot: Bot) -> str:
    name = bot.adapter.get_name().lower()
    if "onebot" in name or "qq" in name:
        return "QQ"
    if "telegram" in name:
        return "TG"
    if "discord" in name:
        return "Discord"
    return bot.adapter.get_name()


def _sender_name(event: Event, fallback: str) -> str:
    sender: Any = getattr(event, "sender", None)
    if sender is not None:
        for key in ("card", "nickname", "name", "username", "display_name"):
            value = getattr(sender, key, None)
            if value:
                return str(value)
    for key in ("sender_name", "username"):
        value = getattr(event, key, None)
        if value:
            return str(value)
    return fallback


@feedback_matcher.handle()
async def handle_feedback(bot: Bot, event: Event) -> None:
    text = event.get_plaintext().strip()
    match = FEEDBACK_RE.match(text)
    if not match:
        return
    content = match.group(1).strip()
    if not content:
        return
    try:
        session_id = event.get_session_id()
    except Exception:
        session_id = "unknown"
    sender_id = event.get_user_id()
    await save_feedback(
        _platform_name(bot),
        session_id,
        _sender_name(event, sender_id),
        content,
    )
    await feedback_matcher.send("已收到反馈")
