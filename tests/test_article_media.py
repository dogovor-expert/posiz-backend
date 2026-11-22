"""Галерея и видео в статье: визуалы из медиа-движка, а не «место под картинку» (этап I3).

Что здесь проверяется — и почему именно это:

1. **Галерея — это несколько визуалов с собственным `alt`.** Один элемент галереи ничего не
   объясняет: он превращается в `image`. Каждая плитка рисуется спекой медиа-движка (inline-SVG
   в разметке), поэтому статья не тянет внешние файлы.
2. **Видео в статье — ролик из спеки статьи.** Заголовок раздела, пункты и подпись уходят в
   `media_reel.build_reel_spec`: получается настоящая спека ролика 16:9 с таймлайном и
   сторибордом. В разметку идёт полоса сцен, а не четыре кадра 1080×1920.
3. **Ролик не выходит за лимиты.** `video_too_long` — проверка, а не пожелание: длительность
   сверяется с `media_reel.limits()`, которые читаются из окружения.
4. **Нет ни спеки, ни ссылки — нет блока.** `video_source` ловит блок без источника видео;
   ссылка клиента (`src`) — законный вариант, но своего файла мы не выдумываем.
5. **Данные сильнее слов.** Галерея и ролик выбираются по `gallery` / `video` в разделе
   (`strict_data`), а не по слову «примеры» в тексте.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from orbita import articles, media_reel
from orbita.main import app

client = TestClient(app)
H = {"X-Actor-Role": "owner"}

TEXT = ("Разбираем по существу, сколько стоит размещение и как считать результат, чтобы не "
        "платить за пустой охват. ") * 12

GALLERY = [{"title": "Тёмная тема с акцентом", "caption": "Пост в тёмной теме"},
           {"title": "Светлая карточка", "caption": "Пост в светлой теме"},
           {"title": "Обложка 16:9", "caption": "Обложка для ролика"}]


def _brief(**overrides) -> dict:
    brief = {
        "topic": "Оформление постов: примеры и ролик",
        "keywords": ["оформление постов"],
        "sections": 4,
        "lead": "Оформление постов: примеры, ролик из поста и что из этого работает.",
        "outline": [
            {"heading": "Примеры оформления", "text": TEXT, "gallery": GALLERY},
            {"heading": "Как выглядит ролик из поста", "text": TEXT,
             "video": {"items": ["Заголовок печатается посимвольно",
                                 "Пункты появляются по одному"],
                       "note": "Ролик 16:9 из одного поста"}},
            {"heading": "Сколько это занимает времени", "text": TEXT,
             "columns": ["Этап", "Время"], "rows": [["Пост", "3 минуты"],
                                                    ["Ролик", "12 минут"]],
             "source": "регламент клиента"},
            {"heading": "Что выбрать", "text": TEXT,
             "columns": [{"title": "Только пост", "items": ["быстро", "дешевле"]},
                         {"title": "Пост и ролик", "items": ["шире охват", "нужен монтаж"]}],
             "source": "регламент клиента"},
        ],
        "faq": [{"q": "Сколько роликов в месяц?", "a": "Столько, сколько постов: ролик из поста."}],
    }
    brief.update(overrides)
    return brief


def _spec(**overrides) -> dict:
    result = articles.compose(_brief(**overrides))
    assert result.get("ok"), result
    return result["spec"]


@pytest.fixture(autouse=True)
def _clean():
    articles.reset()
    yield
    articles.reset()


# ---------------------------------------------------------------- галерея
def test_gallery_builds_tiles_with_alt_and_asset():
    spec = _spec()
    gallery = next(block for block in spec["blocks"] if block["type"] == "gallery")
    items = gallery["props"]["items"]
    assert len(items) == 3
    for item in items:
        assert item["alt"] and item["caption"]
        assert item["asset"]["props"], "у плитки должна быть спека карточки"
        assert item["provider"] == "brand-card"
    assert gallery["reason"] and gallery["alt"]


def test_gallery_requires_at_least_two_items():
    brief = _brief()
    brief["outline"][0]["gallery"] = GALLERY[:1]
    spec = articles.compose(brief)["spec"]
    assert "gallery" not in [block["type"] for block in spec["blocks"]]
    # правило `collection` помечено strict_data и без данных не срабатывает вовсе
    assert "collection" not in [rule["code"] for rule in
                                articles._rank_rules({"heading": "Примеры",
                                                      "text": "Несколько примеров рядом"})]


def test_gallery_items_without_alt_are_reported():
    spec = _spec()
    gallery = next(block for block in spec["blocks"] if block["type"] == "gallery")
    gallery["props"]["items"][1]["alt"] = ""
    issues = articles.check(spec)["issues"]
    fields = [issue["field"] for issue in issues if issue["code"] == "alt_missing"]
    assert any(field.endswith("items[1].alt") for field in fields)


def test_gallery_renders_inline_svg_without_external_files():
    html = articles.render_html(_spec())
    assert 'class="gallery"' in html
    assert html.count("<svg") >= 4          # три плитки + полоса ролика
    # ни тегов <img>, ни внешних источников: всё рисуется inline-SVG медиа-движка
    assert "<img" not in html
    assert 'src="http' not in html and "<link" not in html


# ---------------------------------------------------------------- видео
def test_video_builds_reel_spec_from_section():
    spec = _spec()
    video = next(block for block in spec["blocks"] if block["type"] == "video")
    reel = video["props"]["reel"]
    assert reel and video["props"]["provider"] == "reel"
    assert 2 <= reel["scenes"] <= media_reel.limits()["max_scenes"]
    assert reel["duration"] <= media_reel.limits()["max_duration"]
    assert reel["format"] == "16x9" and reel["fps"] == 25
    assert media_reel.check(reel["spec"])["ok"]
    labels = [shot["label"] for shot in reel["storyboard"]]
    assert labels and all(labels)
    assert [shot["template"] for shot in reel["storyboard"]][0] == "hook-typed"


def test_video_storyboard_timeline_is_monotonic():
    reel = next(block for block in _spec()["blocks"]
                if block["type"] == "video")["props"]["reel"]
    starts = [shot["t"] for shot in reel["storyboard"]]
    assert starts == sorted(starts) and starts[0] == 0
    assert sum(shot["duration"] for shot in reel["storyboard"]) == pytest.approx(
        reel["duration"], abs=0.01)


def test_video_accepts_client_link_only():
    brief = _brief()
    brief["outline"][1]["video"] = {"src": "https://cdn.example.com/reel.mp4"}
    spec = articles.compose(brief)["spec"]
    video = next(block for block in spec["blocks"] if block["type"] == "video")
    assert video["props"]["provider"] == "external"
    assert video["props"]["reel"] is None
    html = articles.render_html(spec)
    assert '<video controls' in html and "cdn.example.com/reel.mp4" in html


def test_video_without_source_is_reported():
    spec = _spec()
    video = next(block for block in spec["blocks"] if block["type"] == "video")
    video["props"]["reel"] = None
    video["props"]["src"] = None
    codes = {issue["code"] for issue in articles.check(spec)["issues"]}
    assert "video_source" in codes


def test_video_longer_than_limit_is_reported():
    spec = _spec()
    video = next(block for block in spec["blocks"] if block["type"] == "video")
    video["props"]["reel"]["duration"] = 500.0
    codes = {issue["code"] for issue in articles.check(spec)["issues"]}
    assert "video_too_long" in codes


def test_video_strip_shows_scenes_and_duration():
    html = articles.render_html(_spec())
    assert "video-strip" in html
    assert "сцен" in html and "16x9" in html
    assert "ролик из спеки статьи" in html


# ---------------------------------------------------------------- приоритет данных
def test_data_outranks_keywords_for_gallery_and_video():
    ranked = articles._rank_rules({"heading": "Примеры", "text": "Несколько примеров рядом",
                                   "data": {"gallery": GALLERY}})
    assert ranked[0]["block"] == "gallery"
    ranked = articles._rank_rules({"heading": "Демонстрация", "text": "Покажем в движении",
                                   "data": {"video": {"items": ["Раз"]}}})
    assert ranked[0]["block"] == "video"


def test_summary_lists_video_providers():
    data = articles.summary()
    codes = {row["code"] for row in data["videos"]}
    assert codes == {"reel", "external"}
    assert next(row for row in data["videos"] if row["code"] == "reel")["available"] is True


# ---------------------------------------------------------------- API
def test_api_article_with_gallery_and_video():
    created = client.post("/api/v1/articles", headers=H,
                          json=_brief(materials=[], strict=False))
    assert created.status_code == 201, created.text
    body = created.json()
    kinds = [block["type"] for block in body["spec"]["blocks"]]
    assert "gallery" in kinds and "video" in kinds
    page = client.get(body["urls"]["preview"], headers=H).text
    assert 'class="gallery"' in page and "video-strip" in page
    detail = client.get(f"/api/v1/articles/{body['article_id']}", headers=H).json()
    video = next(block for block in detail["spec"]["blocks"] if block["type"] == "video")
    assert video["props"]["reel"]["spec"]["spec_version"].startswith("reel.spec")
