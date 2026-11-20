from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from core.secrets import decrypt_json


def load_bot_runtime_env(database_path: Path) -> None:
    """Populate NoneBot adapter tokens from enabled encrypted Bot records."""
    if not database_path.exists():
        return
    try:
        connection = sqlite3.connect(database_path)
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT platform, config_enc FROM bot_configs WHERE enabled = 1 "
            "ORDER BY created_at"
        ).fetchall()
        connection.close()
    except sqlite3.Error:
        return

    telegram_bots: list[dict[str, object]] = []
    discord_bots: list[dict[str, object]] = []
    onebot_token = ""
    for row in rows:
        try:
            config = decrypt_json(row["config_enc"])
        except RuntimeError:
            continue
        platform = row["platform"]
        if platform == "telegram" and config.get("token"):
            telegram_bots.append(
                {"token": str(config["token"]), "is_webhook": False}
            )
        elif platform == "discord" and config.get("token"):
            discord_bots.append(
                {
                    "token": str(config["token"]),
                    "intent": {"message_content": True},
                }
            )
        elif platform == "qq" and config.get("access_token") and not onebot_token:
            onebot_token = str(config["access_token"])

    if telegram_bots:
        os.environ["TELEGRAM_BOTS"] = json.dumps(telegram_bots, ensure_ascii=False)
    if discord_bots:
        os.environ["DISCORD_BOTS"] = json.dumps(discord_bots, ensure_ascii=False)
    if onebot_token:
        os.environ["ONEBOT_V11_ACCESS_TOKEN"] = onebot_token
