"""Перелинковка статей по смыслу и обновление «вечнозелёных» материалов (этап I5, ADR 0024).

Две задачи, у которых общий враг — «сделать вид»:

**1. Перелинковка по смыслу.** Ссылки «читайте также» обычно ставятся по ручной подборке или
по совпадению тегов. Теги врут: две статьи про Telegram-каналы могут не иметь ничего общего, а
статья про охваты и статья про цену действия — связаны по смыслу, но тегов у них нет. Поэтому
близость считается по векторам, и считается **честно**:

* эмбеддинг детерминированный и локальный: мешок основ слов (русские окончания срезаются) плюс
  биграммы соседних основ, со взвешиванием `1 + log(tf)` и L2-нормировкой;
* **не** `llm._hash_embed` — там вектор строится из хэша всего текста, и близость получается
  случайной: у двух статей на одну тему косинус такой же, как у статьи и прайса. Это проверено
  тестом, а не описано словами;
* порог близости и лимит ссылок — из окружения (`ARTICLE_LINK_MIN_SCORE`, `ARTICLE_MAX_LINKS`),
  а не «на глаз»: ниже порога ссылка не ставится, и это названо (`no_candidates`).

**2. Обновление вечнозелёных статей.** «Вечнозелёная» — та, что живёт годами (как считать цену
действия, как читать отчёт). Такие статьи стареют не текстом, а цифрами. Поэтому обновление —
не перегенерация: меняются только данные, у которых есть `source` в материалах клиента. Нет
материала — число остаётся прежним, и это записано в отчёте (`skipped`), а не заменено догадкой.
Нет изменений — `no_changes` и версия не растёт: галка «обновлено» без правки — это ложь.
"""
from __future__ import annotations

import hashlib
import math
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .docs import Collection

#: размерность локального эмбеддинга: 512 хэш-корзин достаточно для сотен статей и дешевле
#: внешней модели; при переходе на pgvector размерность задаётся здесь же
EMBED_DIM = 512
#: порог близости по умолчанию: ниже — тема общая «про маркетинг», а не про то же самое
DEFAULT_MIN_SCORE = 0.14
#: сколько ссылок на статью максимум: больше пяти «читайте также» — уже не навигация
DEFAULT_MAX_LINKS = 4
#: через сколько дней вечнозелёная статья считается устаревшей
DEFAULT_REVIEW_DAYS = 180

_index = Collection("article_index")

#: окончания, которые срезаются перед сравнением основ. Список короткий намеренно: задача — не
#: морфология, а устойчивость к падежам («охвату» = «охваты» = «охватом»)
_SUFFIXES = (
    "ического", "ической", "ические", "ический", "ования", "ование", "ованиях",
    "ениями", "ениям", "ения", "ение", "ания", "ание", "ости", "ость", "ыми", "ими",
    "ами", "ями", "ого", "его", "ому", "ему", "ах", "ях", "ам", "ям", "ов", "ев",
    "ой", "ей", "ою", "ею", "ий", "ый", "ая", "ое", "ые", "ие", "ую", "юю", "ы", "и",
    "а", "е", "о", "у", "ю", "я",
)
_WORD = re.compile(r"[a-zа-яё0-9]+")
#: служебные слова: «как», «что» и «для» есть в любой теме, и близость по ним — обман
_STOP = frozenset("""а без более бы был была были было быть в вам вас весь во вот все всего всю вы
где да даже для до его ее если есть еще же за здесь и из или им их к как ко когда кто ли либо
мне может мы на над надо наш не него нее нет ни них но ну о об однако он она они оно от очень
по под при про с со так также такой там те тем то того тоже той только том ты у уже хотя чего
чей чем что чтобы чье чья эта эти это я the a an and or of to in for on with by is are be as
at from it this that these those we you they""".split())


def _stems(text: str) -> list[str]:
    """Основы слов: нижний регистр, срезанное окончание, цифры как есть."""
    stems: list[str] = []
    for word in _WORD.findall(str(text or "").lower()):
        if word in _STOP:
            continue
        if word.isdigit():
            stems.append(word)
            continue
        if len(word) <= 4:
            stems.append(word)
            continue
        for suffix in _SUFFIXES:
            if word.endswith(suffix) and len(word) - len(suffix) >= 4:
                stems.append(word[: -len(suffix)])
                break
        else:
            stems.append(word)
    return stems


