"""A/B заголовка и первого абзаца на одном теле статьи (этап I6, ADR 0024).

Задача этапа: решение о заголовке принимается по данным, а не по вкусу редактора. Отсюда четыре
правила, и все они проверяемы.

**1. A/B — только заголовок и вводка.** Тело статьи у всех вариантов одно и то же, и это не
соглашение, а проверка: у теста есть `body_hash`, и вариант с другим телом не создаётся
(`body_changed`). Иначе «заголовок победил» означало бы «победила другая статья».

**2. Варианты — данные, а не фантазия модели.** `HEADLINE_KINDS` — реестр формул (вопрос, число,
инструкция, выгода) с гипотезой у каждой. Ни одна формула не обещает CTR: мы не знаем будущий
клик, мы знаем, что именно проверяем. Вариант, который ломает правила (длиннее 70 знаков, теряет
ключевое слово, содержит запрещённое обещание из пака), отбрасывается с причиной.

**3. Разбиение стабильное.** Один и тот же читатель всегда видит один и тот же вариант: корзина
считается из `sha256(test_id + visitor)`. Случайная выдача на каждый показ испортила бы и опыт
читателя, и замер.

**4. Победитель — по статистике, а не по «больше кликов».** Двухвыборочный z-тест для долей с
порогом `ARTICLE_AB_ALPHA` (0.05) и минимумом показов `ARTICLE_AB_MIN_IMPRESSIONS`: пока данных
мало — `not_enough_data`, если разница в пределах шума — `no_difference`. Применение возможно
только после решения (`no_winner`), а версия статьи растёт на одну.

Тело статьи не переписывается ни на одном шаге: `apply()` меняет `seo.title` и текст блока
`lead`, ставит запись в `history` и поднимает версию.
"""
from __future__ import annotations

import hashlib
import math
import os
from datetime import datetime, timezone
from typing import Any, Iterable

from .docs import Collection

_tests = Collection("article_ab_tests")

#: минимальные показы на вариант: на сотне показов разница в 5 кликов — это шум
DEFAULT_MIN_IMPRESSIONS = 300
#: порог значимости: двухсторонний z-тест, 95 %
DEFAULT_ALPHA = 0.05
#: предельная длина заголовка: тот же лимит, что у SEO-заголовка статьи
MAX_TITLE = 70

#: Реестр формул заголовка. `template` получает `keyword`, `heading`, `count`; `hypothesis` —
#: что именно проверяет вариант. Обещаний CTR здесь нет намеренно: их нельзя проверить заранее.
#: Формулы собраны так, чтобы русская грамматика не ломалась от числа: «3 вещи» и «5 вещей»
#: дают разные формы, и заголовок «5 вещи» — это не A/B, это опечатка.
HEADLINE_KINDS: tuple[dict[str, Any], ...] = (
    {"code": "question",
     "template": "{Keyword}? Разбираем по шагам",
     "hypothesis": "вопрос обещает ответ — проверяем, растёт ли переход из выдачи"},
    {"code": "number",
     "template": "{Keyword}: {count} {count_word}, которые стоит проверить",
     "hypothesis": "число задаёт объём — проверяем, понятнее ли читателю, что внутри"},
    {"code": "howto",
     "template": "{Keyword}: инструкция без воды и общих слов",
     "hypothesis": "обещание инструкции снижает риск «снова общие слова» — проверяем"},
    {"code": "benefit",
     "template": "{Keyword}: что это даёт и сколько стоит",
     "hypothesis": "выгода и цена в заголовке отвечают на два вопроса сразу — проверяем"},
)

