from __future__ import annotations

import ipaddress
import logging
import socket
from typing import Any

import httpx

from core.database import connect, utc_now

logger = logging.getLogger("ops_hub.geolocation")


async def _cached_location(ip: str) -> dict[str, Any] | None:
    async with connect() as db:
        cursor = await db.execute("SELECT * FROM server_locations WHERE ip = ?", (ip,))
        row = await cursor.fetchone()
    return dict(row) if row else None


async def _store_location(ip: str, location: dict[str, Any]) -> None:
    async with connect() as db:
        await db.execute(
            """
            INSERT INTO server_locations(
                ip, location_name, country, region, city,
                latitude, longitude, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ip) DO UPDATE SET
                location_name=excluded.location_name,
                country=excluded.country,
                region=excluded.region,
                city=excluded.city,
                latitude=excluded.latitude,
                longitude=excluded.longitude,
                updated_at=excluded.updated_at
            """,
            (
                ip,
                location["location_name"],
                location["country"],
                location["region"],
                location["city"],
                location["latitude"],
                location["longitude"],
                utc_now(),
            ),
        )
        await db.commit()


async def geolocate_server(host: str) -> dict[str, Any] | None:
    host = str(host or "").strip()
    if not host:
        return None
    try:
        ipaddress.ip_address(host)
        ip = host
    except ValueError:
        try:
            ip = socket.gethostbyname(host)
        except OSError:
            return None
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return None
    if not address.is_global:
        return None
    cached = await _cached_location(ip)
    if cached:
        return {
            "ip": ip,
            "location_name": cached["location_name"],
            "country": cached["country"],
            "region": cached["region"],
            "city": cached["city"],
            "latitude": cached["latitude"],
            "longitude": cached["longitude"],
            "source": "cache",
        }
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.get(f"https://ipwho.is/{ip}")
            response.raise_for_status()
            payload = response.json()
        if not payload.get("success"):
            return None
        city = str(payload.get("city") or "")
        region = str(payload.get("region") or "")
        country = str(payload.get("country") or "")
        label = ", ".join(part for part in (city, region, country) if part)
        location = {
            "location_name": label or "未知位置",
            "country": country,
            "region": region,
            "city": city,
            "latitude": float(payload["latitude"]),
            "longitude": float(payload["longitude"]),
        }
        await _store_location(ip, location)
        return {**location, "ip": ip, "source": "ipwho.is"}
    except Exception:
        logger.exception("IP geolocation failed for %s", ip)
        return None
