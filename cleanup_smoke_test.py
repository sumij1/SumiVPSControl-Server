#!/usr/bin/env python3
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from dotenv import dotenv_values

ENV = dotenv_values(Path(__file__).resolve().parents[1] / ".env")
path = ENV.get("DATABASE_PATH") or os.getenv(
    "DATABASE_PATH", "data/ops_hub.sqlite3"
)
con = sqlite3.connect(path)
con.execute("PRAGMA foreign_keys=ON")
with con:
    con.execute("DELETE FROM alerts WHERE node_id LIKE 'smoke-test-%'")
    con.execute(
        "DELETE FROM alert_cooldowns WHERE alert_key LIKE '%smoke-test-%'"
    )
    con.execute(
        "DELETE FROM feedbacks "
        "WHERE session_id = 'self-test-room' OR content = '部署自测反馈'"
    )
    con.execute("DELETE FROM servers WHERE node_id LIKE 'smoke-test-%'")
print("servers", con.execute("SELECT node_id FROM servers ORDER BY node_id").fetchall())
print("alerts", con.execute("SELECT COUNT(*) FROM alerts").fetchone()[0])
print("feedbacks", con.execute("SELECT COUNT(*) FROM feedbacks").fetchone()[0])
con.close()
