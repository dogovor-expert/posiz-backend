"""Recycle: топ-пост из аналитики → ролик в один вызов (этап G, ADR 0023).

Зачем отдельный модуль, а не «кнопка выбрать пост в интерфейсе»:

**1. «Топ-пост» — это число, а не вкус.** Кандидат берётся из накопленной аналитики
(`store.METRICS` + публикации), а не из списка «последние 20 постов». Метрика — вовлечённость
`(реакции + переходы) / показы` на пороге показов: 1 реакция из 1 показа даёт 100 %, и без порога
такой пост «победил» бы любой нормальный.

**2. Порог и отказ видны в ответе.** Пост без данных не становится кандидатом — он уходит в
`skipped` с причиной (`no_metrics`, `below_threshold`). Ролик из поста, у которого нет охватов, —
это не «recycle», а случайный выбор; платформа должна сказать это прямо, а не показать красивую
кнопку.

**3. Ролик объясняет, откуда он.** В спеку попадает блок `origin`: публикация, канал, показы,
вовлечённость и человеческая причина выбора. Поэтому через месяц можно ответить на вопрос «почему
мы сделали ролик именно из этого поста» не по памяти, а по артефакту — и он же входит в `spec_hash`,
то есть два ролика из разных постов не склеятся в один.

Текст берётся из уже одобренного варианта публикации: смысловое ядро прошло согласование, ролик
не противоречит тому, что вышло в каналах.
"""
from __future__ import annotations

import re
from typing import Any

from . import media_reel, store, tenancy

#: порог показов: ниже него вовлечённость — шум, а не сигнал
MIN_IMPRESSIONS = 100
#: по какой давности ищем топ-посты
DEFAULT_DAYS = 180
#: сколько кандидатов показывать
DEFAULT_LIMIT = 10

_BULLET_RE = re.compile(r"^\s*(?:[•·\-–—*]|\d+[.)])\s+")


def _metrics_by_publication() -> dict[str, dict[str, int]]:
    """Сумма снимков по публикации: аналитика приходит срезами, а не одним числом."""
    totals: dict[str, dict[str, int]] = {}
    for row in store.METRICS:
        pid = str(row.get("publication_id") or "")
        if not pid:
            continue
        bucket = totals.setdefault(pid, {"impressions": 0, "reactions": 0, "clicks": 0})
        for key in bucket:
            bucket[key] += int(row.get(key) or 0)
    return totals


def _post_text(publication: dict, workspace_id: str | None) -> dict[str, str]:
    """Заголовок и тело одобренного варианта публикации."""
    variant = store.get_variant(publication.get("variant_id") or "", workspace_id=workspace_id)
    title = str((variant or {}).get("title") or "").strip()
    body = str((variant or {}).get("body") or "").strip()
    return {"title": title, "body": body}


def _why(row: dict) -> str:
    """Человеческая причина выбора — та же, что попадёт в `origin.why`."""
    return (f"{row['impressions']} показов и вовлечённость {row['engagement']:.1%} "
            f"в канале {row['channel']} — лучший результат среди публикаций с достаточными данными")