def _bucket(feature: str) -> tuple[int, float]:
    digest = hashlib.sha256(feature.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % EMBED_DIM, 1.0 if digest[4] & 1 else -1.0


def embed(text: str) -> list[float]:
    """Вектор текста: мешок основ + биграммы, `1 + log(tf)`, L2-норма.

    Свойство, которое важно: близкие по смыслу тексты дают большой косинус, посторонние — около
    нуля. Хэш-эмбеддинг из `llm` этим свойством не обладает (см. `random_embed_gap`).
    """
    stems = _stems(text)
    if not stems:
        return [0.0] * EMBED_DIM
    counts: dict[str, float] = {}
    for stem in stems:
        counts[stem] = counts.get(stem, 0.0) + 1.0
    for left, right in zip(stems, stems[1:]):
        key = f"{left}_{right}"
        counts[key] = counts.get(key, 0.0) + 1.0
    vector = [0.0] * EMBED_DIM
    for feature, count in counts.items():
        weight = 1.0 + math.log(count)
        if "_" in feature:
            weight *= 0.5                     # биграммы уточняют, но не заменяют слова
        bucket, sign = _bucket(feature)
        vector[bucket] += sign * weight
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [round(value / norm, 6) for value in vector]


def cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    return round(sum(a * b for a, b in zip(left, right)), 6)


def random_embed_gap() -> dict:
    """Почему не `llm._hash_embed`: на нём близость неотличима от случайной.

    Возвращает косинусы «одна тема» и «разные темы» для двух способов. Внешняя модель или
    наш мешок основ дают разрыв; хэш-эмбеддинг — нет, и ссылки по нему были бы шумом.
    """
    from . import llm

    related = ("Как читать отчёт по охватам: переходы и цена действия",
               "Отчёт по охватам: охваты, переходы и цена действия в цифрах")
    unrelated = ("Как читать отчёт по охватам: переходы и цена действия",
                 "Прайс на ведение канала: пакеты и сроки")
    hash_related = cosine(llm._client._hash_embed(related[0]), llm._client._hash_embed(related[1]))
    hash_unrelated = cosine(llm._client._hash_embed(unrelated[0]),
                            llm._client._hash_embed(unrelated[1]))
    return {"ours": {"related": cosine(embed(related[0]), embed(related[1])),
                     "unrelated": cosine(embed(unrelated[0]), embed(unrelated[1]))},
            "hash": {"related": hash_related, "unrelated": hash_unrelated},
            "dim": EMBED_DIM}


# ------------------------------------------------------------------ индекс статей
def _entry_text(spec: dict) -> str:
    """Текст для вектора: заголовок, ключевые слова, подзаголовки, вводка.

    Тело статьи целиком не берётся намеренно: у двух статей одной темы совпадает именно
    «о чём», а не формулировки абзацев.
    """
    seo = spec.get("seo") or {}
    parts = [str(seo.get("title") or ""), " ".join(map(str, seo.get("keywords") or []))]
    blocks = spec.get("blocks") or []
    # подзаголовки, а не вводка: вводка часто шаблонная («разбираем по шагам»), и близость по
    # ней сделала бы похожими любые две статьи
    parts.extend(str((block.get("props") or {}).get("text") or "") for block in blocks
                 if block.get("type") == "heading")
    return " ".join(part for part in parts if part).strip()


def _spec_hash(spec: dict) -> str:
    payload = str(sorted(_stems(_entry_text(spec))))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def index_entry(doc: dict) -> dict:
    """Запись индекса по документу статьи: вектор считается один раз на текст."""
    spec = doc.get("spec") or doc
    key = _spec_hash(spec)
    text = _entry_text(spec)
    existing = _index.get(f"idx_{key}")
    if existing and existing.get("text") == text:
        entry = {**existing, "id": doc.get("id") or existing.get("id"),
                 "slug": (doc.get("slug") or existing.get("slug"))}
        _index.put({**entry, "id": f"idx_{key}"})
        return entry
    entry = {"id": f"idx_{key}", "key": key, "article_id": doc.get("id"),
             "slug": doc.get("slug") or spec.get("slug"),
             "title": (spec.get("seo") or {}).get("title"),
             "url": (spec.get("seo") or {}).get("canonical") or f"/blog/{doc.get('slug') or ''}",
             "text": text, "vector": embed(text),
             "evergreen": bool(spec.get("evergreen")),
             "published_at": doc.get("created_at") or spec.get("created_at"),
             "reviewed_at": (spec.get("evergreen") or {}).get("reviewed_at")}
    _index.put(entry)
    return entry


def index_all(docs: Iterable[dict]) -> int:
    count = 0
    for doc in docs:
        index_entry(doc)
        count += 1
    return count


def index_rows() -> list[dict]:
    return sorted(_index.all(), key=lambda row: str(row.get("published_at") or ""), reverse=True)


def reset() -> None:
    _index.clear_memory()


def _limit(value: int | None, env_name: str, default: int) -> int:
    if value is not None:
        return max(0, int(value))
    raw = str(os.environ.get(env_name) or "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _threshold(value: float | None, env_name: str, default: float) -> float:
    if value is not None:
        return float(value)
    raw = str(os.environ.get(env_name) or "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


# ------------------------------------------------------------------ кандидаты и ссылки
def _existing_urls(spec: dict) -> set[str]:
    urls: set[str] = set()
    for block in spec.get("blocks") or []:
        props = block.get("props") or {}
        for key in ("url",):
            if props.get(key):
                urls.add(str(props[key]))
        for item in props.get("items") or []:
            if isinstance(item, dict) and item.get("url"):
                urls.add(str(item["url"]))
    return urls


def anchor(target: dict) -> str:
    """Текст ссылки: заголовок статьи, а не «читать далее» — иначе ссылка не несёт смысла."""
    title = str(target.get("title") or "").strip().rstrip(".")
    return title[:70] if title else ""


def candidates(query: str | dict, *, spec: dict | None = None, limit: int | None = None,
               min_score: float | None = None, exclude: Iterable[str] = ()) -> list[dict]:
    """Ближайшие статьи по смыслу: косинус вектора статьи с векторами индекса."""
    rows = index_rows()
    if not rows:
        return []
    text = query if isinstance(query, str) else _entry_text(query)
    vector = embed(text)
    taken = {str(item) for item in exclude}
    own_key = _spec_hash(spec) if isinstance(spec, dict) else None
    scored: list[dict] = []
    for row in rows:
        if row.get("key") == own_key:
            continue
        if row.get("article_id") in taken or row.get("slug") in taken:
            continue
        score = cosine(vector, row.get("vector") or [])
        scored.append({"article_id": row.get("article_id"), "slug": row.get("slug"),
                       "title": row.get("title"), "url": row.get("url"),
                       "anchor": anchor(row), "score": score,
                       "evergreen": bool(row.get("evergreen"))})
    scored.sort(key=lambda item: item["score"], reverse=True)
    return scored[: _limit(limit, "ARTICLE_MAX_LINKS", DEFAULT_MAX_LINKS)]


def link_plan(spec: dict, *, limit: int | None = None, min_score: float | None = None,
              corpus_rows: list[dict] | None = None) -> dict:
    """Что поставить в «Читайте также». Отказ называется, а не превращается в пустой блок."""
    if corpus_rows is not None:
        index_all(corpus_rows)
    max_links = _limit(limit, "ARTICLE_MAX_LINKS", DEFAULT_MAX_LINKS)
    threshold = _threshold(min_score, "ARTICLE_LINK_MIN_SCORE", DEFAULT_MIN_SCORE)
    rows = index_rows()
    if len([row for row in rows if row.get("key") != _spec_hash(spec)]) == 0:
        return {"ok": False, "code": "corpus_empty", "links": [], "threshold": threshold,
                "reason": "в индексе нет других статей: перелинковывать не с чем"}
    already = _existing_urls(spec)
    scored = candidates(spec, spec=spec, limit=max_links * 3, min_score=threshold)
    accepted = [item for item in scored
                if item["score"] >= threshold and item["url"] not in already]
    if not accepted:
        best = scored[0]["score"] if scored else 0.0
        above = [item for item in scored if item["score"] >= threshold]
        if above and all(item["url"] in already for item in above):
            return {"ok": False, "code": "already_linked", "links": [], "threshold": threshold,
                    "considered": len(scored), "best_score": best,
                    "reason": f"ближайшие по смыслу статьи уже связаны "
                              f"({len(already)} ссылок в статье)"}
        return {"ok": False, "code": "no_candidates", "links": [], "threshold": threshold,
                "considered": len(scored), "best_score": best,
                "reason": (f"ближайшая статья ниже порога {threshold}: {best} — "
                           f"ссылка «по теме маркетинга» читателю не помогает")}
    accepted = accepted[:max_links]
    return {"ok": True, "links": accepted, "threshold": threshold,
            "considered": len(scored), "limit": max_links,
            "reason": f"{len(accepted)} из {len(scored)} ближайших статей выше порога "
                      f"{threshold}: ссылка ставится по смыслу, а не по тегам"}


def check_links(spec: dict) -> list[dict]:
    """Проверки блока ссылок: битая, дублирующая, на себя, без текста."""
    issues: list[dict] = []
    own_urls = {str((spec.get("seo") or {}).get("canonical") or "").strip()}
    seen: dict[str, int] = {}
    for index, block in enumerate(spec.get("blocks") or []):
        if block.get("type") != "related":
            continue
        items = (block.get("props") or {}).get("items") or []
        if not items:
            issues.append({"code": "related_empty", "field": f"blocks[{index}].items",
                           "message": "блок «Читайте также» без ссылок — это пустое место"})
        for position, item in enumerate(items):
            field = f"blocks[{index}].items[{position}]"
            url = str((item or {}).get("url") or "").strip()
            if not url:
                issues.append({"code": "link_broken", "field": field,
                               "message": "у ссылки нет адреса: читатель придёт в никуда"})
                continue
            if url in own_urls:
                issues.append({"code": "link_self", "field": field,
                               "message": "статья ссылается на себя"})
            if not str((item or {}).get("anchor") or "").strip():
                issues.append({"code": "link_anchor_missing", "field": field,
                               "message": "ссылка без текста: «читать далее» не объясняет, куда"})
            if url in seen:
                issues.append({"code": "link_duplicate", "field": field,
                               "message": f"та же ссылка уже стоит в blocks[{seen[url]}]"})
            seen.setdefault(url, index)
    return issues


def apply_links(spec: dict, links: list[dict], *, reason: str | None = None) -> dict:
    """Вставить блок «Читайте также» перед призывом и оставить след в истории."""
    if not links:
        return {"ok": False, "code": "no_links", "spec": spec,
                "reason": "нечего вставлять: перелинковка не нашла ссылок выше порога"}
    items = [{"url": item["url"], "anchor": item.get("anchor") or anchor(item),
              "title": item.get("title") or "", "score": item.get("score"),
              "evergreen": bool(item.get("evergreen"))} for item in links]
    digest = hashlib.sha256(json_key(items).encode("utf-8")).hexdigest()[:10]
    block = {"id": f"blk_rel_{digest}",
             "type": "related", "props": {"items": items},
             "reason": reason or "ближайшие по смыслу статьи: читатель продолжает тему, "
                                 "а не уходит в поиск",
             "alt": None, "caption": None, "source": None}
    blocks = list(spec.get("blocks") or [])
    position = next((index for index, existing in enumerate(blocks)
                     if existing.get("type") in {"cta", "faq"}), len(blocks))
    blocks.insert(position, block)
    updated = {**spec, "blocks": blocks,
               "history": list(spec.get("history") or []) + [{
                   "at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                   "action": "links", "added": len(items),
                   "reason": f"перелинковка по смыслу: {len(items)} ссылок выше порога"}]}
    return {"ok": True, "spec": updated, "block": block, "added": len(items),
            "position": position}


def json_key(payload: Any) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def link_stats(spec: dict) -> dict:
    rows = [item for block in spec.get("blocks") or [] if block.get("type") == "related"
            for item in (block.get("props") or {}).get("items") or []]
    return {"links": len(rows), "unique": len({item.get("url") for item in rows}),
            "evergreen": sum(1 for item in rows if item.get("evergreen")),
            "avg_score": round(sum(float(item.get("score") or 0) for item in rows)
                               / len(rows), 3) if rows else 0.0}


# ------------------------------------------------------------------ вечнозелёные статьи
def _now(value: str | None = None) -> datetime:
    if value:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return datetime.now(timezone.utc)


def evergreen(spec: dict, *, review_days: int | None = None) -> dict:
    """Пометить статью вечнозелёной: у неё появляется срок ревью, а не «обновим как-нибудь»."""
    days = _limit(review_days, "ARTICLE_REVIEW_DAYS", DEFAULT_REVIEW_DAYS)
    return {**spec, "evergreen": {"review_days": days,
                                  "reviewed_at": spec.get("created_at") or _now().isoformat(),
                                  "notes": "цифры обновляются по материалам клиента"}}


def freshness(spec: dict, *, now: str | None = None, review_days: int | None = None) -> dict:
    mark = spec.get("evergreen") or {}
    if not mark:
        return {"evergreen": False, "due": False, "age_days": None, "review_days": None,
                "reason": "статья не помечена вечнозелёной: срок ревью не назначен"}
    days = _limit(review_days if review_days is not None else mark.get("review_days"),
                  "ARTICLE_REVIEW_DAYS", DEFAULT_REVIEW_DAYS)
    reviewed = str(mark.get("reviewed_at") or spec.get("created_at") or "").strip()
    if not reviewed:
        return {"evergreen": True, "due": True, "age_days": None, "review_days": days,
                "reviewed_at": None, "reason": "нет даты ревью: срок неизвестен, считаем должным"}
    moment = _now(now)
    try:
        reviewed_at = _now(reviewed)
    except ValueError:
        return {"evergreen": True, "due": True, "age_days": None, "review_days": days,
                "reviewed_at": reviewed, "reason": "дата ревью не читается"}
    age = max(0, (moment - reviewed_at).days)
    return {"evergreen": True, "due": age >= days, "age_days": age, "review_days": days,
            "reviewed_at": reviewed,
            "due_in_days": max(0, days - age),
            "reason": (f"ревью было {age} дн. назад при сроке {days} дн."
                       if age >= days else
                       f"статья свежая: {age} дн. из {days}")}


def material_key(block: dict, numbers: dict[str, dict]) -> str:
    """Какой ряд материала кормит визуал: `props.material`, иначе источник блока."""
    props = block.get("props") or {}
    for candidate in (props.get("material"), block.get("source")):
        code = str(candidate or "").strip()
        if code and code in numbers:
            return code
    return str(props.get("material") or block.get("source") or "").strip()


def _numbers(materials: Iterable[dict]) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    for material in materials or []:
        code = str((material or {}).get("key") or (material or {}).get("name") or "").strip()
        if code:
            rows[code] = material
    return rows


def refresh_plan(spec: dict, materials: Iterable[dict] | None = None, *,
                 now: str | None = None) -> dict:
    """Что устарело и что можно обновить по материалам — до всякой правки."""
    state = freshness(spec, now=now)
    if not state["evergreen"]:
        return {"ok": False, "code": "not_evergreen", "needed": False, **state}
    if not state["due"]:
        return {"ok": False, "code": "fresh", "needed": False, **state}
    numbers = _numbers(materials or [])
    updatable: list[dict] = []
    skipped: list[dict] = []
    for position, block in enumerate(spec.get("blocks") or []):
        key = material_key(block, numbers)
        if not key or block.get("type") not in {"chart", "table", "stat"}:
            continue
        if not block.get("source"):
            skipped.append({"field": f"blocks[{position}].source", "material": key,
                            "code": "source_missing",
                            "reason": "у цифры нет источника: обновлять нечего"})
            continue
        if key not in numbers:
            skipped.append({"field": f"blocks[{position}]", "material": key,
                            "code": "material_missing",
                            "reason": "в материалах нет этого ряда — число остаётся прежним"})
            continue
        updatable.append({"field": f"blocks[{position}]", "material": key,
                          "block": block.get("type")})
    if not updatable:
        return {"ok": False, "code": "no_materials", "needed": True, "updatable": [],
                "skipped": skipped, **state,
                "reason": "обновлять нечего: ни один визуал не опирается на присланные материалы"}
    return {"ok": True, "needed": True, "updatable": updatable, "skipped": skipped, **state,
            "reason": f"{state['reason']}; обновить можно {len(updatable)} визуалов, "
                      f"пропущено {len(skipped)}"}


def refresh(spec: dict, materials: Iterable[dict] | None = None, *,
            now: str | None = None, reason: str | None = None) -> dict:
    """Обновление данных вечнозелёной статьи: только то, что подтверждено материалами."""
    plan_data = refresh_plan(spec, materials, now=now)
    if not plan_data.get("ok"):
        return {"ok": False, "code": plan_data["code"], "spec": spec,
                "skipped": plan_data.get("skipped") or [],
                "reason": plan_data.get("reason")}
    numbers = _numbers(materials or [])
    blocks = [dict(block) for block in spec.get("blocks") or []]
    changes: list[dict] = []
    skipped = list(plan_data.get("skipped") or [])
    for position, block in enumerate(blocks):
        props = dict(block.get("props") or {})
        key = material_key(block, numbers)
        if key not in numbers or block.get("type") not in {"chart", "table", "stat"}:
            continue
        source_row = numbers[key]
        if block.get("type") == "chart":
            series = list(source_row.get("series") or [])
            if series and series != props.get("series"):
                changes.append({"field": f"blocks[{position}].series", "material": key,
                                "from": props.get("series"), "to": series})
                props["series"] = series
        elif block.get("type") == "table":
            rows = source_row.get("rows") or []
            if rows and rows != props.get("rows"):
                changes.append({"field": f"blocks[{position}].rows", "material": key,
                                "from": props.get("rows"), "to": rows})
                props["rows"] = rows
        elif block.get("type") == "stat":
            value = source_row.get("value") or (source_row.get("series") or [None])[-1]
            if value is not None and str(value) != str(props.get("value")):
                changes.append({"field": f"blocks[{position}].value", "material": key,
                                "from": props.get("value"), "to": value})
                props["value"] = value
        if changes and block.get("type"):
            blocks[position] = {**block, "props": props}
    if not changes:
        return {"ok": False, "code": "no_changes", "spec": spec, "skipped": skipped,
                "reason": "данные в материалах совпадают со статьёй: обновлять нечего, "
                          "версия не растёт"}
    moment = _now(now).replace(microsecond=0).isoformat()
    mark = dict(spec.get("evergreen") or {})
    mark["reviewed_at"] = moment
    history = list(spec.get("history") or []) + [{
        "at": moment, "action": "refresh", "changed": len(changes),
        "reason": reason or plan_data["reason"],
        "changes": [{key: change[key] for key in ("field", "material")}
                    for change in changes],
        "skipped": [{key: row[key] for key in ("field", "code")} for row in skipped]}]
    updated = {**spec, "blocks": blocks, "evergreen": mark, "history": history,
               "updated_at": moment,
               "version": int(spec.get("version") or 1) + 1}
    return {"ok": True, "code": None, "spec": updated, "changes": changes,
            "skipped": skipped, "version": updated["version"], "updated_at": moment,
            "reason": f"обновлено {len(changes)} визуалов по материалам клиента; "
                      f"пропущено {len(skipped)} без источника"}


def evergreen_report(docs: Iterable[dict], *, now: str | None = None) -> dict:
    """Сводка по вечнозелёным статьям: что ждёт ревью и на сколько просрочено."""
    rows: list[dict] = []
    for doc in docs or []:
        spec = doc.get("spec") or {}
        state = freshness(spec, now=now)
        rows.append({"article_id": doc.get("id"), "slug": doc.get("slug"),
                     "title": (spec.get("seo") or {}).get("title"), **state})
    due = [row for row in rows if row["due"]]
    return {"total": len(rows), "evergreen": sum(1 for row in rows if row["evergreen"]),
            "due": len(due), "items": rows,
            "review_days": _limit(None, "ARTICLE_REVIEW_DAYS", DEFAULT_REVIEW_DAYS)}
