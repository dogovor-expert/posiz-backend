"""Редирект-сервис коротких ссылок (posiz.ru).

Зачем: атрибуция в продукте не должна зависеть от выгрузки Метрики клиента.
Каждая ссылка в публикации получает код; переход ``GET /l/{code}`` логируется
(``analytics_events`` в проде, ClickHouse для больших объёмов) и отдаёт 302 на сайт.

Свойства, которые важны с первого дня:
* 302, а не 301 — иначе браузер закэширует и клики перестанут считаться;
* код без похожих символов (0/O, 1/l/I) — ссылки диктуют голосом и переписывают руками;
* ``utm_*`` дописываются на редиректе, даже если в цели их не было;
* клик не блокирует редирект: запись уходит в очередь, ответ отдаётся сразу.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .config import settings

_ALPHABET = "abcdefghijkmnpqrstuvwxyz23456789"   # без 0/o, 1/l/i


def _code(length: int = 7) -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(length))


def with_utm(target_url: str, channel: str, campaign: str, content: str | None = None) -> str:
    parts = urlsplit(target_url if "://" in target_url else f"https://{target_url}")
    query = dict(parse_qsl(parts.query))
    query.setdefault("utm_source", channel)
    query.setdefault("utm_medium", "social")
    query.setdefault("utm_campaign", campaign)
    if content:
        query.setdefault("utm_content", content)
    return urlunsplit(parts._replace(query=urlencode(query)))


def short_url(code: str) -> str:
    return f"https://{settings.link_domain}/l/{code}"


def build(target_url: str, channel: str, campaign: str, variant_id: str | None = None,
          publication_id: str | None = None) -> dict:
    """Создаёт короткую ссылку. Полный URL хранится с UTM-метками."""
    code = _code()
    full = with_utm(target_url, channel, campaign, content=variant_id)
    return {
        "code": code,
        "short_url": short_url(code),
        "target_url": full,
        "original_url": target_url,
        "channel": channel,
        "campaign": campaign,
        "variant_id": variant_id,
        "publication_id": publication_id,
        "created_at": datetime.now(timezone.utc),
        "clicks": 0,
    }


def record_click(code: str, referer: str | None = None, user_agent: str | None = None,
                 ip: str | None = None) -> dict | None:
    """Пишет событие клика. В проде — вставка в analytics_events/ClickHouse."""
    return {
        "code": code,
        "ts": datetime.now(timezone.utc),
        "referer": referer or "",
        "ua_hash": hashlib.sha256((user_agent or "").encode()).hexdigest()[:12],
        "ip_hash": hashlib.sha256((ip or "").encode()).hexdigest()[:12],  # PII не храним в открытом виде
    }


def stats(link: dict, clicks: list[dict], days: int = 7) -> dict:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    recent = [c for c in clicks if c["ts"] >= since]
    by_day: dict[str, int] = {}
    for c in recent:
        key = c["ts"].strftime("%Y-%m-%d")
        by_day[key] = by_day.get(key, 0) + 1
    return {
        "code": link["code"],
        "clicks": len(clicks),
        "clicks_7d": len(recent),
        "uniques_7d": len({c["ip_hash"] for c in recent}),
        "by_day": [{"date": d, "clicks": n} for d, n in sorted(by_day.items())],
        "short_url": link["short_url"],
        "target_url": link["target_url"],
    }
