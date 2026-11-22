"""Видео-отчёт агентства: цифры клиента → ролик за один вызов (этап G, ADR 0023).

Зачем отдельный модуль, а не «шаблон отчёта в интерфейсе»:

**1. Отчёт — это те же цифры, что в кабинете, а не новые.** `media_report.build()` берёт данные из
`store.METRICS` и публикаций, считает охваты/реакции/переходы и **кладёт рядом блок `sources`**:
каждая цифра в кадре указывает на коллекцию, из которой взята. Видео, которое «само посчитало
охваты», — это не отчёт, а реклама.

**2. Нет данных — нет отчёта.** Пустой период возвращает `no_data`, а не ролик с нулями: ноль
просмотров в кадре читается как «работа не дала результата», хотя на деле не было публикаций.

**3. Собирается тем же движком, что и ролик из поста.** Отчёт — не отдельный видеоредактор:
это `media_reel.build_reel_spec` с текстами, собранными из чисел. Значит формат, brand kit,
маркировка, safe zone, язык и смета работают одинаково, и отчёт не «съезжает» при правке рендерера.
"""
from __future__ import annotations

import datetime as _dt
from typing import Any

from . import media_reel, store, tenancy

DEFAULT_PERIOD_DAYS = 30
#: сколько топ-постов показывать в отчёте
TOP_POSTS = 3


def _parse(value: Any) -> _dt.datetime | None:
    if not value:
        return None
    try:
        moment = _dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=_dt.timezone.utc)


def _in_period(value: Any, days: int) -> bool:
    moment = _parse(value)
    if moment is None:
        return True                                  # без даты не отбрасываем: данные неполные
    return (_dt.datetime.now(_dt.timezone.utc) - moment).days <= days


def _fmt(number: int | float, locale: str | None) -> str:
    """Разделитель разрядов по языку: 12 480 в ru, 12,480 в en."""
    text = f"{int(number):,}".replace(",", " " if (locale or "ru") != "en" else ",")
    return text


def numbers(*, days: int = DEFAULT_PERIOD_DAYS, channel: str | None = None,
            workspace_id: str | None = None) -> dict[str, Any]:
    """Цифры отчёта: охваты, реакции, переходы, вовлечённость, топ-посты, расход."""
    scope = workspace_id or tenancy.scope_workspace()
    totals = {"impressions": 0, "reactions": 0, "clicks": 0, "cost_rub": 0.0}
    by_publication: dict[str, dict[str, Any]] = {}
    publications = 0

    for publication in store.PUBLICATIONS.values():
        if publication.get("status") != "published":
            continue
        if scope is not None and publication.get("workspace_id") != scope:
            continue
        if channel and publication.get("channel") != channel:
            continue
        if not _in_period(publication.get("published_at"), days):
            continue
        publications += 1
        by_publication[str(publication.get("id"))] = {
            "publication_id": publication.get("id"), "channel": publication.get("channel"),
            "published_at": publication.get("published_at"), "impressions": 0,
            "reactions": 0, "clicks": 0, "title": "",
        }

    for row in store.METRICS:
        pid = str(row.get("publication_id") or "")
        bucket = by_publication.get(pid)
        if bucket is None:
            continue
        for key in ("impressions", "reactions", "clicks"):
            value = int(row.get(key) or 0)
            bucket[key] += value
            totals[key] += value
        totals["cost_rub"] += float(row.get("cost_rub") or 0)

    for pid, bucket in by_publication.items():
        variant = store.get_variant(
            (store.get_publication(pid) or {}).get("variant_id") or "", workspace_id=scope)
        bucket["title"] = str((variant or {}).get("title") or "").strip()
        bucket["engagement"] = round((bucket["reactions"] + bucket["clicks"])
                                     / max(bucket["impressions"], 1), 4)

    ranked = sorted(by_publication.values(),
                    key=lambda item: (item["impressions"], item["engagement"]), reverse=True)
    top = [row for row in ranked if row["impressions"] > 0][:TOP_POSTS]
    return {"days": days, "channel": channel, "publications": publications,
            "impressions": totals["impressions"], "reactions": totals["reactions"],
            "clicks": totals["clicks"], "cost_rub": round(totals["cost_rub"], 2),
            "engagement": round((totals["reactions"] + totals["clicks"])
                                / max(totals["impressions"], 1), 4),
            "top_posts": top}