def candidates(*, days: int = DEFAULT_DAYS, channel: str | None = None,
               limit: int = DEFAULT_LIMIT, min_impressions: int | None = None,
               workspace_id: str | None = None) -> dict[str, Any]:
    """Кандидаты на recycle, отсортированные по вовлечённости.

    Возвращаются **все** просмотренные публикации с пометкой `eligible` и причиной — включая те,
    что не прошли порог: «почему в списке три поста, а не двадцать» — такой же вопрос, как «почему
    вариантов хука три».
    """
    threshold = int(min_impressions if min_impressions is not None else MIN_IMPRESSIONS)
    scope = workspace_id or tenancy.scope_workspace()
    totals = _metrics_by_publication()

    rows: list[dict[str, Any]] = []
    for publication in store.PUBLICATIONS.values():
        if publication.get("status") != "published":
            continue
        if scope is not None and publication.get("workspace_id") != scope:
            continue
        if channel and publication.get("channel") != channel:
            continue
        pid = str(publication.get("id"))
        numbers = totals.get(pid)
        text = _post_text(publication, scope)
        row: dict[str, Any] = {
            "publication_id": pid, "variant_id": publication.get("variant_id"),
            "channel": publication.get("channel"), "published_at": publication.get("published_at"),
            "title": text["title"] or _first_line(text["body"]),
            "impressions": (numbers or {}).get("impressions", 0),
            "reactions": (numbers or {}).get("reactions", 0),
            "clicks": (numbers or {}).get("clicks", 0),
            "engagement": 0.0, "eligible": False, "reason": None,
        }
        if numbers is None:
            row["reason"] = "no_metrics: аналитика по публикации ещё не приходила"
        elif row["impressions"] < threshold:
            row["reason"] = (f"below_threshold: {row['impressions']} показов при пороге "
                             f"{threshold} — вовлечённость была бы шумом")
        elif not row["title"]:
            row["reason"] = "no_text: у публикации нет заголовка — ролик собрать не из чего"
        else:
            row["engagement"] = round((row["reactions"] + row["clicks"])
                                      / max(row["impressions"], 1), 4)
            row["eligible"] = True
            row["why"] = _why(row)
        rows.append(row)

    if days:
        rows = [row for row in rows if _within_days(row.get("published_at"), days)]
    eligible = [row for row in rows if row["eligible"]]
    eligible.sort(key=lambda item: (item["engagement"], item["impressions"],
                                    str(item.get("published_at") or "")), reverse=True)
    skipped = [row for row in rows if not row["eligible"]]
    skipped.sort(key=lambda item: (item["impressions"], str(item.get("published_at") or "")),
                 reverse=True)
    return {"ok": True, "threshold": threshold, "days": days, "channel": channel,
            "rows": eligible[:limit], "skipped": skipped[:limit],
            "counts": {"published": len(rows), "eligible": len(eligible),
                       "skipped": len(skipped)}}


def _within_days(published_at: Any, days: int) -> bool:
    import datetime as _dt

    if not published_at:
        return True
    text = str(published_at).replace("Z", "+00:00")
    try:
        moment = _dt.datetime.fromisoformat(text)
    except ValueError:
        return True
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_dt.timezone.utc)
    return (_dt.datetime.now(_dt.timezone.utc) - moment).days <= days


def _first_line(body: str) -> str:
    for line in body.splitlines():
        if line.strip():
            return line.strip()[:120]
    return ""


def _sentences(body: str) -> list[str]:
    """Пункты ролика из тела поста: сначала маркированные строки, затем предложения."""
    bullets = [line.strip() for line in body.splitlines() if _BULLET_RE.match(line)]
    if bullets:
        return [re.sub(_BULLET_RE, "", item) for item in bullets if len(item) > 2]
    parts, current = [], ""
    for char in body:
        current += char
        if char in ".!?…":
            parts.append(current.strip())
            current = ""
    if current.strip():
        parts.append(current.strip())
    return [part for part in parts if len(part) > 2]


def post_from_publication(row: dict[str, Any], *, limit_items: int = 4) -> dict[str, Any]:
    """Публикация → пост для движка ролика.

    Заголовок остаётся заголовком (его уже одобрили), тело режется на пункты по маркерам и
    предложениям. Пункт длиннее лимита чипа **обрезается по границе слова**, а не по символу:
    «Скидка 20% на весь ката…» в кадре — это брак рендера, а не экономия места.
    """
    body = str(row.get("body") or "")
    items: list[str] = []
    for sentence in _sentences(body):
        chip = _clip(sentence, media_reel.MAX_CHIP_CHARS)
        if chip and chip not in items:
            items.append(chip)
        if len(items) >= limit_items:
            break
    return {
        "title": str(row.get("title") or "")[:120],
        "eyebrow": f"{row.get('channel') or 'канал'} · топ-пост",
        "items": items,
        "footer": str(row.get("footer") or "posiz.ru"),
        "cta": str(row.get("cta") or "Читать целиком"),
    }


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if " " in cut:
        cut = cut[:cut.rfind(" ")]
    return cut.rstrip(" ,;:—-")


