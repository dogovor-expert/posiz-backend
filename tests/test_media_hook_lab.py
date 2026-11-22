"""Хук-лаборатория: варианты первых секунд и честное решение (этап F).

Что здесь важно проверить:

1. **Сравнение честное.** Варианты различаются только первым кадром — остальные сцены и
   тайминги одинаковы. Иначе метрика досмотра показывает разницу роликов, а не силу хука.
2. **Хук не выдумывает факты.** Варианты собираются из данных поста (цифра, число пунктов,
   тема, сам заголовок); нет данных — нет варианта, и это видно в `skipped`.
3. **Победитель без данных — не победитель.** Мало показов → `not_enough_data`; равные
   метрики → `tie`, а не случайный выбор.
4. **Решение фиксируется.** Повторный вызов возвращает того же победителя, его спека
   собирается тем же конвейером, что обычный ролик.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from orbita import media_lab, media_reel
from orbita.main import app

client = TestClient(app)
H = {"X-Actor-Role": "owner"}

POST = {"title": "Минус 20% на всё до 30 ноября", "eyebrow": "ТехноЛайн · распродажа",
        "items": ["Бесплатная доставка по РФ", "Гарантия 2 года на технику"],
        "note": "Промокод NOV20 действует на всё, кроме товаров с финальной ценой.",
        "footer": "technoline.ru", "cta": "Забрать скидку"}


@pytest.fixture(autouse=True)
def clean(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_SPOOL_DIR", str(tmp_path / "media"))
    monkeypatch.setenv("MEDIA_STORAGE", "spool")
    monkeypatch.setenv("MEDIA_URL_SECRET", "test-media-url-secret")
    media_lab.reset()
    media_reel._reels.clear_memory()
    media_reel._specs.clear_memory()
    yield
    media_lab.reset()


def _lab(**kwargs) -> dict:
    return media_lab.build_lab(kwargs.pop("post", POST), fps=kwargs.pop("fps", 25), **kwargs)


# ------------------------------------------------------------------ гипотезы
def test_candidates_come_from_post_data():
    rows = {row["strategy"]: row for row in media_lab.candidate_hooks(POST)}
    assert rows["number"]["hook"].startswith("20%")
    assert rows["count"]["hook"] == "2 пункта — коротко и по делу"
    assert rows["direct"]["hook"] == POST["title"]
    assert rows["question"]["hook"].startswith("ТехноЛайн")


def test_missing_data_skips_strategy_instead_of_inventing():
    post = {"title": "Без цифр и пунктов", "footer": "x.ru"}
    rows = {row["strategy"]: row for row in media_lab.candidate_hooks(post)}
    assert rows["number"]["hook"] is None
    assert "слот" in rows["number"]["skipped"]
    assert rows["count"]["hook"] is None
    assert rows["direct"]["hook"] == "Без цифр и пунктов"


def test_hooks_are_unique_and_within_limits():
    hooks = [row["hook"] for row in media_lab.candidate_hooks(POST) if row["hook"]]
    assert len(hooks) == len(set(hooks))
    for hook in hooks:
        assert len(hook.split()) <= media_reel.MAX_HOOK_WORDS
        assert len(hook) <= media_reel.media.MAX_HEADLINE_CHARS


def test_plural_is_correct():
    assert media_lab._plural(1, ("пункт", "пункта", "пунктов")) == "пункт"
    assert media_lab._plural(3, ("пункт", "пункта", "пунктов")) == "пункта"
    assert media_lab._plural(5, ("пункт", "пункта", "пунктов")) == "пунктов"
    assert media_lab._plural(11, ("пункт", "пункта", "пунктов")) == "пунктов"


# ------------------------------------------------------------------ лаборатория
def test_lab_builds_requested_variants():
    lab = _lab(count=3)
    assert lab["built"] == 3 and lab["status"] == "ready"
    assert [variant["id"] for variant in lab["variants"]] == ["var_1", "var_2", "var_3"]


def test_variants_differ_only_in_first_scene():
    lab = _lab(count=3)
    heads = {variant["spec"]["scenes"][0]["props"]["headline"] for variant in lab["variants"]}
    assert len(heads) == 3
    tails = {tuple(str(scene) for scene in variant["spec"]["scenes"][1:])
             for variant in lab["variants"]}
    assert len(tails) == 1, "у вариантов различаются не только первые секунды"


def test_every_variant_is_a_valid_reel():
    for variant in _lab(count=3)["variants"]:
        assert media_reel.check(variant["spec"])["ok"] is True


def test_variants_have_distinct_hashes():
    hashes = {variant["spec_hash"] for variant in _lab(count=3)["variants"]}
    assert len(hashes) == 3


def test_lab_is_idempotent():
    first = _lab(count=3)
    second = _lab(count=3)
    assert second["reused"] is True and second["id"] == first["id"]
    assert len(second["variants"]) == len(first["variants"])


def test_lab_respects_count_bounds():
    """Просили 99 — получили максимум 5 и столько, сколько дал материал поста."""
    lab = _lab(count=99)
    assert lab["requested"] == media_lab.MAX_VARIANTS
    assert 2 <= lab["built"] <= media_lab.MAX_VARIANTS


def test_skipped_reports_why():
    """Стратегия без данных попадает в skipped с причиной, а не молча исчезает."""
    post = {"title": "Скидка", "items": ["Один пункт"], "footer": "x.ru", "eyebrow": "Витрина"}
    lab = media_lab.build_lab(post, fps=25, count=3)
    reasons = {item["strategy"] for item in lab["skipped"]}
    assert "number" in reasons, lab["skipped"]
    assert lab["built"] >= 2


def test_insufficient_variants_status():
    """Голый заголовок даёт один вариант — лаборатория честно говорит, что сравнивать нечего."""
    post = {"title": "Коротко", "footer": "x.ru"}
    lab = media_lab.build_lab(post, fps=25, count=3)
    assert lab["built"] == 1
    assert lab["status"] == "insufficient_variants"
    assert media_lab.decide(lab["id"])["ok"] is False


# ------------------------------------------------------------------ метрики и решение
def test_metrics_accumulate_not_overwrite():
    lab = _lab(count=3)
    media_lab.record_metrics(lab["id"], "var_1", views=100, completions=20, avg_watch_seconds=3.0)
    media_lab.record_metrics(lab["id"], "var_1", views=50, completions=10, avg_watch_seconds=4.0)
    variant = media_lab.lab(lab["id"])["variants"][0]
    assert variant["metrics"]["views"] == 150
    assert variant["metrics"]["completions"] == 30
    assert variant["metrics"]["reports"] == 2
    # среднее время просмотра усредняется по показам, а не по отчётам
    assert variant["metrics"]["watch_seconds"] == pytest.approx(100 * 3.0 + 50 * 4.0)


def test_metrics_validation_is_honest():
    lab = _lab(count=3)
    assert media_lab.record_metrics(lab["id"], "var_1", views=0)["code"] == "views_invalid"
    assert media_lab.record_metrics(lab["id"], "var_1", views=10,
                                    completions=11)["code"] == "completions_invalid"
    assert media_lab.record_metrics(lab["id"], "var_9", views=10)["code"] == "variant_not_found"
    assert media_lab.record_metrics("lab_none", "var_1", views=10)["code"] == "lab_not_found"


def test_no_decision_without_data():
    lab = _lab(count=3)
    result = media_lab.decide(lab["id"])
    assert result["ok"] is False and result["code"] == "not_enough_data"
    assert media_lab.winner_spec(lab["id"]) is None


def test_no_decision_with_single_eligible_variant():
    lab = _lab(count=3)
    media_lab.record_metrics(lab["id"], "var_1", views=500, avg_watch_seconds=6.0)
    media_lab.record_metrics(lab["id"], "var_2", views=20, avg_watch_seconds=9.0)
    decision = media_lab.table(lab["id"])["decision"]
    assert decision["status"] == "not_enough_data"
    assert "сравнивать не с чем" in decision["reason"]


def test_winner_is_chosen_by_watch_ratio():
    lab = _lab(count=3)
    media_lab.record_metrics(lab["id"], "var_1", views=200, avg_watch_seconds=3.0)
    media_lab.record_metrics(lab["id"], "var_2", views=200, avg_watch_seconds=9.0)
    media_lab.record_metrics(lab["id"], "var_3", views=200, avg_watch_seconds=5.0)
    decision = media_lab.table(lab["id"])["decision"]
    assert decision["status"] == "winner"
    assert decision["winner_variant_id"] == "var_2"
    assert decision["metric_name"] == "watch_ratio"


def test_completion_rate_is_used_when_watch_time_missing():
    lab = _lab(count=3)
    media_lab.record_metrics(lab["id"], "var_1", views=200, completions=40)
    media_lab.record_metrics(lab["id"], "var_2", views=200, completions=90)
    decision = media_lab.table(lab["id"])["decision"]
    assert decision["status"] == "winner" and decision["winner_variant_id"] == "var_2"
    assert decision["metric_name"] == "completion_rate"


def test_equal_metrics_are_a_tie_not_a_winner():
    lab = _lab(count=3)
    for variant in ("var_1", "var_2"):
        media_lab.record_metrics(lab["id"], variant, views=300, avg_watch_seconds=6.0)
    decision = media_lab.table(lab["id"])["decision"]
    assert decision["status"] == "tie"
    assert set(decision["candidates"]) == {"var_1", "var_2"}
    assert media_lab.decide(lab["id"])["code"] == "tie"


def test_decision_is_idempotent_and_returns_spec():
    lab = _lab(count=3)
    media_lab.record_metrics(lab["id"], "var_1", views=300, avg_watch_seconds=3.0)
    media_lab.record_metrics(lab["id"], "var_2", views=300, avg_watch_seconds=9.0)
    first = media_lab.decide(lab["id"])
    second = media_lab.decide(lab["id"])
    assert first["ok"] is True and second["reused"] is True
    assert second["decision"]["winner_variant_id"] == first["decision"]["winner_variant_id"]
    assert media_lab.winner_spec(lab["id"])["scenes"][0]["props"]["headline"] == \
        first["winner"]["hook"]


def test_threshold_can_be_lowered_explicitly():
    lab = _lab(count=3)
    media_lab.record_metrics(lab["id"], "var_1", views=40, avg_watch_seconds=3.0)
    media_lab.record_metrics(lab["id"], "var_2", views=40, avg_watch_seconds=9.0)
    assert media_lab.table(lab["id"])["decision"]["status"] == "not_enough_data"
    assert media_lab.table(lab["id"], min_views=20)["decision"]["status"] == "winner"


# ------------------------------------------------------------------ API
def test_api_creates_lab_and_exposes_table():
    response = client.post("/api/v1/media/hooks", headers=H, json={"post": POST, "count": 3})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["built"] == 3
    assert body["table"]["decision"]["status"] == "not_enough_data"
    listed = client.get("/api/v1/media/hooks", headers=H).json()
    assert listed["summary"]["labs"] >= 1


def test_api_metrics_then_decision_then_reel():
    lab_id = client.post("/api/v1/media/hooks", headers=H,
                         json={"post": POST, "count": 3}).json()["lab_id"]
    refused = client.post(f"/api/v1/media/hooks/{lab_id}/decide", headers=H)
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "not_enough_data"

    for variant, watch in (("var_1", 3.0), ("var_2", 9.0)):
        ok = client.post(f"/api/v1/media/hooks/{lab_id}/metrics", headers=H,
                         json={"variant_id": variant, "views": 200,
                               "avg_watch_seconds": watch, "channel": "vk"})
        assert ok.status_code == 200, ok.text

    decided = client.post(f"/api/v1/media/hooks/{lab_id}/decide", headers=H)
    assert decided.status_code == 200
    assert decided.json()["decision"]["winner_variant_id"] == "var_2"

    reel = client.post(f"/api/v1/media/hooks/{lab_id}/reel?render=false", headers=H)
    assert reel.status_code == 200
    payload = reel.json()
    assert payload["reel"]["status"] in {"planned", "accepted"}
    assert payload["spec"]["scenes"][0]["props"]["headline"].startswith("2 пункта")


def test_api_reel_without_winner_is_refused():
    lab_id = client.post("/api/v1/media/hooks", headers=H,
                         json={"post": POST, "count": 3}).json()["lab_id"]
    response = client.post(f"/api/v1/media/hooks/{lab_id}/reel", headers=H)
    assert response.status_code == 409
    assert response.json()["detail"]["kind"] == "hook_not_decided"


def test_api_rejects_bad_metric():
    lab_id = client.post("/api/v1/media/hooks", headers=H,
                         json={"post": POST, "count": 3}).json()["lab_id"]
    response = client.post(f"/api/v1/media/hooks/{lab_id}/metrics", headers=H,
                           json={"variant_id": "var_9", "views": 10})
    assert response.status_code == 409
    assert response.json()["detail"]["kind"] == "variant_not_found"


def test_api_404_for_unknown_lab():
    assert client.get("/api/v1/media/hooks/lab_nope", headers=H).status_code == 404
    assert client.post("/api/v1/media/hooks/lab_nope/decide", headers=H).status_code == 404


def test_api_summary_reports_hook_labs():
    client.post("/api/v1/media/hooks", headers=H, json={"post": POST, "count": 3})
    summary = client.get("/api/v1/media/summary", headers=H).json()
    assert summary["hooks"]["labs"] == 1
    assert "count" in summary["hooks"]["strategies"]