def _post_from_numbers(data: dict[str, Any], *, locale: str | None) -> dict[str, Any]:
    """Цифры → пост для движка: хук с главным числом, пункты, вывод, призыв."""
    period = f"{data['days']} дней"
    items = [
        f"Публикаций: {data['publications']}",
        f"Реакции: {_fmt(data['reactions'], locale)} · переходы: {_fmt(data['clicks'], locale)}",
        f"Вовлечённость: {data['engagement']:.1%}",
    ]
    if data["cost_rub"]:
        items.append(f"Расход: {data['cost_rub']:.0f} ₽")
    top = data["top_posts"][0] if data["top_posts"] else None
    note = (f"Лучший пост: {top['title']} — {_fmt(top['impressions'], locale)} показов"
            if top else "Лучший пост: данных пока недостаточно")
    if top:
        # лучший пост идёт отдельным пунктом: `note` уходит в озвучку и титры, а сцена
        # с пунктами титры не рисует — без этого числа топ-поста не было бы в кадре вовсе
        # число впереди: при обрезке чипа заголовок теряет хвост, а цифра — нет
        items.append(_clip(f"Топ: {_fmt(top['impressions'], locale)} показов — {top['title']}",
                           media_reel.MAX_CHIP_CHARS))
    return {
        "title": f"{_fmt(data['impressions'], locale)} просмотров за {period}",
        "eyebrow": "Отчёт · " + (data["channel"] or "все каналы"),
        "items": items,
        "note": note,
        "footer": "posiz.ru",
        "cta": "Полный отчёт в кабинете",
    }


def _clip(text: str, limit: int) -> str:
    """Обрезка по границе слова: чип не должен заканчиваться на «ката…»."""
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if " " in cut:
        cut = cut[:cut.rfind(" ")]
    return cut.rstrip(" ,;:—-")


def build(*, days: int = DEFAULT_PERIOD_DAYS, channel: str | None = None,
          locale: str | None = None, fmt: str = media_reel.DEFAULT_FORMAT, fps: int = 30,
          brand: dict | None = None, render: bool = False,
          workspace_id: str | None = None) -> dict[str, Any]:
    """Собрать видео-отчёт. Без данных — отказ, а не ролик из нулей."""
    data = numbers(days=days, channel=channel, workspace_id=workspace_id)
    if not data["publications"]:
        return {"ok": False, "code": "no_data",
                "reason": f"за {days} дней нет опубликованных постов — отчёт не из чего собрать",
                "numbers": data}
    if not data["impressions"]:
        return {"ok": False, "code": "no_metrics",
                "reason": ("публикации есть, но охватов по ним не приходило: отчёт из нулей "
                           "читался бы как провал кампании"),
                "numbers": data}

    post = _post_from_numbers(data, locale=locale)
    spec = media_reel.build_reel_spec(post, brand=brand, fps=fps, fmt=fmt, locale=locale)
    spec["origin"] = {
        "kind": "report", "days": days, "channel": channel,
        "publications": data["publications"], "impressions": data["impressions"],
        "reactions": data["reactions"], "clicks": data["clicks"],
        "engagement": data["engagement"],
    }
    checks = media_reel.check(spec)
    result: dict[str, Any] = {
        "ok": True, "numbers": data, "post": post, "spec": spec, "checks": checks,
        "spec_hash": media_reel.spec_hash(spec),
        # каждая цифра в кадре указывает на источник — отчёт проверяем, а не «верим»
        "sources": {
            "impressions": "store.METRICS.impressions (сумма снимков за период)",
            "reactions": "store.METRICS.reactions",
            "clicks": "store.METRICS.clicks",
            "publications": "store.PUBLICATIONS (status=published за период)",
            "engagement": "(реакции + переходы) / показы",
            "top_posts": "те же публикации, отсортированные по показам",
        },
    }
    if render:
        result["submission"] = media_reel.submit(spec, render=True, workspace_id=workspace_id)
    return result


def summary(*, workspace_id: str | None = None) -> dict[str, Any]:
    data = numbers(workspace_id=workspace_id)
    return {"days": data["days"], "publications": data["publications"],
            "impressions": data["impressions"], "engagement": data["engagement"],
            "ready": bool(data["publications"] and data["impressions"])}
