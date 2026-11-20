from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any

import httpx

from core.settings import settings

logger = logging.getLogger("ops_hub.ai")

SYSTEM_PROMPT = (
    "你是服务器故障分级器。只输出JSON："
    '{"level":整数1-5,"analysis":"不超过100个中文字符"}。'
    "1=服务器死亡或停摆；2=安全风险；3=已经出现的故障；"
    "4=可能导致故障；5=不重要、可修可不修。"
    "必须选最接近的一级。analysis只写关键证据和建议，不用Markdown。"
)


def _clean_analysis(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:100]


async def _judge(payload: dict[str, Any]) -> dict[str, Any] | None:
    if not settings.ai_api_key:
        return None
    request_body = {
        "model": settings.ai_model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            },
        ],
        "temperature": 0,
        "max_tokens": 120,
        "response_format": {"type": "json_object"},
        "stream": False,
    }
    try:
        async with httpx.AsyncClient(timeout=settings.ai_timeout_seconds) as client:
            response = await client.post(
                f"{settings.ai_api_base}/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.ai_api_key}",
                    "Content-Type": "application/json",
                },
                json=request_body,
            )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        level = max(1, min(5, int(parsed["level"])))
        analysis = _clean_analysis(parsed.get("analysis"))
        if not analysis:
            return None
        return {"level": level, "analysis": analysis, "model": settings.ai_model}
    except Exception:
        logger.exception("AI fault classification failed")
        return None


async def classify_telemetry(report: dict[str, Any], events: list[str]) -> dict[str, Any] | None:
    errors = [
        str(item.get("message") or "")[:180]
        for item in report.get("errors", [])[:3]
    ]
    down_ports = [
        int(item["port"])
        for item in report.get("ports", [])
        if not item.get("alive")
    ]
    payload = {
        "cpu": round(float(report.get("cpu_percent") or 0), 1),
        "mem": round(float(report.get("memory_percent") or 0), 1),
        "disk": round(float(report.get("disk_percent") or 0), 1),
        "latency_ms": round(float(report.get("report_latency_ms") or 0), 1),
        "down_ports": down_ports,
        "errors": errors,
        "events": [str(event)[:120] for event in events[:6]],
    }
    return await _judge(payload)


async def classify_feedback(content: str, platform: str) -> dict[str, Any] | None:
    return await _judge(
        {
            "source": platform[:20],
            "feedback": str(content or "")[:600],
        }
    )


async def review_ticket_content(ticket: dict[str, Any]) -> dict[str, Any] | None:
    if not settings.ai_api_key:
        return None
    prompt = (
        '只输出JSON：{"level":1-5,"analysis":"不超过100字",'
        '"ignore":true或false,"ignore_minutes":整数0-1440}。'
        "1=停摆死亡，2=安全风险，3=已出现故障，4=可能故障，5=不重要。"
        "结合处理状态、当前时间和人员备注重判。若备注明确解释为计划维护、"
        "已知限制、临时处理或重复误报，可ignore=true；1级只有在备注明确给出"
        "未来维护时间窗时才能忽略，2级安全风险必须ignore=false。"
    )
    payload = {
        "title": str(ticket.get("title") or "")[:180],
        "summary": str(ticket.get("description") or "")[:700],
        "previous_analysis": str(ticket.get("analysis") or "")[:180],
        "status": str(ticket.get("status") or ""),
        "staff_remark": str(ticket.get("remark") or "")[:700],
        "now": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
        "timezone": "Asia/Shanghai",
    }
    body = {
        "model": settings.ai_model,
        "messages": [
            {"role": "system", "content": prompt},
            {
                "role": "user",
                "content": json.dumps(
                    payload, ensure_ascii=False, separators=(",", ":")
                ),
            },
        ],
        "temperature": 0,
        "max_tokens": 160,
        "response_format": {"type": "json_object"},
        "stream": False,
    }
    try:
        async with httpx.AsyncClient(timeout=settings.ai_timeout_seconds) as client:
            response = await client.post(
                f"{settings.ai_api_base}/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.ai_api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        level = max(1, min(5, int(parsed["level"])))
        ignore = bool(parsed.get("ignore"))
        ignore_minutes = max(0, min(1440, int(parsed.get("ignore_minutes") or 0)))
        if level == 2:
            ignore = False
            ignore_minutes = 0
        if ignore and ignore_minutes == 0:
            ignore_minutes = 20
        return {
            "level": level,
            "analysis": _clean_analysis(parsed.get("analysis"))
            or "AI 已完成复核，请结合人员备注执行。",
            "ignore": ignore,
            "ignore_minutes": ignore_minutes,
            "model": settings.ai_model,
        }
    except Exception:
        logger.exception("AI ticket review failed")
        return None


def _clean_paths(value: Any) -> list[str]:
    raw = str(value or "").replace("\n", ",").split(",")
    paths: list[str] = []
    for item in raw:
        path = item.strip()
        if not path.startswith("/") or ".." in path or len(path) > 240:
            continue
        if not re.fullmatch(r"/[A-Za-z0-9_./@+-]+", path):
            continue
        if path not in paths:
            paths.append(path)
    return paths[:8]


async def infer_log_paths(
    context: dict[str, Any],
    allow_web_search: bool = False,
) -> dict[str, Any] | None:
    if not settings.ai_api_key:
        return None
    prompt = (
        '只输出JSON：{"paths":"绝对路径逗号分隔","analysis":"不超过100字"}。'
        "根据服务器名称、域名、端口和备注推断最可能的错误日志文件。"
        "只给常见路径，最多6个；不确定时给通用系统日志。"
    )
    user_payload = {
        "name": str(context.get("name") or "")[:100],
        "ssh_host": str(context.get("ssh_host") or "")[:180],
        "ports": str(context.get("check_ports") or "")[:100],
        "remark": str(context.get("remark") or "")[:180],
    }
    body: dict[str, Any] = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": prompt},
            {
                "role": "user",
                "content": json.dumps(
                    user_payload, ensure_ascii=False, separators=(",", ":")
                ),
            },
        ],
        "temperature": 0,
        "max_tokens": 140,
        "response_format": {"type": "json_object"},
        "stream": False,
    }
    used_web_search = False
    if allow_web_search:
        body.update(
            {
                "model": "deepseek-flash",
                "thinking": {"type": "disabled"},
                "tools": [{"type": "deepseek_web_search"}],
                "max_tokens": 320,
            }
        )
        body.pop("response_format", None)
        used_web_search = True
    try:
        async with httpx.AsyncClient(timeout=max(settings.ai_timeout_seconds, 15)) as client:
            response = await client.post(
                f"{settings.ai_api_base}/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.ai_api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", content, re.DOTALL)
            if not match:
                raise
            parsed = json.loads(match.group(0))
        paths = _clean_paths(parsed.get("paths"))
        if not paths:
            paths = ["/var/log/syslog", "/var/log/auth.log"]
        return {
            "paths": paths,
            "analysis": _clean_analysis(parsed.get("analysis"))
            or "AI 推断结果可能不准确，请在目标服务器核实。",
            "web_search_used": used_web_search,
            "model": body["model"],
        }
    except Exception:
        logger.exception("AI log path inference failed")
        return None
