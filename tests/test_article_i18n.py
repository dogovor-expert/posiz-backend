"""Мультиязычная статья: перевод данными, «хром» кодом, `hreflang` в артефакте (этап I7).

Что проверяется — и почему именно это:

1. **Движок не переводит.** Перевод приходит `translations`; блок без перевода попадает в
   `missing` с адресом, а не остаётся русским посреди английской страницы.
2. **Строгий режим — отказ.** `strict=True` при неполном переводе даёт `translation_missing`
   со списком полей; нестрогий сохраняет черновик с `complete: false`.
3. **«Хром» переводит код.** Подписи («Read next», «source», «min read»), время чтения по
   профилю языка и `lang` — из реестра, а не из переводчика.
4. **Темп чтения статьи — свой.** Профиль ролика меряет слова в секунду с экрана (2.6),
   статья — слова в минуту (180): иначе «5 минут» превращаются в 8.
5. **`hreflang` полон.** Ссылка на себя обязательна (`hreflang_self_missing`), `x-default` тоже
   (`hreflang_default_missing`), неизвестный язык — `locale_unknown`, а не тихий откат на ru.
6. **Лимит заголовка зависит от языка.** 60 знаков у `en` против 70 у `ru` — заголовок, который
   влезает по-русски, обрезается в английской выдаче.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from orbita import article_i18n, articles
from orbita.main import app

client = TestClient(app)
H = {"X-Actor-Role": "owner"}

TEXT = ("Отчёт по охватам нужен для решения, а не для отчётности: смотрим охваты, переходы и "
        "цену действия. Без цены действия канал нельзя ни оставить, ни закрыть. ") * 8

EN_TEXT = ("The reach report exists for decisions, not for reporting: reach, clicks and cost per "
           "action. Without cost per action a channel can be neither kept nor closed. ") * 8


@pytest.fixture(autouse=True)
def _clean():
    articles.reset()
    yield
    articles.reset()


def _spec(**extra) -> dict:
    brief = {"topic": "Как читать отчёт по охватам", "keywords": ["отчёт по охватам"],
             "sections": 3,
             "lead": "Отчёт по охватам нужен для решений, а не для отчётности.",
             "outline": [{"heading": head, "text": TEXT}
                         for head in ("Охваты и переходы", "Цена действия", "Что дальше")],
             "faq": [{"q": "Как часто нужен отчёт?", "a": "Раз в неделю — чаще шум."}],
             **extra}
    result = articles.compose(brief)
    assert result["ok"], result
    return result["spec"]


def _translations(spec: dict) -> dict:
    """Полный перевод: заголовок, описание, все текстовые поля блоков."""
    blocks: dict[str, dict] = {}
    for block in spec["blocks"]:
        props = block.get("props") or {}
        given: dict = {}
        if props.get("text"):
            given["text"] = EN_TEXT if block["type"] == "paragraph" else "English text"
        if props.get("items") and block["type"] == "list":
            given["items"] = ["First item", "Second item"]
        if props.get("items") and block["type"] == "faq":
            given["items"] = [{"q": "How often?", "a": "Once a week."}]
        if props.get("caption"):
            given["caption"] = "English caption"
        if props.get("alt"):
            given["alt"] = "English alt"
        if given:
            blocks[block["id"]] = given
    return {"seo": {"title": "How to read a reach report", "description": "Reach, clicks, cost.",
                    "keywords": ["reach report"]},
            "blocks": blocks, "cta": {"text": "Open your report"}}


# ------------------------------------------------------------------ профили
def test_catalog_has_article_pace_and_ui_strings():
    rows = {row["code"]: row for row in article_i18n.catalog()}
    assert set(rows) == {"ru", "en", "kk", "uz"}
    assert rows["en"]["words_per_minute"] > rows["ru"]["words_per_minute"]
    assert rows["en"]["title_limit"] < rows["ru"]["title_limit"]
    assert "related" in rows["en"]["ui"]
    assert article_i18n.ui_strings("en")["related"] == "Read next"


def test_reading_time_uses_article_pace_not_subtitle_pace():
    spec = _spec()
    ru = article_i18n.reading(spec, "ru")
    en = article_i18n.reading(spec, "en")
    assert ru["words_per_minute"] == 180
    assert en["words_per_minute"] == 220
    assert en["minutes"] <= ru["minutes"]
    # по темпу титров (2.6 слова/с ≈ 156 в минуту) время было бы больше
    naive = round(ru["words"] / (2.6 * 60))
    assert ru["minutes"] <= naive


# ------------------------------------------------------------------ перевод
def test_translation_complete_switches_locale_and_chrome():
    spec = _spec()
    result = article_i18n.translate(spec, "en", translations=_translations(spec),
                                    article_id="art_ru")
    assert result["ok"] is True
    assert result["partial"] is False
    local = result["spec"]
    assert local["locale"] == "en"
    assert local["translation_of"] == "art_ru"
    assert local["translation"]["complete"] is True
    assert local["labels"]["related"] == "Read next"
    assert local["seo"]["title"] == "How to read a reach report"
    html = articles.render_html(local)
    assert '<html lang="en">' in html
    assert "min read" in html
    assert "мин чтения" not in html
    # исходная спека не изменилась: перевод — копия, а не правка на месте
    assert spec["locale"] == "ru"
    assert spec["seo"]["title"] != local["seo"]["title"]


def test_missing_translation_is_named_and_strict_refuses():
    spec = _spec()
    partial = _translations(spec)
    dropped = next(block["id"] for block in spec["blocks"]
                   if block["type"] == "paragraph" and block["id"] in partial["blocks"])
    partial["blocks"].pop(dropped)
    refused = article_i18n.translate(spec, "en", translations=partial, strict=True)
    assert refused["ok"] is False
    assert refused["code"] == "translation_missing"
    assert any(row["field"].startswith(f"blocks[{dropped}]") for row in refused["missing"])
    assert refused["spec"] is spec

    draft = article_i18n.translate(spec, "en", translations=partial, strict=False)
    assert draft["ok"] is True
    assert draft["partial"] is True
    assert draft["spec"]["translation"]["complete"] is False
    codes = {issue["code"] for issue in articles.check(draft["spec"])["issues"]}
    assert "translation_missing" in codes


def test_unknown_locale_is_refused_not_fallen_back():
    spec = _spec()
    result = article_i18n.translate(spec, "de", translations={})
    assert result["ok"] is False
    assert result["code"] == "locale_unknown"
    assert "не поддержан" in result["reason"]


def test_same_locale_is_not_a_translation():
    spec = _spec()
    result = article_i18n.translate(spec, "ru", translations={})
    assert result["ok"] is False
    assert result["code"] == "already_localized"


def test_title_limit_depends_on_locale():
    spec = _spec()
    long_en = _translations(spec)
    long_en["seo"]["title"] = "A" * 65          # 65 ≤ 70 у ru, но > 60 у en
    result = article_i18n.translate(spec, "en", translations=long_en, strict=False)
    assert result["ok"] is True
    assert any(row["code"] == "title_too_long_locale" for row in result["issues"])
    codes = {issue["code"] for issue in articles.check(result["spec"])["issues"]}
    assert "title_too_long_locale" in codes


# ------------------------------------------------------------------ hreflang
def test_hreflang_requires_self_and_x_default():
    spec = _spec()
    urls = {"ru": "https://posiz.ru/blog/ru/", "en": "https://posiz.ru/blog/en/"}
    group = article_i18n.hreflang(spec, urls)
    assert group["ok"] is True
    rows = {row["hreflang"]: row for row in group["rows"]}
    assert rows["ru"]["self"] is True
    assert rows["en"]["self"] is False
    assert rows["x-default"]["href"] == urls["ru"]
    only_other = article_i18n.hreflang(spec, {"en": "https://posiz.ru/blog/en/"})
    assert only_other["ok"] is False
    assert only_other["code"] == "hreflang_self_missing"


def test_hreflang_default_and_unknown_locale():
    spec = _spec()
    no_default = article_i18n.hreflang(spec, {"ru": "/blog/ru/", "en": "/blog/en/"}, default="kk")
    assert no_default["ok"] is False
    assert no_default["code"] == "hreflang_default_missing"
    unknown = article_i18n.hreflang(spec, {"ru": "/blog/ru/", "de": "/blog/de/"})
    assert unknown["ok"] is False
    assert unknown["code"] == "locale_unknown"


def test_hreflang_links_are_rendered_and_checked():
    spec = _spec()
    localized = article_i18n.translate(spec, "en", translations=_translations(spec),
                                       article_id="art_ru")["spec"]
    group = article_i18n.hreflang(localized, {"ru": "https://posiz.ru/blog/ru/",
                                             "en": "https://posiz.ru/blog/en/"})
    localized["hreflang"] = group["rows"]
    html = articles.render_html(localized)
    assert html.count('rel="alternate"') == 3          # ru, en, x-default
    assert 'hreflang="x-default"' in html
    codes = {issue["code"] for issue in articles.check(localized)["issues"]}
    assert "hreflang_self_missing" not in codes, codes

    poisoned = {**localized, "hreflang": [row for row in group["rows"]
                                         if row["hreflang"] != "en"]}
    codes = {issue["code"] for issue in articles.check(poisoned)["issues"]}
    assert {"hreflang_self_missing", "hreflang_default_missing"} <= codes or \
        "hreflang_self_missing" in codes


# ------------------------------------------------------------------ матрица
def test_matrix_translates_complete_and_reports_partial():
    spec = _spec()
    result = article_i18n.matrix(spec, ["en", "kk"],
                                 translations={"en": _translations(spec)},
                                 urls={"ru": "/blog/ru/", "en": "/blog/en/", "kk": "/blog/kk/"},
                                 article_id="art_ru")
    rows = {row["locale"]: row for row in result["rows"]}
    assert rows["en"]["ok"] is True and rows["en"]["missing"] == 0
    assert rows["kk"]["ok"] is False and rows["kk"]["missing"] > 0
    assert result["complete"] == ["en"]
    assert result["hreflang"]["ok"] is True
    assert "en" in result["specs"] and "kk" not in result["specs"]


# ------------------------------------------------------------------ API
def test_api_locales_and_translation_flow():
    spec = _spec()
    published = articles.publish(spec)["article"]
    locales = client.get("/api/v1/articles/locales", headers=H)
    assert locales.status_code == 200
    assert any(row["code"] == "en" for row in locales.json()["locales"])

    refused = client.post(f"/api/v1/articles/{published['id']}/translations",
                          json={"locale": "en", "translations": {}}, headers=H)
    assert refused.status_code == 422
    detail = refused.json()["detail"]
    assert detail["kind"] == "translation_missing"
    assert detail["missing"]

    made = client.post(f"/api/v1/articles/{published['id']}/translations",
                       json={"locale": "en", "translations": _translations(spec),
                             "urls": {"ru": "https://posiz.ru/blog/ru/",
                                      "en": "https://posiz.ru/blog/en/"}}, headers=H)
    assert made.status_code == 201, made.text
    body = made.json()
    assert body["complete"] is True
    assert body["locale"] == "en"
    assert body["reading"]["words_per_minute"] == 220
    assert any(row["hreflang"] == "x-default" for row in body["hreflang"])

    listing = client.get(f"/api/v1/articles/{published['id']}/translations", headers=H)
    assert listing.status_code == 200
    payload = listing.json()
    assert payload["translations"][0]["locale"] == "en"
    assert payload["hreflang"]["ok"] is True
    assert payload["urls"]["en"] == "https://posiz.ru/blog/en/"


def test_api_unknown_locale_and_single_locale_group():
    published = articles.publish(_spec())["article"]
    unknown = client.post(f"/api/v1/articles/{published['id']}/translations",
                          json={"locale": "de", "translations": {}}, headers=H)
    assert unknown.status_code == 422
    assert unknown.json()["detail"]["kind"] == "locale_unknown"
    listing = client.get(f"/api/v1/articles/{published['id']}/translations", headers=H).json()
    assert listing["translations"] == []
    assert listing["hreflang"]["code"] == "single_locale"
