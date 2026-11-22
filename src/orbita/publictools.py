"""Бесплатные SEO-утилиты `posiz.ru/tools/*`: бэкенд-часть.

Четыре инструмента из стратегии продвижения. Они бесплатны и без регистрации — это
магнит органического трафика, поэтому обслуживаются публичным API с жёстким rate-limit,
без доступа к данным клиентов и без записи в БД.

* ``erid_check`` — признаки рекламы по 38-ФЗ и готовый блок маркировки;
* ``risk_calculator`` — сколько охвата рискует при блокировке/замедлении канала;
* ``utm_builder`` — UTM-ссылка и смета доставки (включая pay-per-use X);
* ``thread_splitter`` — нарезка лонгрида в тред под лимиты площадок.

Ничего не «притворяется»: утилиты отдают те же правила, что и продукт, иначе пользователь
получит противоречивые советы на сайте и в приложении.
"""
from __future__ import annotations

import re
from typing import Any

from . import links, tools

# маркеры рекламы: цена, скидка, промо, призыв к покупке, гарантии результата
AD_PATTERNS: list[tuple[str, str]] = [
    (r"\d[\s\u00a0]?\d*\s?(₽|руб|рублей|р\.)", "указана цена"),
    (r"\b(скидк|акци|распродаж|промокод|спецпредложен)", "промо-механика"),
    (r"\b(купи|купить|закажи|заказать|оформи|подпишись|запишись|жми|переходи по ссылке)", "призыв к действию"),
    (r"\b(бесплатн|0\s?₽|даром)", "обещание бесплатного"),
    (r"\b(гарантиру|100\s?%|лучш|№\s?1|самый выгодн)", "гарантия/превосходная степень"),
    (r"https?://", "внешняя ссылка"),
]

# риск охвата по каналам: (статус, доля доступной аудитории, комментарий)
CHANNEL_RISK: dict[str, tuple[str, float, str]] = {
    "max":        ("ok", 1.00, "официальный Bot API, предустановка на устройствах"),
    "vk":         ("ok", 1.00, "полный API, ограничений на публикацию нет"),
    "ok":         ("ok", 0.95, "постинг есть, строгая модерация"),
    "dzen":       ("ok", 0.95, "постинга нет — RSS-автоимпорт"),
    "telegram":   ("degraded", 0.60, "замедление с 02.2026, доставка нестабильна"),
    "instagram":  ("blocked", 0.15, "заблокирован в РФ, реклама запрещена"),
    "whatsapp":   ("blocked", 0.05, "заблокирован с 02.2026, только обходные каналы"),
    "youtube":    ("degraded", 0.50, "замедление, реклама ограничена"),
    "x":          ("paid", 0.90, "доставка платная: $0,015/пост, $0,20 со ссылкой в тексте"),
    "tenchat":    ("manual", 0.85, "официального API нет — headless-публикация"),
    "rutube":     ("manual", 0.85, "официального API нет — headless-публикация"),
    "threads":    ("paid", 0.70, "публикация по API, ограничения платформы"),
}

# смета доставки, ₽ за публикацию (курс ≈ 90 ₽/$ — фиксируем, чтобы цифры не «плавали»)
DELIVERY_RUB: dict[str, dict[str, float]] = {
    "max": {"post": 0.0, "post_with_link": 0.0},
    "vk": {"post": 0.0, "post_with_link": 0.0},
    "telegram": {"post": 0.0, "post_with_link": 0.0},
    "ok": {"post": 0.0, "post_with_link": 0.0},
    "dzen": {"post": 0.0, "post_with_link": 0.0},
    "x": {"post": 1.4, "post_with_link": 18.0},
    "threads": {"post": 0.0, "post_with_link": 0.0},
    "instagram": {"post": 0.0, "post_with_link": 0.0},
    "youtube": {"post": 0.0, "post_with_link": 0.0},
    "tenchat": {"post": 0.0, "post_with_link": 0.0},
    "rutube": {"post": 0.0, "post_with_link": 0.0},
}

MARKING_TEMPLATE = ("Реклама. {advertiser}{inn_part}\nerid: {erid}")