def recycle(*, publication_id: str | None = None, channel: str | None = None,
            days: int = DEFAULT_DAYS, fmt: str = media_reel.DEFAULT_FORMAT, fps: int = 30,
            locale: str | None = None, brand: dict | None = None,
            min_impressions: int | None = None, render: bool = False,
            workspace_id: str | None = None) -> dict[str, Any]:
    """Один вызов: найти топ-пост (или взять указанный) и собрать из него ролик.

    Отказ вместо угадывания: нет данных, нет порога показов, нет текста, неизвестный
    `publication_id` — всё это коды, а не пустой ролик.
    """
    if publication_id:
        publication = store.get_publication(publication_id)
        if not publication or publication.get("status") != "published":
            return {"ok": False, "code": "publication_not_found",
                    "reason": f"опубликованный пост {publication_id} не найден"}
        numbers = _metrics_by_publication().get(publication_id) or {}
        text = _post_text(publication, workspace_id or tenancy.scope_workspace())
        title = text["title"] or _first_line(text["body"])
        impressions = int(numbers.get("impressions") or 0)
        threshold = int(min_impressions if min_impressions is not None else MIN_IMPRESSIONS)
        if impressions < threshold:
            return {"ok": False, "code": "below_threshold",
                    "reason": (f"у публикации {impressions} показов при пороге {threshold}: "
                               "ролик из поста без охватов — это не recycle, а случайный выбор"),
                    "impressions": impressions, "threshold": threshold}
        if not title:
            return {"ok": False, "code": "no_text", "reason": "у публикации нет текста"}
        candidate = {
            "publication_id": publication_id, "variant_id": publication.get("variant_id"),
            "channel": publication.get("channel"), "published_at": publication.get("published_at"),
            "title": title, "impressions": impressions,
            "reactions": int(numbers.get("reactions") or 0), "clicks": int(numbers.get("clicks") or 0),
            "engagement": round((int(numbers.get("reactions") or 0)
                                 + int(numbers.get("clicks") or 0)) / max(impressions, 1), 4),
            "eligible": True,
        }
        candidate["why"] = _why(candidate)
        body = text["body"]
    else:
        ranked = candidates(days=days, channel=channel, min_impressions=min_impressions,
                            workspace_id=workspace_id)
        if not ranked["rows"]:
            return {"ok": False, "code": "no_candidates",
                    "reason": (f"нет публикаций, прошедших порог {ranked['threshold']} показов: "
                               "recycle работает по данным, а не по интуиции"),
                    "counts": ranked["counts"], "skipped": ranked["skipped"]}
        candidate = ranked["rows"][0]
        publication = store.get_publication(candidate["publication_id"]) or {}
        body = _post_text(publication, workspace_id or tenancy.scope_workspace())["body"]

    post = post_from_publication({**candidate, "body": body})
    spec = media_reel.build_reel_spec(post, brand=brand, fps=fps, fmt=fmt, locale=locale)
    spec["origin"] = {
        "kind": "recycle", "publication_id": candidate.get("publication_id"),
        "variant_id": candidate.get("variant_id"), "channel": candidate.get("channel"),
        "impressions": candidate.get("impressions"), "reactions": candidate.get("reactions"),
        "clicks": candidate.get("clicks"), "engagement": candidate.get("engagement"),
        "why": candidate.get("why"),
    }
    checks = media_reel.check(spec)
    result: dict[str, Any] = {"ok": True, "candidate": candidate, "post": post,
                              "spec": spec, "checks": checks,
                              "spec_hash": media_reel.spec_hash(spec)}
    if render:
        result["submission"] = media_reel.submit(spec, render=True, workspace_id=workspace_id)
    return result


def summary(*, workspace_id: str | None = None) -> dict[str, Any]:
    """Сколько публикаций пригодно для recycle — для витрины и health."""
    ranked = candidates(workspace_id=workspace_id)
    return {"threshold": ranked["threshold"], "days": ranked["days"],
            "published": ranked["counts"]["published"],
            "eligible": ranked["counts"]["eligible"], "skipped": ranked["counts"]["skipped"]}
