"""A/B-тестирование заголовков и обложек.

Логика: публикуем вариант A, через N минут сверяем CTR/вовлечённость,
при разнице выше порога обновляем пост на победивший через метод edit адаптера.
В каркасе оценка детерминированная (seed от id), в проде — метрики канала.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

from .docs import Collection

TESTS: dict[str, dict] = {}          # кэш процесса
TESTS_DOCS = Collection("ab_tests", id_field="id")
THRESHOLD = 0.15     # минимальная относительная разница CTR
MIN_IMPRESSIONS = 200


def create(publication_id: str, headline_a: str, headline_b: str, channel: str = "telegram",
           check_after_minutes: int = 120, metric: str = "ctr") -> dict:
    tid = f"ab_{len(TESTS) + 1}"
    test = {
        "id": tid, "publication_id": publication_id, "channel": channel, "metric": metric,
        "variants": {"a": {"headline": headline_a, "impressions": 0, "clicks": 0, "ctr": 0.0},
                     "b": {"headline": headline_b, "impressions": 0, "clicks": 0, "ctr": 0.0}},
        "status": "running", "created_at": datetime.now(timezone.utc),
        "check_after": datetime.now(timezone.utc) + timedelta(minutes=check_after_minutes),
        "min_impressions": MIN_IMPRESSIONS, "threshold": THRESHOLD, "winner": None, "edit_applied": False,
    }
    TESTS[tid] = test
    return TESTS_DOCS.put(test)


def list_tests() -> list[dict]:
    return TESTS_DOCS.all() or list(TESTS.values())


def _simulate(test: dict) -> None:
    """Детерминированные «живые» метрики: в проде приходят из collect_metrics."""
    seed = int(hashlib.sha256(test["id"].encode()).hexdigest(), 16)
    base = 400 + seed % 600
    for key, off in (("a", 0), ("b", 137)):
        v = test["variants"][key]
        if v["impressions"]:
            continue
        impressions = base + (seed + off) % 300
        ctr = 0.035 + ((seed + off) % 40) / 1000     # 3,5–7,5 %
        v.update(impressions=impressions, clicks=round(impressions * ctr), ctr=round(ctr, 4))


def evaluate(test_id: str, force: bool = False) -> dict:
    test = TESTS_DOCS.get(test_id) or TESTS.get(test_id)
    if not test:
        return {"error": "тест не найден"}
    _simulate(test)
    a, b = test["variants"]["a"], test["variants"]["b"]
    if min(a["impressions"], b["impressions"]) < MIN_IMPRESSIONS and not force:
        return {**test, "status": "collecting",
                "note": f"мало показов (нужно {MIN_IMPRESSIONS} на вариант)"}

    diff = (b["ctr"] - a["ctr"]) / max(a["ctr"], 1e-9)
    winner = None
    if abs(diff) >= THRESHOLD:
        winner = "b" if diff > 0 else "a"
    test["status"] = "decided" if winner else "no_difference"
    test["winner"] = winner
    test["decided_at"] = datetime.now(timezone.utc)
    test["relative_diff"] = round(diff, 3)
    if winner:
        # в проде: adapter.edit(publication_id, headline=winner_headline)
        test["edit_applied"] = True
        test["edit_call"] = {"method": "edit", "publication_id": test["publication_id"],
                             "headline": test["variants"][winner]["headline"]}
    return test


def apply(test_id: str) -> dict:
    test = TESTS_DOCS.get(test_id) or TESTS.get(test_id)
    if not test or not test.get("winner"):
        return {"error": "у теста нет победителя"}
    test["edit_applied"] = True
    return {"test_id": test_id, "applied": test["winner"],
            "headline": test["variants"][test["winner"]]["headline"]}
