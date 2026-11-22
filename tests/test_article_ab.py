"""A/B заголовка и первого абзаца на одном теле статьи (этап I6).

Что проверяется — и почему именно это:

1. **Тело статьи одно.** У всех вариантов один `body_hash`; если тело статьи изменилось после
   старта теста, применение отказывает (`body_changed`): иначе «победил заголовок» означало бы
   «победила другая статья».
2. **Варианты проходят правила.** Заголовок длиннее 70 знаков, потерянное ключевое слово и
   запрещённое паком обещание отбрасываются с кодом, а не молча.
3. **Разбиение стабильное.** Один посетитель всегда видит один вариант, разные посетители
   распределяются по всем вариантам.
4. **Решение — по статистике.** Пока показов меньше порога `not_enough_data`, разница в пределах
   шума — `no_difference`, иначе победитель с `p_value` и `uplift`.
5. **Применение меняет только заголовок и вводку**, версия растёт, тело остаётся прежним, а
   повторное применение — `already_applied`.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from orbita import article_ab as ab, articles
from orbita.main import app

client = TestClient(app)
H = {"X-Actor-Role": "owner"}

TEXT = ("Отчёт по охватам нужен для решения, а не для отчётности: смотрим охваты, переходы и "
        "цену действия. Без цены действия канал нельзя ни оставить, ни закрыть. ") * 8


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in ("ARTICLE_AB_MIN_IMPRESSIONS", "ARTICLE_AB_ALPHA"):
        monkeypatch.delenv(name, raising=False)
    articles.reset()
    ab.reset()
    yield
    articles.reset()
    ab.reset()


def _spec(topic: str = "Как читать отчёт по охватам", keyword: str = "отчёт по охватам",
          **extra) -> dict:
    brief = {"topic": topic, "keywords": [keyword], "sections": 3,
             "outline": [{"heading": head, "text": TEXT}
                         for head in ("Охваты и переходы", "Цена действия", "Что дальше")],
             **extra}
    result = articles.compose(brief)
    assert result["ok"], result
    return result["spec"]


def _fill(test_id: str, rows: dict[str, tuple[int, int]]) -> None:
    for code, (impressions, clicks) in rows.items():
        assert ab.record(test_id, code, impressions=impressions, clicks=clicks)["ok"]


# ------------------------------------------------------------------ варианты
def test_variants_keep_body_and_differ_only_in_title_and_lead():
    spec = _spec()
    made = ab.headline_variants(spec, count=3)
    assert made["ok"] is True
    titles = {variant["title"] for variant in made["variants"]}
    assert len(titles) == len(made["variants"])
    assert all(variant["title_length"] <= ab.MAX_TITLE for variant in made["variants"])
    assert all("отчёт по охватам" in variant["title"].lower() for variant in made["variants"])
    test = ab.create(spec, count=3, article_id="art_x")["test"]
    assert {variant["code"] for variant in test["variants"]} == set(
        [variant["code"] for variant in made["variants"]])
    assert test["body_hash"] == ab._body_hash(spec)


def test_long_title_is_skipped_with_reason(monkeypatch):
    spec = _spec()
    # подменяем формулу на заведомо длинную: правило должно отбросить вариант, а не обрезать
    long_kind = {"code": "long", "template": "{Keyword} — очень подробный разбор по шагам, "
                                             "примерам и цифрам, который не влезает в лимит",
                 "hypothesis": "проверяем правило длины"}
    monkeypatch.setattr(ab, "HEADLINE_KINDS", (long_kind,) + ab.HEADLINE_KINDS)
    made = ab.headline_variants(spec, count=4)
    codes = {row["code"] for row in made["skipped"]}
    assert "title_too_long" in codes
    assert all(variant["title_length"] <= ab.MAX_TITLE for variant in made["variants"])


def test_lost_keyword_is_skipped():
    spec = _spec()
    lost_kind = {"code": "lost", "template": "Разбор без ключевого слова",
                 "hypothesis": "проверяем правило ключа"}
    original = ab.HEADLINE_KINDS
    try:
        ab.HEADLINE_KINDS = (lost_kind,) + original
        made = ab.headline_variants(spec, count=4)
    finally:
        ab.HEADLINE_KINDS = original
    assert any(row["code"] == "keyword_lost" for row in made["skipped"])


def test_banned_phrase_from_pack_is_skipped():
    spec = _spec(rule_pack="high-stakes")
    assert spec["policy"].get("banned_phrases"), spec["policy"]
    phrase = spec["policy"]["banned_phrases"][0]
    risky = {"code": "risky", "template": "{Keyword}: " + phrase,
             "hypothesis": "проверяем запрещённое обещание"}
    original = ab.HEADLINE_KINDS
    try:
        ab.HEADLINE_KINDS = (risky,) + original
        made = ab.headline_variants(spec, count=4)
    finally:
        ab.HEADLINE_KINDS = original
    assert any(row["code"] == "banned_phrase" for row in made["skipped"])


def test_single_surviving_variant_is_not_a_test():
    spec = _spec()
    made = ab.headline_variants(spec, count=2, kinds=["question"])
    assert made["ok"] is False
    assert made["code"] == "no_variants"


# ------------------------------------------------------------------ разбиение
def test_assignment_is_stable_per_visitor():
    spec = _spec()
    test = ab.create(spec, count=3, article_id="art_x")["test"]
    first = ab.assign(test["id"], "reader-42")
    again = ab.assign(test["id"], "reader-42")
    assert first["code"] == again["code"]
    assert first["bucket"] == again["bucket"]


def test_assignment_spreads_visitors_across_variants():
    spec = _spec()
    test = ab.create(spec, count=3, article_id="art_x")["test"]
    seen: dict[str, int] = {}
    for index in range(300):
        code = ab.assign(test["id"], f"reader-{index}")["code"]
        seen[code] = seen.get(code, 0) + 1
    assert len(seen) == len(test["variants"])
    assert min(seen.values()) > 40, seen


def test_assignment_needs_visitor():
    spec = _spec()
    test = ab.create(spec, count=3, article_id="art_x")["test"]
    result = ab.assign(test["id"], "")
    assert result["ok"] is False
    assert result["code"] == "visitor_required"


# ------------------------------------------------------------------ метрики и решение
def test_metrics_are_summed_and_validated():
    spec = _spec()
    test = ab.create(spec, count=3, article_id="art_x")["test"]
    code = test["variants"][0]["code"]
    ab.record(test["id"], code, impressions=100, clicks=10)
    ab.record(test["id"], code, impressions=100, clicks=15)
    stored = ab.test(test["id"])["variants"][0]
    assert stored["impressions"] == 200
    assert stored["clicks"] == 25
    assert stored["ctr"] == 0.125
    assert ab.record(test["id"], code, impressions=10, clicks=99)["code"] == "invalid_metrics"
    assert ab.record(test["id"], "nope", impressions=1, clicks=0)["code"] == "variant_unknown"


def test_evaluate_waits_for_data(monkeypatch):
    monkeypatch.setenv("ARTICLE_AB_MIN_IMPRESSIONS", "500")
    spec = _spec()
    test = ab.create(spec, count=3, article_id="art_x")["test"]
    _fill(test["id"], {row["code"]: (100, 5) for row in test["variants"]})
    result = ab.evaluate(test["id"])
    assert result["ok"] is False
    assert result["code"] == "not_enough_data"
    assert result["needed"] == 500
    assert result["waiting"]


def test_evaluate_finds_winner():
    spec = _spec()
    test = ab.create(spec, count=2, article_id="art_x")["test"]
    low, high = test["variants"][0], test["variants"][1]
    _fill(test["id"], {low["code"]: (1000, 30), high["code"]: (1000, 80)})
    result = ab.evaluate(test["id"])
    assert result["ok"] is True
    assert result["winner"] == high["code"]
    assert result["p_value"] < 0.05
    assert result["uplift"] > 0
    assert ab.test(test["id"])["status"] == "decided"


def test_evaluate_calls_noise_noise():
    spec = _spec()
    test = ab.create(spec, count=2, article_id="art_x")["test"]
    low, high = test["variants"][0], test["variants"][1]
    _fill(test["id"], {low["code"]: (1000, 30), high["code"]: (1000, 33)})
    result = ab.evaluate(test["id"])
    assert result["ok"] is False
    assert result["code"] == "no_difference"
    assert result["winner"] is None
    assert "в пределах шума" in result["reason"]


def test_ztest_matches_known_case():
    z, p_value = ab.ztest(80, 1000, 30, 1000)
    assert z > 4
    assert p_value < 0.0001


# ------------------------------------------------------------------ применение
def test_apply_requires_decision():
    spec = _spec()
    test = ab.create(spec, count=2, article_id="art_x")["test"]
    result = ab.apply(test["id"], spec)
    assert result["ok"] is False
    assert result["code"] == "no_winner"


def test_apply_changes_title_and_lead_but_not_body():
    spec = _spec()
    test = ab.create(spec, count=2, article_id="art_x")["test"]
    _fill(test["id"], {test["variants"][0]["code"]: (1000, 20),
                       test["variants"][1]["code"]: (1000, 70)})
    decision = ab.evaluate(test["id"])
    winner = next(row for row in test["variants"] if row["code"] == decision["winner"])
    result = ab.apply(test["id"], spec)
    assert result["ok"] is True
    assert result["spec"]["seo"]["title"] == winner["title"]
    lead = next(block for block in result["spec"]["blocks"] if block["type"] == "lead")
    assert lead["props"]["text"] == winner["lead"]
    assert result["spec"]["version"] == 2
    assert result["spec"]["history"][-1]["action"] == "abtest"
    assert "выиграл по данным" in result["spec"]["history"][-1]["reason"]
    assert ab._body_hash(result["spec"]) == test["body_hash"]
    assert ab.apply(test["id"], result["spec"])["code"] == "already_applied"


def test_apply_refuses_when_article_body_changed():
    spec = _spec()
    test = ab.create(spec, count=2, article_id="art_x")["test"]
    _fill(test["id"], {test["variants"][0]["code"]: (1000, 20),
                       test["variants"][1]["code"]: (1000, 70)})
    ab.evaluate(test["id"])
    changed = {**spec, "blocks": [*spec["blocks"],
                                  {"id": "blk_new", "type": "paragraph",
                                   "props": {"text": "Дописанный абзац."}}]}
    result = ab.apply(test["id"], changed)
    assert result["ok"] is False
    assert result["code"] == "body_changed"


# ------------------------------------------------------------------ API
def _api_article() -> str:
    spec = _spec()
    return articles.publish(spec)["article"]["id"]


def test_api_flow_create_assign_metrics_evaluate_apply():
    article_id = _api_article()
    created = client.post(f"/api/v1/articles/{article_id}/abtests",
                          json={"count": 2}, headers=H)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["status"] == "running"
    test_id = body["test_id"]
    assert len(body["variants"]) == 2
    assigned = client.post(f"/api/v1/articles/abtests/{test_id}/assign",
                           params={"visitor": "reader-7"}, headers=H)
    assert assigned.status_code == 200, assigned.text
    first = assigned.json()
    assert first["body_hash"] == body["body_hash"]
    codes = [row["code"] for row in body["variants"]]
    metrics = client.post(f"/api/v1/articles/abtests/{test_id}/metrics",
                          json={"code": codes[0], "impressions": 1000, "clicks": 20}, headers=H)
    assert metrics.status_code == 200, metrics.text
    client.post(f"/api/v1/articles/abtests/{test_id}/metrics",
                json={"code": codes[1], "impressions": 1000, "clicks": 80}, headers=H)
    evaluate = client.post(f"/api/v1/articles/abtests/{test_id}/evaluate", headers=H)
    assert evaluate.status_code == 200, evaluate.text
    decision = evaluate.json()
    assert decision["winner"] == codes[1]
    applied = client.post(f"/api/v1/articles/abtests/{test_id}/apply", headers=H)
    assert applied.status_code == 200, applied.text
    result = applied.json()
    assert result["version"] == 2
    stored = articles.article(article_id)["spec"]
    assert stored["seo"]["title"] == decision["winner_title"]
    again = client.post(f"/api/v1/articles/abtests/{test_id}/apply", headers=H)
    assert again.status_code == 409
    assert again.json()["detail"]["kind"] == "already_applied"


def test_api_reports_early_stop_and_unknown_test():
    article_id = _api_article()
    test_id = client.post(f"/api/v1/articles/{article_id}/abtests", json={"count": 2},
                          headers=H).json()["test_id"]
    early = client.post(f"/api/v1/articles/abtests/{test_id}/evaluate", headers=H)
    assert early.status_code == 409
    assert early.json()["detail"]["kind"] == "not_enough_data"
    assert early.json()["detail"]["needed"] >= 1
    listing = client.get("/api/v1/articles/abtests", headers=H)
    assert listing.status_code == 200
    assert listing.json()["items"][0]["test_id"] == test_id
    assert any(row["code"] == "question" for row in listing.json()["kinds"])
    missing = client.post("/api/v1/articles/abtests/ab_nope/evaluate", headers=H)
    assert missing.status_code == 404
