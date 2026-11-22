"""Календарь инфоповодов РФ: законы, налоги, праздники, профессиональные дни.

Слой поверх календаря публикаций: AI заранее предлагает прогревы к датам,
которые аудитория и так держит в голове.
"""
from __future__ import annotations

from datetime import date

EVENTS: list[dict] = [
    {"date": "2026-11-20", "kind": "tax", "title": "Уплата налогов и взносов за октябрь (ЕНП)",
     "suggest": "Прогрев: «Что будет, если не заплатить до 28 ноября» — разбор санкций", "scope": "business"},
    {"date": "2026-11-26", "kind": "pro", "title": "День предпринимателя (неофиц. предпринимательская дата)",
     "suggest": "Пост-поздравление + подборка «5 документов, которые нужны каждому ИП»", "scope": "business"},
    {"date": "2026-11-28", "kind": "tax", "title": "Срок уплаты налогов за октябрь / аванс УСН",
     "suggest": "Напоминание с чек-листом и ссылкой на конструктор платёжек", "scope": "business"},
    {"date": "2026-12-01", "kind": "law", "title": "Вступают в силу изменения в ГК РФ (обзор новелл)",
     "suggest": "Лонгрид: «Что меняется в договорах с 1 декабря» — высокий спрос на разбор", "scope": "legal"},
    {"date": "2026-12-01", "kind": "tax", "title": "Уплата имущественных налогов физлиц за 2025 год",
     "suggest": "Пост: как проверить начисления и что делать при ошибке в квитанции", "scope": "citizens"},
    {"date": "2026-12-03", "kind": "pro", "title": "День юриста",
     "suggest": "Поздравление + «день открытых дверей»: бесплатная проверка одного пункта договора", "scope": "legal"},
    {"date": "2026-12-15", "kind": "tax", "title": "Отчётность по страховым взносам / персонифицированные сведения",
     "suggest": "Чек-лист «что сдать до 15 декабря»", "scope": "business"},
    {"date": "2026-12-25", "kind": "law", "title": "Публикация обзора судебной практики за IV квартал",
     "suggest": "Дайджест-разбор трёх дел, которые меняют практику договоров", "scope": "legal"},
    {"date": "2026-12-31", "kind": "memo", "title": "Завершение переходного периода по ограниченным ресурсам",
     "suggest": "Пост: «что проверить перед 2027 годом» + ссылка на аудит договоров", "scope": "legal"},
    {"date": "2027-01-09", "kind": "tax", "title": "Срок подачи уведомления по ЕНП за декабрь",
     "suggest": "Напоминание в первую рабочую неделю января", "scope": "business"},
]


def between(start: str | None = None, end: str | None = None) -> list[dict]:
    s = date.fromisoformat(start) if start else None
    e = date.fromisoformat(end) if end else None
    out = []
    for ev in EVENTS:
        d = date.fromisoformat(ev["date"])
        if s and d < s:
            continue
        if e and d > e:
            continue
        out.append(ev)
    return sorted(out, key=lambda x: x["date"])


def upcoming(limit: int = 5) -> list[dict]:
    return between()[:limit]