def erid_check(text: str, advertiser: str = "", inn: str | None = None,
               erid: str | None = None) -> dict[str, Any]:
    """Нужна ли маркировка и что именно указать."""
    lowered = (text or "").lower()
    reasons = [label for pattern, label in AD_PATTERNS if re.search(pattern, lowered)]
    has_erid_in_text = bool(re.search(r"erid:\s*\w+", lowered)) or bool(erid)
    needs_marking = bool(reasons) and not has_erid_in_text
    inn_part = f", ИНН {inn}" if inn else ""
    block = MARKING_TEMPLATE.format(advertiser=advertiser or "наименование рекламодателя",
                                    inn_part=inn_part, erid=erid or "получить в ОРД")
    checklist = [
        "Пометка «Реклама» — в начале или конце креатива, видимая без раскрытия",
        "Наименование рекламодателя и ИНН (или ссылка на сайт с этими данными)",
        "erid, полученный через ОРД, рядом с пометкой",
        "Отчёт в ЕРИР — до 30 числа следующего месяца",
        "Для сторис и видео erid должен быть видим в кадре, а не только в описании",
    ]
    return {
        "needs_marking": needs_marking,
        "reasons": reasons,
        "risk": "high" if needs_marking and len(reasons) >= 3 else
                "medium" if needs_marking else "low",
        "fine_note": "штраф за отсутствие маркировки — до 500 000 ₽ на юрлицо за публикацию",
        "marking_block": block,
        "checklist": checklist,
        "formula": "38-ФЗ ст. 18.1 + правила ОРД",
    }


def risk_calculator(channels: list[str], audience: int = 100_000) -> dict[str, Any]:
    """Оценка потери охвата и рекомендация по фейловеру."""
    rows, reach, reach_safe = [], 0, 0.0
    for channel in channels:
        status, share, note = CHANNEL_RISK.get(channel, ("unknown", 0.5, "канал не описан"))
        part = int(audience * share)
        reach += part
        reach_safe += audience * share
        rows.append({"channel": channel, "status": status, "share": share,
                     "reach": part, "note": note})
    at_risk = sum(r["reach"] for r in rows if r["status"] in ("blocked", "degraded"))
    stable = sorted([r["channel"] for r in rows if r["status"] == "ok"],
                    key=lambda c: -CHANNEL_RISK[c][1])
    return {
        "audience": audience,
        "channels": rows,
        "reach_total": reach,
        "reach_at_risk": at_risk,
        "at_risk_share": round(at_risk / reach, 3) if reach else 0.0,
        "recommendation": (f"Продублировать контент в {', '.join(stable[:2])}"
                           if at_risk else "Резервный канал не требуется"),
        "failover": stable[:3],
    }


def utm_builder(target_url: str, channel: str, campaign: str = "post",
                content: str | None = None, term: str | None = None,
                with_link_in_text: bool = True) -> dict[str, Any]:
    """UTM-ссылка + смета доставки по каналам (важно для pay-per-use X)."""
    url = links.with_utm(target_url, channel, campaign, content=content)
    if term:
        url = f"{url}&utm_term={term}"
    estimate = {}
    for name, costs in DELIVERY_RUB.items():
        estimate[name] = costs["post_with_link" if with_link_in_text else "post"]
    cheapest = min(estimate, key=lambda k: estimate[k])
    return {
        "url": url,
        "short_hint": f"https://posiz.ru/l/<code> — короткая ссылка с той же атрибуцией",
        "delivery_rub": estimate,
        "chosen_channel_cost": estimate.get(channel),
        "cheapest_channel": cheapest,
        "note": ("в X ссылка в тексте стоит в ~13 раз дороже, чем в первом ответе — "
                 "вынесите её в ответ через Posiz"),
    }


def thread_splitter(text: str, channel: str = "x", limit: int | None = None) -> dict[str, Any]:
    posts = tools.split_thread(text, channel=channel, limit=limit)
    return {
        "channel": channel,
        "limit": tools.LIMITS.get(channel, 4000),
        "count": len(posts),
        "posts": posts,
        "lengths": [len(p) for p in posts],
    }


def index() -> dict[str, Any]:
    from . import seo

    return {"tools": seo.tool_meta(),
            "policy": "бесплатно, без регистрации, без хранения текста; лимит 30 запросов/мин с IP"}
