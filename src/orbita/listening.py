"""Мониторинг упоминаний: бренд, конкуренты, темы.

Сканирование в каркасе детерминированное (демо-выборка), в проде — воркеры по каналам
(Telegram-чаты через userbot/MTProto, VK wall.search, отзывы в картах, веб-поиск).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .docs import Collection

KEYWORDS: dict[str, dict] = {
    "kw_1": {"id": "kw_1", "phrase": "конструктор договоров", "channels": ["telegram", "vk", "yandex_business"],
             "active": True, "hits": 34},
    "kw_2": {"id": "kw_2", "phrase": "проверить договор", "channels": ["telegram", "vk"], "active": True, "hits": 27},
    "kw_3": {"id": "kw_3", "phrase": "dogovor.expert", "channels": ["telegram", "vk", "vcru"], "active": True, "hits": 12},
    "kw_4": {"id": "kw_4", "phrase": "вернуть залог", "channels": ["telegram", "twogis"], "active": True, "hits": 41},
}

_SAMPLES = [
    ("telegram", "Чат «Малый бизнес и право»", "Ищу конструктор договоров аренды, чтобы не собирать вручную. Кто чем пользуется?",
     "high", "вопрос по теме — можно ответить нативно"),
    ("vk", "Сообщество «Юристы СПб»", "Проверить договор поставки перед подписанием — куда идти?",
     "high", "готов предложить чек-лист"),
    ("telegram", "Чат «Аренда коммерческих помещений»", "Вернуть залог не получается, арендодатель тянет. Есть шаблон претензии?",
     "high", "горячий лид: нужна претензия"),
    ("vcru", "Комментарий к статье «Договоры 2026»", "dogovor.expert похоже делает то же самое, но дешевле",
     "medium", "упоминание бренда, можно поблагодарить"),
    ("yandex_business", "Отзыв в карточке", "Проверить договор помогли, но ждал ответа 2 часа",
     "medium", "негатив по SLA — ответить и предложить бонус"),
    ("twogis", "Отзыв в карточке", "Вернуть залог — подсказали с первого раза",
     "low", "позитив: попросить отзыв на сайте"),
]

MENTIONS: list[dict] = []

KEYWORDS_DOCS = Collection("listening_keywords", seed=list(KEYWORDS.values()), id_field="id")
MENTIONS_DOCS = Collection("listening_mentions", id_field="id")


def add_keyword(phrase: str, channels: list[str] | None = None) -> dict:
    kid = f"kw_{len(KEYWORDS) + 1}"
    KEYWORDS[kid] = {"id": kid, "phrase": phrase.lower(), "channels": channels or ["telegram"],
                     "active": True, "hits": 0, "workspace_id": "ws_de"}
    return KEYWORDS_DOCS.put(KEYWORDS[kid])


def list_keywords() -> list[dict]:
    return KEYWORDS_DOCS.all()


def scan() -> list[dict]:
    """Демо-скан: подбирает релевантные фразы к ключам, считает приоритет."""
    now = datetime.now(timezone.utc)
    keywords = KEYWORDS_DOCS.all()
    phrases = [k["phrase"] for k in keywords]
    created = []
    for i, (channel, source, text, sentiment, hint) in enumerate(_SAMPLES):
        matched = next((p for p in phrases if p in text.lower()), phrases[0])
        mention = {
            "id": f"mn_{len(MENTIONS) + 1}", "channel": channel, "source": source, "text": text,
            "matched_keyword": matched, "sentiment": sentiment, "hint": hint,
            "priority": {"high": 3, "medium": 2, "low": 1}[sentiment],
            "found_at": (now - timedelta(hours=i * 3)).isoformat(timespec="seconds"),
            "status": "new",
        }
        MENTIONS.append(mention)
        MENTIONS_DOCS.put(mention)
        created.append(mention)
        for k in keywords:
            if k["phrase"] == matched:
                k["hits"] = k.get("hits", 0) + 1
                KEYWORDS_DOCS.put(k)
    return created


def list_mentions(priority: str | None = None, limit: int = 50) -> list[dict]:
    items = sorted(MENTIONS_DOCS.all() or MENTIONS,
                   key=lambda m: (-m["priority"], m["found_at"]), reverse=False)
    if priority:
        items = [m for m in items if m["sentiment"] == priority]
    return items[:limit]


def reply_draft(mention_id: str) -> dict:
    """AI-агент предлагает нативный ответ; отправляет человек или правило."""
    mention = MENTIONS_DOCS.get(mention_id) or next(
        (m for m in MENTIONS if m["id"] == mention_id), None)
    if not mention:
        return {"error": "упоминание не найдено"}
    templates = {
        "high": "Похоже, это про вас. Мы как раз собрали чек-лист по этой теме — могу прислать, "
                "если полезно (без спама в чат).",
        "medium": "Спасибо, что заметили! Если пригодится — вот разбор по теме.",
        "low": "Спасибо за отзыв! Передали команде, ответили в личку.",
    }
    return {"mention_id": mention_id, "draft": templates[mention["sentiment"]],
            "action": "ответить от имени сообщества", "approve_required": True}