#: варианты вводки: полная, короткая (первое предложение) и «сводка цифрами»
LEAD_KINDS: tuple[dict[str, Any], ...] = (
    {"code": "full", "hypothesis": "полная вводка даёт контекст, но занимает экран"},
    {"code": "short", "hypothesis": "короткая вводка быстрее приводит к сути"},
    {"code": "digest", "hypothesis": "вводка со перечислением показывает структуру сразу"},
)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _env_int(name: str, default: int) -> int:
    raw = str(os.environ.get(name) or "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = str(os.environ.get(name) or "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _body_hash(spec: dict) -> str:
    """Отпечаток тела статьи: всё, кроме заголовка и вводки.

    Именно это делает A/B честным: если у вариантов разойдётся тело, сравнение потеряет смысл.
    """
    payload = [{"type": block.get("type"), "props": block.get("props"),
                "reason": block.get("reason")}
               for block in spec.get("blocks") or []
               if block.get("type") != "lead"]
    import json

    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def _lead_block(spec: dict) -> dict | None:
    return next((block for block in spec.get("blocks") or [] if block.get("type") == "lead"), None)


def _keyword(spec: dict) -> str:
    keywords = (spec.get("seo") or {}).get("keywords") or []
    if keywords:
        return str(keywords[0])
    return str((spec.get("seo") or {}).get("title") or "").split(":")[0]


def plural(count: int, one: str, few: str, many: str) -> str:
    """Русская форма после числа: 1 вещь, 3 вещи, 5 вещей."""
    value = abs(int(count))
    if value % 10 == 1 and value % 100 != 11:
        return one
    if 2 <= value % 10 <= 4 and not 12 <= value % 100 <= 14:
        return few
    return many


def _capitalize(text: str) -> str:
    return text[:1].upper() + text[1:] if text else text


def _banned(spec: dict) -> tuple[str, ...]:
    policy = spec.get("policy") or {}
    return tuple(str(phrase).lower() for phrase in policy.get("banned_phrases") or ())


def headline_variants(spec: dict, *, count: int = 3,
                      kinds: Iterable[str] | None = None) -> dict:
    """Варианты заголовка и вводки с честными причинами отказа.

    Возвращает `variants` — только те, что прошли правила, и `skipped` — с кодом и причиной:
    вариант длиннее лимита, потерял ключевое слово или содержит запрещённое обещание.
    """
    keyword = _keyword(spec).strip()
    style = spec.get("template")
    rows = [row for row in HEADLINE_KINDS if not kinds or row["code"] in set(kinds)]
    lead = (_lead_block(spec) or {}).get("props", {}).get("text") or ""
    headings = [str((block.get("props") or {}).get("text") or "")
                for block in spec.get("blocks") or [] if block.get("type") == "heading"]
    stats = spec.get("stats") or {}
    # число в заголовке берётся из структуры статьи: подзаголовки — «сколько вещей внутри»,
    # визуалы — запасной вариант, если подзаголовков нет
    count_value = max(2, min(9, len(headings) or int(stats.get("visuals") or 0) or 3))
    banned = _banned(spec)
    lead_options = _lead_variants(lead, headings)

    variants: list[dict] = []
    skipped: list[dict] = []
    for position, row in enumerate(rows):
        if len(variants) >= max(2, int(count)):
            break
        title = row["template"].format(
            Keyword=_capitalize(keyword), keyword=keyword,
            heading=(headings[0] if headings else keyword), count=count_value,
            count_word=plural(count_value, "вещь", "вещи", "вещей"))
        title = " ".join(title.split())
        if len(title) > MAX_TITLE:
            skipped.append({"kind": row["code"], "code": "title_too_long", "title": title,
                            "reason": f"заголовок длиннее {MAX_TITLE} знаков: {len(title)}"})
            continue
        if keyword and keyword.lower() not in title.lower():
            skipped.append({"kind": row["code"], "code": "keyword_lost", "title": title,
                            "reason": f"в заголовке нет ключевого слова «{keyword}»"})
            continue
        hit = next((phrase for phrase in banned if phrase in title.lower()), None)
        if hit:
            skipped.append({"kind": row["code"], "code": "banned_phrase", "title": title,
                            "reason": f"заголовок содержит запрещённое паком обещание «{hit}»"})
            continue
        lead_row = lead_options[position % len(lead_options)] if lead_options else \
            {"code": "full", "text": lead, "hypothesis": ""}
        variants.append({"code": row["code"], "kind": row["code"],
                         "title": title, "title_length": len(title),
                         "lead": lead_row["text"], "lead_kind": lead_row["code"],
                         "hypothesis": row["hypothesis"],
                         "lead_hypothesis": lead_row["hypothesis"],
                         "template": style})
    if len(variants) < 2:
        return {"ok": False, "code": "no_variants", "variants": [], "skipped": skipped,
                "reason": "после правил осталось меньше двух вариантов: сравнивать нечего"}
    return {"ok": True, "variants": variants, "skipped": skipped,
            "reason": f"{len(variants)} варианта проходят правила (длина ≤{MAX_TITLE}, "
                      f"ключевое слово на месте, без запрещённых обещаний)"}


def _lead_variants(lead: str, headings: list[str]) -> list[dict]:
    text = " ".join(str(lead or "").split())
    if not text:
        return []
    rows = [{"code": "full", "text": text, "hypothesis": LEAD_KINDS[0]["hypothesis"]}]
    first = text.split(". ")[0].strip()
    if first and first != text:
        short = first if first.endswith(".") else f"{first}."
        rows.append({"code": "short", "text": short[:220],
                     "hypothesis": LEAD_KINDS[1]["hypothesis"]})
    if headings:
        digest = f"Коротко: {', '.join(headings[:3])}."
        if digest.lower() != text.lower():
            rows.append({"code": "digest", "text": digest[:220],
                         "hypothesis": LEAD_KINDS[2]["hypothesis"]})
    return rows


# ------------------------------------------------------------------ тест
def create(spec: dict, *, count: int = 3, article_id: str | None = None,
           kinds: Iterable[str] | None = None, salt: str | None = None) -> dict:
    """Создать тест на этой спеке: варианты + отпечаток тела + правила замера.

    Повторный вызов с тем же телом и теми же вариантами возвращает существующий тест (ретрай не
    плодит дубли); `salt` нужен, когда на одной статье действительно запускают второй тест.
    """
    made = headline_variants(spec, count=count, kinds=kinds)
    if not made.get("ok"):
        return made
    seed = (str(article_id) + _body_hash(spec) + made["variants"][0]["title"] + str(salt or ""))
    test_id = f"ab_{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:12]}"
    existing = _tests.get(test_id)
    if existing:
        return {"ok": True, "reused": True, "test": existing}
    doc = {"id": test_id, "article_id": article_id, "slug": spec.get("slug"),
           "body_hash": _body_hash(spec), "status": "running",
           "variants": [{**row, "impressions": 0, "clicks": 0} for row in made["variants"]],
           "skipped": made["skipped"],
           "baseline": {"title": (spec.get("seo") or {}).get("title"),
                        "lead": ((_lead_block(spec) or {}).get("props") or {}).get("text")},
           "min_impressions": _env_int("ARTICLE_AB_MIN_IMPRESSIONS", DEFAULT_MIN_IMPRESSIONS),
           "alpha": _env_float("ARTICLE_AB_ALPHA", DEFAULT_ALPHA),
           "created_at": _now(), "decision": None}
    _tests.put(doc)
    return {"ok": True, "reused": False, "test": doc}


def test(test_id: str) -> dict | None:
    return _tests.get(test_id)


def tests(limit: int = 50) -> list[dict]:
    rows = sorted(_tests.all(), key=lambda row: str(row.get("created_at") or ""), reverse=True)
    return rows[:limit]


def assign(test_id: str, visitor: str) -> dict:
    """Какой вариант видит читатель: стабильно от `visitor`, а не случайно от показа."""
    doc = _tests.get(test_id)
    if not doc:
        return {"ok": False, "code": "test_not_found"}
    if not str(visitor or "").strip():
        return {"ok": False, "code": "visitor_required",
                "reason": "без идентификатора читателя разбиение не стабильно"}
    digest = hashlib.sha256(f"{test_id}:{visitor}".encode("utf-8")).digest()
    variants = doc["variants"]
    index = int.from_bytes(digest[:4], "big") % len(variants)
    chosen = variants[index]
    return {"ok": True, "test_id": test_id, "bucket": index,
            "code": chosen["code"], "title": chosen["title"], "lead": chosen["lead"],
            "body_hash": doc["body_hash"], "status": doc["status"]}


def record(test_id: str, code: str, *, impressions: int, clicks: int) -> dict:
    """Записать накопленные показы и клики варианта (суммируются, а не перезаписываются)."""
    doc = _tests.get(test_id)
    if not doc:
        return {"ok": False, "code": "test_not_found"}
    if doc["status"] == "applied":
        return {"ok": False, "code": "test_applied",
                "reason": "тест применён: дальше мерить нечего"}
    variant = next((row for row in doc["variants"] if row["code"] == code), None)
    if not variant:
        return {"ok": False, "code": "variant_unknown", "known": [row["code"] for row in doc["variants"]]}
    if impressions < 0 or clicks < 0:
        return {"ok": False, "code": "invalid_metrics",
                "reason": "отрицательные показы или клики"}
    if clicks > impressions:
        return {"ok": False, "code": "invalid_metrics",
                "reason": f"кликов ({clicks}) больше, чем показов ({impressions})"}
    variant["impressions"] = int(variant.get("impressions") or 0) + int(impressions)
    variant["clicks"] = int(variant.get("clicks") or 0) + int(clicks)
    variant["ctr"] = round(variant["clicks"] / variant["impressions"], 4) \
        if variant["impressions"] else 0.0
    _tests.put(doc)
    return {"ok": True, "test_id": test_id, "variant": variant,
            "total": sum(row["impressions"] for row in doc["variants"])}


def _phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def ztest(clicks_a: int, impressions_a: int, clicks_b: int, impressions_b: int) -> tuple[float, float]:
    """Двухвыборочный z-тест для долей: (z, p_value)."""
    if impressions_a <= 0 or impressions_b <= 0:
        return 0.0, 1.0
    p_a = clicks_a / impressions_a
    p_b = clicks_b / impressions_b
    pooled = (clicks_a + clicks_b) / (impressions_a + impressions_b)
    se = math.sqrt(pooled * (1 - pooled) * (1 / impressions_a + 1 / impressions_b))
    if se == 0:
        return 0.0, 1.0
    z = (p_a - p_b) / se
    return round(z, 4), round(2 * (1 - _phi(abs(z))), 6)


def evaluate(test_id: str) -> dict:
    """Решение по данным: мало показов — ждать, шум — нет разницы, иначе — победитель."""
    doc = _tests.get(test_id)
    if not doc:
        return {"ok": False, "code": "test_not_found"}
    variants = doc["variants"]
    need = int(doc.get("min_impressions") or DEFAULT_MIN_IMPRESSIONS)
    alpha = float(doc.get("alpha") or DEFAULT_ALPHA)
    ctrs = {row["code"]: round((row.get("clicks") or 0) / (row.get("impressions") or 1), 4)
            for row in variants}
    short = [row["code"] for row in variants if int(row.get("impressions") or 0) < need]
    if short:
        return {"ok": False, "code": "not_enough_data", "needed": need,
                "waiting": short, "ctr": ctrs, "alpha": alpha,
                "reason": f"показов меньше {need} у вариантов: {', '.join(short)} — "
                          f"решение было бы гаданием"}
    ranked = sorted(variants, key=lambda row: ctrs[row["code"]], reverse=True)
    best, worst = ranked[0], ranked[-1]
    if len(variants) > 2:
        # сравниваем лидера со следующим: «лучше всех» при трёх вариантах — это про пару
        worst = ranked[1]
    z, p_value = ztest(best["clicks"], best["impressions"], worst["clicks"], worst["impressions"])
    uplift = round(ctrs[best["code"]] - ctrs[worst["code"]], 4)
    if p_value > alpha:
        return {"ok": False, "code": "no_difference", "winner": None, "ctr": ctrs,
                "z": z, "p_value": p_value, "uplift": uplift, "alpha": alpha,
                "reason": f"разница {uplift} в пределах шума (p = {p_value} > {alpha}): "
                          f"менять заголовок не на чем"}
    decision = {"winner": best["code"], "winner_title": best["title"],
                "leader": worst["code"], "ctr": ctrs, "z": z, "p_value": p_value,
                "uplift": uplift, "alpha": alpha, "decided_at": _now(),
                "reason": f"{best['code']} лучше {worst['code']} на {uplift} при "
                          f"p = {p_value} < {alpha}"}
    doc["status"] = "decided"
    doc["decision"] = decision
    _tests.put(doc)
    return {"ok": True, "code": "decision", **decision, "test_id": test_id}


def apply(test_id: str, spec: dict) -> dict:
    """Применить победителя к статье: заголовок и вводка меняются, тело остаётся прежним."""
    doc = _tests.get(test_id)
    if not doc:
        return {"ok": False, "code": "test_not_found"}
    if doc["status"] == "applied":
        return {"ok": False, "code": "already_applied",
                "reason": "этот тест уже применён к статье"}
    decision = doc.get("decision") or {}
    if not decision.get("winner"):
        return {"ok": False, "code": "no_winner",
                "reason": "победителя нет: сначала нужно решение по данным"}
    if _body_hash(spec) != doc["body_hash"]:
        return {"ok": False, "code": "body_changed",
                "reason": "тело статьи изменилось после старта теста: применять нельзя, "
                          "победил бы другой текст, а не другой заголовок"}
    winner = next(row for row in doc["variants"] if row["code"] == decision["winner"])
    title = winner["title"] if len(winner["title"]) <= MAX_TITLE else (spec.get("seo") or {}).get("title")
    lead_text = winner["lead"]
    blocks = []
    for block in spec.get("blocks") or []:
        if block.get("type") == "lead":
            blocks.append({**block, "props": {**(block.get("props") or {}), "text": lead_text}})
        else:
            blocks.append(block)
    history = list(spec.get("history") or []) + [{
        "at": _now(), "action": "abtest",
        "reason": f"вариант {winner['code']} выиграл по данным: CTR "
                  f"{decision['ctr'].get(winner['code'])} против "
                  f"{decision['ctr'].get(decision.get('leader'))} при p = {decision.get('p_value')}",
        "test_id": test_id}]
    updated = {**spec, "blocks": blocks, "history": history,
               "seo": {**(spec.get("seo") or {}), "title": title},
               "version": int(spec.get("version") or 1) + 1,
               "updated_at": _now()}
    doc["status"] = "applied"
    doc["applied_at"] = _now()
    _tests.put(doc)
    return {"ok": True, "spec": updated, "winner": winner["code"], "title": title,
            "lead": lead_text, "version": updated["version"], "decision": decision,
            "body_hash": doc["body_hash"],
            "reason": f"заголовок «{title}» и вводка варианта {winner['code']} применены; "
                      f"тело статьи не изменилось (отпечаток совпал)"}


def report(test_id: str) -> dict:
    """Сводка теста для интерфейса: варианты, CTR, ожидаемый объём, состояние."""
    doc = _tests.get(test_id)
    if not doc:
        return {"ok": False, "code": "test_not_found"}
    need = int(doc.get("min_impressions") or DEFAULT_MIN_IMPRESSIONS)
    return {"ok": True, "test_id": test_id, "status": doc["status"],
            "slug": doc.get("slug"), "body_hash": doc["body_hash"],
            "variants": [{key: row[key] for key in
                          ("code", "title", "title_length", "lead", "hypothesis",
                           "impressions", "clicks", "ctr") if key in row}
                         for row in doc["variants"]],
            "skipped": doc.get("skipped") or [], "decision": doc.get("decision"),
            "min_impressions": need, "alpha": doc.get("alpha"),
            "progress": {row["code"]: round(min(1.0, (row.get("impressions") or 0) / need), 3)
                         for row in doc["variants"]}}


def reset() -> None:
    _tests.clear_memory()
