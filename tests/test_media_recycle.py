"""Recycle, локализация и видео-отчёт (этап G, ADR 0023).

Что здесь важно проверить — и почему именно это:

1. **«Топ-пост» — число, а не вкус.** Кандидаты сортируются по вовлечённости из аналитики, посты
   без данных и без порога показов уходят в `skipped` с причиной. Если бы порог не работал,
   «1 реакция из 1 показа» побеждала бы кампанию с 5 200 показами.
2. **Ролик объясняет происхождение.** В спеке есть блок `origin`, и он входит в хеш: два recycle
   из разных постов не склеятся в один артефакт.
3. **Язык — данные.** Профиль языка меняет темп чтения и лимиты строки; неизвестный язык — отказ,
   а не тихий откат на русский.
4. **Перевод не ломает кадр.** `localize` пересчитывает тайминги по профилю языка и не пропускает
   текст, который уже не влезает.
5. **Отчёт не выдумывает цифры.** Каждое число в кадре равно сумме из `store`, а пустой период
   даёт `no_data`, а не ролик с нулями.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from orbita import media_locale, media_recycle, media_reel, media_report, store
from orbita.main import app

client = TestClient(app)
H = {"X-Actor-Role": "owner"}
WS = "ws_de"

BIG_BODY = ("Скидка 20% на весь каталог до 30 ноября.\n"
            "• Скидка действует на всё, кроме финальных цен\n"
            "• Бесплатная доставка от 3000 ₽\n"
            "• Промокод NOV20 суммируется с бонусами")


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setenv("MEDIA_URL_SECRET", "test-media-url-secret")
    store.PUBLICATIONS.clear()
    store.METRICS.clear()
    media_reel._reels.clear_memory()
    media_reel._specs.clear_memory()
    yield
    store.PUBLICATIONS.clear()
    store.METRICS.clear()


def _publish(pid: str, title: str, body: str, channel: str, impressions: int,
             reactions: int, clicks: int, *, days_ago: int = 5,
             metrics: bool = True) -> dict:
    """Сид: опубликованный вариант + снимок метрик — как это делает конвейер публикации."""
    variant = store.save_variant("sig_1", channel, "post", title, body, None, False, 0.9, WS)
    published = datetime.now(timezone.utc) - timedelta(days=days_ago)
    store.PUBLICATIONS[pid] = {
        "id": pid, "variant_id": variant["id"], "workspace_id": WS, "channel": channel,
        "status": "published", "scheduled_at": None, "media_ids": [], "is_ad": False,
        "published_at": published.isoformat(timespec="seconds"),
    }
    if metrics:
        store.METRICS.append({"publication_id": pid, "channel": channel,
                              "impressions": impressions, "reactions": reactions,
                              "clicks": clicks, "cost_rub": 0.0})
    return store.PUBLICATIONS[pid]


def _seed_three() -> None:
    _publish("pub_big", "Минус 20% на всё до 30 ноября", BIG_BODY, "vk", 5200, 210, 140)
    _publish("pub_mid", "Как выбрать подрядчика", "Три критерия выбора. Проверьте договор.",
             "max", 800, 12, 3)
    _publish("pub_tiny", "Малый охват", "текст поста", "tg", 40, 3, 0)


# ------------------------------------------------------------------ кандидаты
def test_candidates_rank_by_engagement_not_by_recency():
    _seed_three()
    rows = media_recycle.candidates()["rows"]
    assert [row["publication_id"] for row in rows] == ["pub_big", "pub_mid"]
    assert rows[0]["engagement"] == pytest.approx(350 / 5200, abs=1e-4)


def test_post_below_threshold_is_skipped_with_reason():
    """40 показов при пороге 100: 3 реакции — это шум, а не «лучший пост»."""
    _seed_three()
    skipped = {row["publication_id"]: row["reason"]
               for row in media_recycle.candidates()["skipped"]}
    assert skipped["pub_tiny"].startswith("below_threshold")
    assert "порог" in skipped["pub_tiny"]


def test_post_without_metrics_is_skipped_not_zeroed():
    _publish("pub_none", "Пост без аналитики", "текст", "tg", 0, 0, 0, metrics=False)
    data = media_recycle.candidates()
    assert data["rows"] == []
    assert data["skipped"][0]["reason"].startswith("no_metrics")


def test_unpublished_posts_are_not_candidates():
    variant = store.save_variant("sig_1", "vk", "post", "Черновик", "тело", None, False, 0.9, WS)
    store.PUBLICATIONS["pub_draft"] = {"id": "pub_draft", "variant_id": variant["id"],
                                       "workspace_id": WS, "channel": "vk", "status": "draft"}
    store.METRICS.append({"publication_id": "pub_draft", "channel": "vk",
                          "impressions": 9000, "reactions": 900, "clicks": 90, "cost_rub": 0.0})
    assert media_recycle.candidates()["rows"] == []


def test_channel_filter_and_period_filter_work():
    _seed_three()
    assert [row["publication_id"]
            for row in media_recycle.candidates(channel="max")["rows"]] == ["pub_mid"]
    old = _publish("pub_old", "Старый пост", "тело старого поста", "vk", 5000, 400, 100,
                   days_ago=400)
    assert old["id"] not in [row["publication_id"]
                             for row in media_recycle.candidates(days=180)["rows"]]


def test_threshold_is_configurable():
    _seed_three()
    rows = media_recycle.candidates(min_impressions=10)["rows"]
    assert "pub_tiny" in [row["publication_id"] for row in rows]


# ------------------------------------------------------------------ recycle
def test_recycle_builds_reel_from_top_post_with_origin():
    _seed_three()
    result = media_recycle.recycle()
    assert result["ok"] is True
    origin = result["spec"]["origin"]
    assert origin["kind"] == "recycle" and origin["publication_id"] == "pub_big"
    assert origin["impressions"] == 5200 and origin["why"]
    assert result["checks"]["ok"] is True


def test_recycle_items_come_from_post_text_not_from_blank():
    _seed_three()
    items = media_recycle.recycle()["post"]["items"]
    assert items and all(len(item) <= media_reel.MAX_CHIP_CHARS for item in items)
    assert any("доставка" in item.lower() for item in items)


def test_recycle_refuses_without_candidates():
    result = media_recycle.recycle()
    assert result["ok"] is False and result["code"] == "no_candidates"
    assert "по данным" in result["reason"]


def test_recycle_refuses_explicit_post_below_threshold():
    _seed_three()
    result = media_recycle.recycle(publication_id="pub_tiny")
    assert result["code"] == "below_threshold" and result["impressions"] == 40


def test_recycle_unknown_publication_is_not_guessed():
    result = media_recycle.recycle(publication_id="pub_missing")
    assert result["code"] == "publication_not_found"


def test_recycle_spec_hash_depends_on_origin():
    _seed_three()
    first = media_recycle.recycle(publication_id="pub_big")["spec_hash"]
    second = media_recycle.recycle(publication_id="pub_mid")["spec_hash"]
    assert first != second


def test_recycle_is_deterministic_for_same_post():
    _seed_three()
    assert (media_recycle.recycle()["spec_hash"]
            == media_recycle.recycle()["spec_hash"])


# ------------------------------------------------------------------ языки
def test_locale_profiles_differ_in_reading_pace():
    assert media_locale.words_per_second("en") > media_locale.words_per_second("ru")
    assert media_locale.max_caption_chars("en") >= media_locale.max_caption_chars("ru")
    assert "ru" in [row["code"] for row in media_locale.catalog()]


def test_unknown_locale_is_an_error_not_a_fallback():
    code, profile, issues = media_locale.resolve("fr")
    assert code == "fr" and profile["title"] == "Русский"
    assert issues[0]["code"] == "locale_unknown"


def test_english_spec_reads_faster_than_russian():
    post = {"title": "Скидка 20% на весь каталог до 30 ноября", "eyebrow": "Витрина",
            "items": ["Скидка на всё", "Доставка бесплатно"], "cta": "Забрать скидку"}
    ru = media_reel.build_reel_spec(post, fps=30, locale="ru")
    en = media_reel.build_reel_spec(post, fps=30, locale="en")
    assert ru["locale"] == "ru" and en["locale"] == "en"
    assert en["duration"] < ru["duration"]


def test_spec_with_unknown_locale_fails_validation():
    spec = media_reel.build_reel_spec({"title": "Скидка 20%"}, fps=30)
    spec["locale"] = "fr"
    codes = [issue["code"] for issue in media_reel.validate_reel_spec(spec)]
    assert "locale_unknown" in codes


def test_localize_retimes_and_keeps_structure():
    post = {"title": "Скидка 20% на весь каталог до 30 ноября", "eyebrow": "Витрина · распродажа",
            "items": ["Скидка 20% на каталог", "Бесплатная доставка от 3000 ₽"], "cta": "Забрать скидку"}
    ru = media_reel.build_reel_spec(post, fps=30)
    result = media_reel.localize(ru, locale="en", texts={
        "headline": "20% off storewide until Nov 30", "eyebrow": "Shop · sale",
        "items": ["20% off everything", "Free delivery over $30"], "cta": "Get the deal"})
    assert result["ok"] is True
    local = result["spec"]
    assert local["locale"] == "en" and local["localized_from"] == "ru"
    assert [scene["template"] for scene in local["scenes"]] == [scene["template"]
                                                               for scene in ru["scenes"]]
    assert local["duration"] < ru["duration"]
    assert local["scenes"][0]["props"]["headline"].startswith("20% off")


def test_localize_returns_original_spec_when_translation_does_not_fit():
    post = {"title": "Скидка 20% на весь каталог", "items": ["Скидка на всё"], "cta": "Забрать"}
    ru = media_reel.build_reel_spec(post, fps=30)
    result = media_reel.localize(ru, locale="en", texts={
        "headline": "This is an extremely long translated headline that cannot possibly fit the frame",
        "items": ["Extraordinarily verbose translated bullet that overflows the chip in the frame"]})
    assert result["ok"] is False
    codes = {issue["code"] for issue in result["issues"]}
    assert codes & {"headline_too_long", "hook_too_long", "chip_too_long"}
    assert result["spec"] is ru                      # исходная спека не испорчена


def test_localize_unknown_locale_refuses():
    ru = media_reel.build_reel_spec({"title": "Скидка 20% на всё"}, fps=30)
    result = media_reel.localize(ru, locale="fr", texts={"headline": "x"})
    assert result["ok"] is False and result["issues"][0]["code"] == "locale_unknown"


# ------------------------------------------------------------------ отчёт
def test_report_numbers_equal_store_numbers():
    _seed_three()
    result = media_report.build(days=30)
    assert result["ok"] is True
    data = result["numbers"]
    assert data["impressions"] == sum(row["impressions"] for row in store.METRICS)
    assert data["reactions"] == sum(row["reactions"] for row in store.METRICS)
    assert data["publications"] == 3
    assert data["engagement"] == pytest.approx(368 / 6040, abs=1e-4)


def test_report_refuses_without_publications():
    result = media_report.build(days=30)
    assert result["code"] == "no_data"
    assert "нет опубликованных" in result["reason"]


def test_report_refuses_when_metrics_missing():
    _publish("pub_none", "Пост без аналитики", "текст", "tg", 0, 0, 0, metrics=False)
    result = media_report.build(days=30)
    assert result["code"] == "no_metrics"


def test_report_period_filter_excludes_old_posts():
    _seed_three()
    _publish("pub_old", "Старый пост", "тело", "vk", 1000, 10, 1, days_ago=90)
    assert media_report.build(days=30)["numbers"]["publications"] == 3
    assert media_report.build(days=180)["numbers"]["publications"] == 4


def test_report_headline_uses_real_number_and_locale_format():
    _seed_three()
    ru = media_report.build(days=30, locale="ru")
    en = media_report.build(days=30, locale="en")
    assert ru["post"]["title"].startswith("6 040")
    assert en["post"]["title"].startswith("6,040")
    assert en["spec"]["locale"] == "en"


def test_report_lists_sources_for_every_number():
    _seed_three()
    sources = media_report.build(days=30)["sources"]
    assert {"impressions", "reactions", "clicks", "publications", "engagement"} <= set(sources)
    assert all(value for value in sources.values())


def test_report_spec_is_valid_reel():
    _seed_three()
    result = media_report.build(days=30, fmt="16x9")
    assert result["checks"]["ok"] is True
    assert result["spec"]["format"] == "16x9"
    assert result["spec"]["origin"]["kind"] == "report"


def test_report_shows_top_post_inside_frame():
    """Лучший пост должен быть в кадре: `note` уходит в озвучку, а сцена пунктов титров не рисует."""
    _seed_three()
    result = media_report.build(days=30)
    chips = result["spec"]["scenes"][1]["props"]["items"]
    assert any(item.startswith("Топ:") for item in chips)
    assert all(len(item) <= media_reel.MAX_CHIP_CHARS for item in chips)
    assert any("5 200 показов" in item for item in chips)


def test_report_top_posts_are_ranked_by_impressions():
    _seed_three()
    top = media_report.build(days=30)["numbers"]["top_posts"]
    assert [row["publication_id"] for row in top][:1] == ["pub_big"]


# ------------------------------------------------------------------ API
def test_api_candidates_and_recycle():
    _seed_three()
    listed = client.get("/api/v1/media/recycle/candidates", headers=H)
    assert listed.status_code == 200
    assert listed.json()["counts"]["eligible"] == 2
    created = client.post("/api/v1/media/recycle", headers=H,
                          json={"render": False, "format": "4x5"})
    assert created.status_code == 201
    body = created.json()
    assert body["spec"]["format"] == "4x5"
    assert body["spec"]["origin"]["publication_id"] == "pub_big"


def test_api_recycle_409_with_reasons():
    response = client.post("/api/v1/media/recycle", headers=H, json={"render": False})
    assert response.status_code == 409
    assert response.json()["detail"]["kind"] == "no_candidates"


def test_api_report_201_and_409():
    _seed_three()
    ok = client.post("/api/v1/media/reports", headers=H, json={"days": 30, "render": False})
    assert ok.status_code == 201 and ok.json()["numbers"]["impressions"] == 6040
    store.PUBLICATIONS.clear()
    store.METRICS.clear()
    empty = client.post("/api/v1/media/reports", headers=H, json={"days": 30, "render": False})
    assert empty.status_code == 409 and empty.json()["detail"]["kind"] == "no_data"


def test_api_reel_accepts_locale_and_rejects_unknown():
    ok = client.post("/api/v1/media/reels", headers=H,
                     json={"post": {"title": "Скидка 20% на всё"}, "locale": "en",
                           "render": False})
    assert ok.status_code == 201 and ok.json()["spec"]["locale"] == "en"
    bad = client.post("/api/v1/media/reels", headers=H,
                      json={"post": {"title": "Скидка 20% на всё"}, "locale": "fr",
                            "render": False})
    assert bad.status_code == 422
    assert [issue["code"] for issue in bad.json()["detail"]["issues"]] == ["locale_unknown"]


def test_api_templates_expose_locales_and_summary_counts():
    reels = client.get("/api/v1/media/templates", headers=H).json()["reels"]
    assert "ru" in [row["code"] for row in reels["locales"]]
    _seed_three()
    summary = client.get("/api/v1/media/summary", headers=H).json()
    assert summary["recycle"]["eligible"] == 2
    assert summary["reports"]["ready"] is True
