"""Хук-лаборатория: три варианта первых двух секунд и честный победитель (этап F, ADR 0023).

Зачем отдельный модуль, а не «сгенерировать три заголовка»:

**1. Досмотр решается в первых двух секундах.** Остальные сцены у всех вариантов одинаковы —
поэтому разница в метрике относится к хуку, а не к ролику целиком. Лаборатория строит варианты
как одну спеку с разным первым кадром, а не три разных ролика: тогда сравнение честное.

**2. Хук не выдумывает факты.** Варианты собираются из данных поста (цифра, число пунктов,
сам заголовок) — не из «креативных» обещаний. Обещания результата («гарантируем», «точно
вырастет») запрещены стоп-листом `compliance.py`, и лаборатория их не предлагает даже как
вариант: A/B-тест не может нарушать закон.

**3. Победитель без данных — не победитель.** Если показов меньше порога, лаборатория отвечает
`not_enough_data`, а не «выбирает лучший из нулей». При равенстве метрик она честно говорит
`tie`, а не выдаёт случайный вариант за вывод.

**4. Победитель фиксируется.** После решения лаб сохраняет `winner_variant_id` и `spec_hash`
победителя: повторное решение возвращает тот же вариант, а ролик собирается по уже
отрендеренной спеке (идемпотентность хранилища — этап A).
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from . import media_reel
from .docs import Collection

#: сколько вариантов имеет смысл сравнивать: больше — уже не хватает трафика на ячейку
MIN_VARIANTS, MAX_VARIANTS, DEFAULT_VARIANTS = 2, 5, 3
#: порог показов на вариант: ниже него разница в досмотре — шум, а не сигнал
DEFAULT_MIN_VIEWS = 100
#: разница метрики, ниже которой варианты считаются равными
TIE_EPSILON = 0.01

_labs = Collection("media_labs")

#: стратегии хука — данные. «{number}» — первая цифра из поста, «{count}» — число пунктов,
#: «{title}» — исходный заголовок. Новая стратегия — запись, а не ветка в коде.
STRATEGIES: dict[str, dict[str, Any]] = {
    "number": {
        "title": "Цифра из поста",
        "template": "{number} — главное в этом посте",
        "needs": "number",
    },
    "count": {
        "title": "Число пунктов",
        "template": "{count} {word} — коротко и по делу",
        "needs": "count",
    },
    "direct": {
        "title": "Прямой заголовок",
        "template": "{title}",
        "needs": "title",
    },
    "question": {
        "title": "Вопрос к аудитории",
        "template": "{topic} — на что смотреть в первую очередь?",
        "needs": "topic",
    },
}
#: порядок «силы» гипотез: сначала цифра (её видно в первом кадре), затем перечисление,
#: затем вопрос, и только потом прямой заголовок
STRATEGY_ORDER = ("number", "count", "question", "direct")

_NUMBER_RE = re.compile(r"\d[\d\s]*(?:[.,]\d+)?\s*(?:%|₽|руб\.?|тыс\.?|млн|шт\.?|дн[а-я]*|год[а-я]*|лет)?",
                        re.I)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _uid() -> str:
    return f"lab_{uuid.uuid4().hex[:12]}"


def _plural(count: int, forms: tuple[str, str, str]) -> str:
    """1 пункт / 2 пункта / 5 пунктов — иначе хук выдаёт «2 пункт»."""
    tail = count % 100
    if 11 <= tail <= 14:
        return forms[2]
    last = count % 10
    if last == 1:
        return forms[0]
    if 2 <= last <= 4:
        return forms[1]
    return forms[2]


def _numbers(text: str) -> list[str]:
    found = [match.group(0).strip() for match in _NUMBER_RE.finditer(text or "")]
    return [item for item in found if any(char.isdigit() for char in item)]


def _topic(post: dict) -> str:
    """Тема для вопроса: подзаголовок или первые слова заметки.

    Заголовок в тему не берём: вопрос из самого заголовка («Скидка — на что смотреть?»)
    дублирует прямой вариант и превращает A/B-тест в сравнение двух одинаковых идей.
    """
    eyebrow = str(post.get("eyebrow") or "").strip()
    if eyebrow:
        return eyebrow.split("·")[0].strip()
    note = str(post.get("note") or "").strip().rstrip(".!?")
    if note:
        return " ".join(note.split()[:4])
    return ""


def slots(post: dict) -> dict[str, Any]:
    """Что вообще есть в посте для хука. Нет данных — нет и варианта (а не выдумка)."""
    title = str(post.get("title") or post.get("headline") or "").strip()
    items = [str(item).strip() for item in (post.get("items") or []) if str(item).strip()]
    numbers = _numbers(" ".join([title, str(post.get("note") or "")]))
    return {"title": title, "count": len(items) or None,
            "number": numbers[0] if numbers else None, "topic": _topic(post) or None}


def candidate_hooks(post: dict) -> list[dict[str, Any]]:
    """Гипотезы хука из данных поста: стратегия, текст и причина отказа, если не вышло."""
    data = slots(post)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for code in STRATEGY_ORDER:
        strategy = STRATEGIES[code]
        need = strategy["needs"]
        value = data.get(need)
        if value in (None, "", 0):
            rows.append({"strategy": code, "hook": None,
                         "skipped": f"в посте нет данных для слота «{need}»"})
            continue
        text = strategy["template"].format(
            number=data.get("number"), count=data.get("count"),
            word=_plural(int(data["count"]), ("пункт", "пункта", "пунктов"))
            if data.get("count") else "",
            title=data.get("title"), topic=data.get("topic") or "",
        ).strip()
        text = re.sub(r"\s{2,}", " ", text)
        key = text.lower()
        if key in seen:
            rows.append({"strategy": code, "hook": None, "skipped": "дубль текста хука"})
            continue
        if len(text.split()) > media_reel.MAX_HOOK_WORDS:
            rows.append({"strategy": code, "hook": None,
                         "skipped": f"хук длиннее {media_reel.MAX_HOOK_WORDS} слов"})
            continue
        if len(text) > media_reel.media.MAX_HEADLINE_CHARS:
            rows.append({"strategy": code, "hook": None,
                         "skipped": f"хук длиннее {media_reel.media.MAX_HEADLINE_CHARS} знаков"})
            continue
        seen.add(key)
        rows.append({"strategy": code, "hook": text, "skipped": None})
    return rows


def _seed(post: dict, brand: dict | None, compliance: dict | None, fmt: str, fps: int,
          count: int) -> str:
    payload = json.dumps({"post": post, "brand": brand or {}, "compliance": compliance or {},
                          "format": fmt, "fps": fps, "count": count},
                         ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def build_lab(post: dict, *, brand: dict | None = None, compliance: dict | None = None,
              fmt: str = media_reel.DEFAULT_FORMAT, fps: int = 30,
              count: int = DEFAULT_VARIANTS, workspace_id: str = "ws_de") -> dict[str, Any]:
    """Построить лабораторию: N вариантов одного ролика с разными первыми кадрами.

    Остальные сцены у всех вариантов совпадают по тексту и длительности — иначе метрика
    досмотра покажет не силу хука, а разницу роликов.
    """
    count = max(MIN_VARIANTS, min(MAX_VARIANTS, int(count or DEFAULT_VARIANTS)))
    lab_id = f"lab_{_seed(post, brand, compliance, fmt, fps, count)}"
    existing = _labs.get(lab_id)
    if existing:
        return {**existing, "reused": True}

    base = media_reel.build_reel_spec(post, brand=brand, compliance=compliance, fps=fps, fmt=fmt)
    proposals = candidate_hooks(post)
    candidates = [row for row in proposals if row["hook"]]
    variants: list[dict[str, Any]] = []
    # отказы стратегий видны в ответе: «почему три, а не пять» — вопрос, который задают всегда
    skipped: list[dict[str, Any]] = [
        {"strategy": row["strategy"], "hook": None,
         "issues": [{"code": "strategy_skipped", "message": row["skipped"]}]}
        for row in proposals if not row["hook"]]
    for index, row in enumerate(candidates[:count]):
        spec = json.loads(json.dumps(base))                 # глубокая копия без общего состояния
        spec["scenes"][0]["props"]["headline"] = row["hook"]
        report = media_reel.check(spec)
        if not report["ok"]:
            skipped.append({"strategy": row["strategy"], "hook": row["hook"],
                            "issues": report["issues"]})
            continue
        variants.append({
            "id": f"var_{index + 1}",
            "strategy": row["strategy"],
            "strategy_title": STRATEGIES[row["strategy"]]["title"],
            "hook": row["hook"],
            "spec": spec,
            "spec_hash": media_reel.spec_hash(spec),
            "duration": spec.get("duration"),
            "metrics": None,
        })
    for row in candidates[count:]:
        skipped.append({"strategy": row["strategy"], "hook": row["hook"],
                        "issues": [{"code": "beyond_count",
                                    "message": f"вариантов просили {count}"}]})

    lab = {
        "id": lab_id, "kind": "hook_lab", "workspace_id": workspace_id,
        "created_at": _now(), "format": fmt, "fps": fps,
        "requested": count, "built": len(variants),
        "variants": variants, "skipped": skipped,
        "status": "ready" if len(variants) >= MIN_VARIANTS else "insufficient_variants",
        "winner_variant_id": None, "decided_at": None, "winner_spec_hash": None,
        "min_views": DEFAULT_MIN_VIEWS, "shared_spec_hash": media_reel.spec_hash(base),
        "base_headline": (base["scenes"][0].get("props") or {}).get("headline"),
    }
    _labs.put(lab)
    return {**lab, "reused": False}


def lab(lab_id: str) -> dict | None:
    return _labs.get(lab_id)


def labs(limit: int = 50) -> list[dict]:
    rows = sorted(_labs.all(), key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return rows[:limit]


def _issue(code: str, message: str) -> dict[str, Any]:
    return {"code": code, "message": message}


def record_metrics(lab_id: str, variant_id: str, *, views: int, completions: int | None = None,
                   avg_watch_seconds: float | None = None,
                   channel: str | None = None) -> dict[str, Any]:
    """Записать показатели варианта. Источник в проде — аналитика канала, в песочнице — API.

    Считаем **приращениями** и складываем: публикация может отдать статистику несколько раз
    (первый час, сутки, неделя), и «перезапись последним значением» теряла бы историю.
    """
    row = _labs.get(lab_id)
    if not row:
        return {"ok": False, "reason": "лаборатория не найдена", "code": "lab_not_found"}
    variant = next((item for item in row["variants"] if item["id"] == variant_id), None)
    if variant is None:
        return {"ok": False, "reason": f"варианта {variant_id} нет в лаборатории",
                "code": "variant_not_found"}
    if int(views) <= 0:
        return {"ok": False, "reason": "показов должно быть больше нуля", "code": "views_invalid"}
    if completions is not None and not 0 <= int(completions) <= int(views):
        return {"ok": False, "reason": "досмотров не может быть больше показов",
                "code": "completions_invalid"}

    metrics = variant.get("metrics") or {"views": 0, "completions": 0, "watch_seconds": 0.0,
                                         "reports": 0, "channels": []}
    metrics["views"] += int(views)
    if completions is not None:
        metrics["completions"] += int(completions)
    if avg_watch_seconds is not None:
        metrics["watch_seconds"] += float(avg_watch_seconds) * int(views)
    metrics["reports"] += 1
    if channel and channel not in metrics["channels"]:
        metrics["channels"].append(channel)
    variant["metrics"] = metrics
    row["updated_at"] = _now()
    _labs.put(row)
    return {"ok": True, "lab_id": lab_id, "variant_id": variant_id, "metrics": metrics}


def score(variant: dict[str, Any], duration: float | None = None) -> dict[str, Any] | None:
    """Метрика варианта: доля досмотров, иначе — средняя доля просмотра."""
    metrics = variant.get("metrics")
    if not metrics or not metrics.get("views"):
        return None
    views = int(metrics["views"])
    duration = float(duration or 0) or None
    completion = round(metrics.get("completions", 0) / views, 4)
    watch = round(metrics.get("watch_seconds", 0.0) / views, 2)
    # без среднего времени просмотра соотношение было бы нулём у всех и давало ложное равенство
    ratio = round(watch / duration, 4) if duration and metrics.get("watch_seconds") else None
    primary = ratio if ratio is not None else completion
    return {"views": views, "completion_rate": completion, "avg_watch_seconds": watch,
            "watch_ratio": ratio, "metric": primary,
            "metric_name": "watch_ratio" if ratio is not None else "completion_rate"}


def table(lab_id: str, *, min_views: int | None = None) -> dict[str, Any]:
    """Сводка вариантов: метрика по каждому и решение — или причина, почему его нет."""
    row = _labs.get(lab_id)
    if not row:
        return {"ok": False, "reason": "лаборатория не найдена", "code": "lab_not_found"}
    threshold = int(min_views if min_views is not None else row.get("min_views")
                    or DEFAULT_MIN_VIEWS)
    rows: list[dict[str, Any]] = []
    for variant in row["variants"]:
        scored = score(variant, row.get("duration") or variant.get("duration"))
        if scored is None:
            scored = score(variant, variant.get("duration"))
        rows.append({"id": variant["id"], "strategy": variant["strategy"],
                     "strategy_title": variant.get("strategy_title"), "hook": variant["hook"],
                     "spec_hash": variant["spec_hash"],
                     "metrics": variant.get("metrics"), "score": scored})
    measured = [item for item in rows if item["score"]]
    eligible = [item for item in measured if item["score"]["views"] >= threshold]
    decision: dict[str, Any] = {"status": "not_enough_data", "min_views": threshold,
                                "measured": len(measured), "eligible": len(eligible)}
    if len(eligible) >= 2:
        best = max(eligible, key=lambda item: item["score"]["metric"])
        rivals = [item for item in eligible if item["id"] != best["id"]]
        runner = max(rivals, key=lambda item: item["score"]["metric"])
        gap = round(best["score"]["metric"] - runner["score"]["metric"], 4)
        if gap <= TIE_EPSILON:
            decision = {"status": "tie", "min_views": threshold, "gap": gap,
                        "candidates": [best["id"], runner["id"]]}
        else:
            decision = {"status": "winner", "winner_variant_id": best["id"],
                        "winner_hook": best["hook"], "metric": best["score"]["metric"],
                        "metric_name": best["score"]["metric_name"], "gap": gap,
                        "min_views": threshold}
    elif len(eligible) == 1:
        decision = {"status": "not_enough_data", "min_views": threshold,
                    "reason": "порог показов прошёл только один вариант: сравнивать не с чем",
                    "measured": len(measured), "eligible": 1}
    return {"ok": True, "lab_id": lab_id, "requested": row.get("requested"),
            "built": row.get("built"), "status": row.get("status"),
            "winner_variant_id": row.get("winner_variant_id"),
            "decided_at": row.get("decided_at"), "shared_spec_hash": row.get("shared_spec_hash"),
            "rows": rows, "decision": decision}


def decide(lab_id: str, *, min_views: int | None = None) -> dict[str, Any]:
    """Зафиксировать победителя. Без данных — отказ, а не «первый по списку».

    Идемпотентность: если решение уже принято, возвращается тот же вариант и та же спека.
    """
    row = _labs.get(lab_id)
    if not row:
        return {"ok": False, "reason": "лаборатория не найдена", "code": "lab_not_found"}
    if row.get("winner_variant_id"):
        winner = next((item for item in row["variants"]
                       if item["id"] == row["winner_variant_id"]), None)
        return {"ok": True, "reused": True, "lab_id": lab_id,
                "decision": {"status": "winner", "winner_variant_id": row["winner_variant_id"],
                             "winner_hook": (winner or {}).get("hook")},
                "winner": winner}

    summary = table(lab_id, min_views=min_views)
    decision = summary.get("decision") or {}
    if decision.get("status") != "winner":
        return {"ok": False, "code": decision.get("status") or "not_enough_data",
                "reason": {"status": decision.get("status"),
                           "min_views": decision.get("min_views"),
                           "message": "лаборатория не выбирает победителя без накопленных показов"},
                "table": summary}
    winner_id = decision["winner_variant_id"]
    row["winner_variant_id"] = winner_id
    row["decision"] = decision
    row["decided_at"] = _now()
    winner = next(item for item in row["variants"] if item["id"] == winner_id)
    row["winner_spec_hash"] = winner["spec_hash"]
    _labs.put(row)
    return {"ok": True, "reused": False, "lab_id": lab_id, "decision": decision,
            "winner": winner, "spec": winner["spec"]}


def winner_spec(lab_id: str) -> dict | None:
    """Спека победителя: то, что уходит в публикацию после теста."""
    row = _labs.get(lab_id)
    if not row or not row.get("winner_variant_id"):
        return None
    winner = next((item for item in row["variants"]
                   if item["id"] == row["winner_variant_id"]), None)
    return (winner or {}).get("spec")


def summary() -> dict[str, Any]:
    rows = _labs.all()
    decided = [item for item in rows if item.get("winner_variant_id")]
    by_status: dict[str, int] = {}
    for item in rows:
        by_status[item.get("status")] = by_status.get(item.get("status"), 0) + 1
    return {"labs": len(rows), "decided": len(decided), "by_status": by_status,
            "strategies": sorted(STRATEGIES), "variants": [MIN_VARIANTS, MAX_VARIANTS],
            "min_views": DEFAULT_MIN_VIEWS}


def reset() -> None:
    """Очистить лаборатории: нужно тестам и песочнице."""
    _labs.clear_memory()
