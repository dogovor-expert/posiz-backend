"""Карточки и обложки из спеки (этап A медиа-конвейера, ADR 0023).

Проверяется не «файл создался», а четыре обещания:

1. **Спека отделена от вёрстки.** Модель выбирает шаблон из закрытого набора и заполняет
   поля; координаты приносит рендерер, поэтому «сдвинуть заголовок влево» извне нельзя.
2. **Проверки работают до рендера.** Обрезка, попадание под интерфейс канала, контраст и
   маркировка рекламы — арифметика, а не «модель посмотрела». Тесты проверяют, что
   плохая спека отвергается, а не «рендерится как получится».
3. **Рендер детерминирован.** Одинаковая спека — побайтово тот же SVG; повторный запрос
   отдаёт готовый артефакт, а не платит CPU заново.
4. **Текст не исполняется.** Пользовательский заголовок попадает в SVG только экранированным.
"""
from __future__ import annotations

import re
from xml.etree import ElementTree as ET

import pytest
from fastapi.testclient import TestClient

from orbita import media, tenancy
from orbita.main import app

client = TestClient(app)
H = {"X-Actor-Role": "owner"}


POST = {
    "title": "Один договор — 29 площадок",
    "eyebrow": "контент-конвейер",
    "note": "Без копипаста: адаптации под лимиты каналов, маркировка рекламы — до публикации",
    "footer": "posiz.ru",
}
BRAND = {"name": "dogovor.expert", "accent": "#7c5cff"}
AD = {"ad": True, "erid": "2Vtzqw9DemoErID", "advertiser": "ООО «Договор Эксперт»",
      "inn": "7700000000"}


def _spec(fmt: str = "9x16", template: str = "card-hero", **kwargs) -> dict:
    post = kwargs.pop("post", POST)
    return media.build_spec(post, fmt, template, brand=kwargs.pop("brand", BRAND),
                            compliance=kwargs.pop("compliance", None))


# ------------------------------------------------------------------ схема
def test_unknown_template_is_rejected():
    spec = _spec()
    spec["template"] = "card-fancy-3d"
    codes = [issue["code"] for issue in media.validate_spec(spec)]
    assert "template_unknown" in codes


def test_wrong_spec_version_is_rejected():
    spec = _spec()
    spec["spec_version"] = "reel.spec.v2"
    assert "spec_version" in [i["code"] for i in media.validate_spec(spec)]


def test_unknown_format_is_rejected():
    """4:5 теперь полноценный формат, поэтому «неизвестный» — это 21:9."""
    spec = _spec()
    spec["format"] = "21x9"
    assert "format_unknown" in [i["code"] for i in media.validate_spec(spec)]


def test_headline_longer_than_limit_is_rejected():
    spec = _spec(post={"title": "а" * (media.MAX_HEADLINE_CHARS + 1)})
    assert "headline_too_long" in [i["code"] for i in media.validate_spec(spec)]


def test_unbreakable_word_is_rejected():
    spec = _spec(post={"title": "ок " + "ш" * (media.MAX_LINE_CHARS + 5)})
    assert "word_too_long" in [i["code"] for i in media.validate_spec(spec)]


def test_too_many_items_are_rejected():
    # build_spec обрезает список до лимита, поэтому проверяем «сырую» спеку:
    # именно такой документ может прийти из API или из старых данных
    spec = _spec("1x1", "card-list", post={"title": "Список", "items": ["пункт"] * media.MAX_ITEMS})
    spec["props"]["items"] = [f"пункт {n}" for n in range(media.MAX_ITEMS + 1)]
    assert "items_too_many" in [i["code"] for i in media.validate_spec(spec)]


def test_list_without_items_falls_back_to_hero():
    spec = media.build_spec({"title": "Только заголовок"}, "1x1", "card-list")
    assert spec["template"] == "card-hero"


# ------------------------------------------------------------------ геометрия
def test_metrics_are_measured_not_guessed():
    """Ширина считается по метрикам глифов: заголовок из 30 знаков шире 860 px в 9:16.

    Первая версия оценивала кириллицу в 0,52 em и «не видела» переполнение —
    заголовок обрезался в кадре при зелёных проверках.
    """
    spec = _spec()
    headline = spec["props"]["headline"]
    assert media.text_width(headline, 64, bold=True) > media.content_box("9x16")["width"]
    assert len(media._wrap(headline, 64, 860, bold=True)) > 1


def test_headline_is_wrapped_inside_content_box():
    spec = _spec()
    assert media.check_layout(spec) == []
    box = media.content_box("9x16")
    for bbox in media.measure(spec):
        assert bbox["x"] >= box["x"]
        assert bbox["x"] + bbox["width"] <= box["x"] + box["width"]
        assert bbox["y"] >= box["y"]
        assert bbox["y"] + bbox["height"] <= box["y"] + box["height"]


def test_overflowing_content_hits_safe_zone():
    """Слишком много валидного текста для 1:1: пункты уезжают в нижнюю зону интерфейса.

    Все лимиты по отдельности соблюдены (заголовок, длина пунктов, их количество) —
    ловится именно составная проверка геометрии, ради которой она и написана.
    """
    spec = _spec("1x1", "card-list", post={
        "title": "Девять причин перейти на один конвейер публикаций и не копипастить вручную",
        "eyebrow": "контент",
        "items": ["Первый пункт ровно сорок восемь символов в срок!",
                  "Второй пункт ровно сорок восемь символов в срок",
                  "Третий пункт ровно сорок восемь символов в срок",
                  "Четвертый пункт сорок восемь символов в срок!",
                  "Пятый пункт ровно сорок восемь символов в срок"],
        "footer": "posiz.ru",
    })
    assert media.validate_spec(spec) == []          # данные валидны
    report = media.check(spec)
    assert report["ok"] is False
    assert "safe_zone" in {i["code"] for i in report["issues"]}


def test_safe_zones_match_design_tokens():
    """Числа должны совпадать с 20-DESIGN/video/README.md — иначе прототип и код разойдутся."""
    assert media.SAFE_ZONES["9x16"] == {"top": 220, "bottom": 420, "left": 80, "right": 140}
    assert media.content_box("9x16") == {"x": 80, "y": 220, "width": 860, "height": 1280}


# ------------------------------------------------------------------ контраст
def test_contrast_uses_wcag_formula():
    assert media.contrast_ratio("#ffffff", "#000000") == 21.0
    assert media.contrast_ratio("#6f4bf5", "#ffffff") == 5.25
    assert media.contrast_ratio("#7c5cff", "#ffffff") == 4.35


def test_low_contrast_background_is_rejected():
    """Светлая подложка при светлом тексте — та же ошибка, что ловил аудит сайта."""
    spec = _spec(brand={"name": "test", "bg": "#f2f2f2", "text": "#ffffff"})
    codes = [i["code"] for i in media.check(spec)["issues"]]
    assert "contrast" in codes


# ------------------------------------------------------------------ комплаенс
def test_ad_without_erid_is_rejected():
    spec = _spec(compliance={"ad": True})
    codes = [i["code"] for i in media.validate_spec(spec)]
    assert {"erid_required", "advertiser_required", "inn_invalid"} <= set(codes)
    assert media.check(spec)["ok"] is False


def test_ad_with_marking_renders_plate():
    spec = _spec(compliance=AD)
    report = media.check(spec)
    assert report["ok"] is True, report["issues"]
    svg = media.render_svg(spec)
    assert "Реклама" in svg
    assert AD["erid"] in svg
    assert "ИНН 7700000000" in svg


def test_non_ad_card_has_no_plate():
    svg = media.render_svg(_spec())
    assert "Реклама" not in svg


def test_invalid_inn_is_rejected():
    spec = _spec(compliance={**AD, "inn": "12345"})
    assert "inn_invalid" in [i["code"] for i in media.validate_spec(spec)]


def test_invalid_min_age_is_rejected():
    spec = _spec(compliance={"ad": False, "min_age": 21})
    assert "min_age_invalid" in [i["code"] for i in media.validate_spec(spec)]


# ------------------------------------------------------------------ рендер
@pytest.mark.parametrize("fmt", sorted(media.FORMATS))
def test_render_has_format_size_and_is_valid_svg(fmt: str):
    svg = media.render_svg(_spec(fmt))
    assert f'width="{media.FORMATS[fmt][0]}"' in svg
    assert f'height="{media.FORMATS[fmt][1]}"' in svg
    root = ET.fromstring(svg)              # валидный XML, а не «похоже на SVG»
    assert root.tag.endswith("svg")


def test_render_is_deterministic():
    spec = _spec()
    assert media.render_svg(spec) == media.render_svg(spec)


def test_user_text_is_escaped():
    spec = _spec(post={"title": "<script>alert(1)</script> 29 площадок"})
    svg = media.render_svg(spec)
    assert "<script>" not in svg
    assert "&lt;script&gt;" in svg
    ET.fromstring(svg)


def test_spec_hash_is_order_independent():
    spec = _spec()
    shuffled = dict(reversed(list(spec.items())))
    assert media.spec_hash(spec) == media.spec_hash(shuffled)


def test_renderer_version_changes_hash_and_keeps_history():
    """Правка шаблона обязана давать новый артефакт, а не «тот же файл»."""
    spec = _spec()
    first = media.spec_hash(spec)
    original = media.RENDERER
    try:
        media.RENDERER = "inhouse-svg@2"
        assert media.spec_hash(spec) != first
    finally:
        media.RENDERER = original


# ------------------------------------------------------------------ задачи
def test_submit_creates_job_and_asset():
    result = media.submit(_spec())
    assert result["job"]["status"] == "done"
    # mime зависит от узла: есть растеризатор — PNG, нет — SVG. Обещать заранее нельзя,
    # поэтому проверяем, что SVG в хранилище есть всегда (этап B: SVG — источник истины)
    assert result["asset"]["mime"] in {"image/svg+xml", "image/png"}
    assert result["asset"]["bytes"] > 500
    assert result["asset"].get("svg_key")
    assert media.asset_content(result["asset"]["id"], "svg") is not None
    assert media.job(result["job"]["id"])["asset_id"] == result["asset"]["id"]


def test_rejected_spec_creates_failed_job_without_asset():
    bad = media.build_spec({"title": "Реклама без маркировки"}, "1x1", "card-hero",
                           compliance={"ad": True})
    result = media.submit(bad)
    assert result["job"]["status"] == "failed"
    assert result["job"]["error"] == "spec_rejected"
    assert result["asset"] is None


def test_same_spec_is_rendered_once():
    spec = _spec()
    first = media.submit(spec)
    second = media.submit(spec)
    assert second.get("reused") is True
    assert second["asset"]["id"] == first["asset"]["id"]


def test_cancel_marks_job():
    result = media.submit(_spec())
    item = result["job"]
    item["status"] = "queued"
    media._jobs.put(item)
    cancelled = media.cancel(item["id"])
    assert cancelled["status"] == "cancelled"
    assert media.cancel("rj_нет-такой") is None


# ------------------------------------------------------------------ API
def test_api_returns_cards_for_all_formats():
    response = client.post("/api/v1/media/cards", headers=H, json={
        "post": POST, "formats": ["1x1", "9x16", "16x9"], "brand": BRAND,
    })
    assert response.status_code == 201, response.text
    body = response.json()
    assert [item["format"] for item in body["items"]] == ["1x1", "9x16", "16x9"]
    for item in body["items"]:
        assert item["status"] == "done"
        assert item["checks"]["ok"] is True
        asset = client.get(f"/api/v1/media/assets/{item['asset_id']}", headers=H)
        assert asset.status_code == 200
        assert asset.headers["content-type"].startswith("image/svg+xml")
        assert asset.text.startswith("<svg")


def test_api_route_404_for_unknown_asset():
    assert client.get("/api/v1/media/assets/as_нет", headers=H).status_code == 404


def test_api_422_when_ad_has_no_erid():
    response = client.post("/api/v1/media/cards", headers=H, json={
        "post": POST, "formats": ["1x1"], "compliance": {"ad": True},
    })
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["items"][0]["checks"]["ok"] is False


def test_api_ad_card_contains_marking():
    response = client.post("/api/v1/media/cards", headers=H, json={
        "post": {"title": "Скидка 20% на курс до 3 марта"}, "formats": ["9x16"],
        "compliance": AD, "brand": BRAND,
    })
    assert response.status_code == 201
    asset_id = response.json()["items"][0]["asset_id"]
    svg = client.get(f"/api/v1/media/assets/{asset_id}", headers=H).text
    assert AD["erid"] in svg and "Реклама" in svg


def test_api_skips_unknown_format_but_renders_known():
    response = client.post("/api/v1/media/cards", headers=H, json={
        "post": POST, "formats": ["9x16", "21x9"],
    })
    assert response.status_code == 201
    body = response.json()
    assert body["skipped"] == [{"format": "21x9", "reason": "format_unknown"}]


def test_api_templates_and_limits_are_public_to_workspace():
    response = client.get("/api/v1/media/templates", headers=H)
    assert response.status_code == 200
    body = response.json()
    ids = {item["id"] for item in body["items"]}
    assert ids == set(media.TEMPLATES)
    assert body["limits"]["max_headline_chars"] == media.MAX_HEADLINE_CHARS


def test_api_jobs_list_and_detail():
    created = client.post("/api/v1/media/cards", headers=H,
                          json={"post": POST, "formats": ["1x1"]}).json()
    job_id = created["items"][0]["job_id"]
    detail = client.get(f"/api/v1/media/jobs/{job_id}", headers=H)
    assert detail.status_code == 200
    assert detail.json()["job"]["id"] == job_id
    assert detail.json()["spec"]["spec_version"] == media.SPEC_VERSION
    listing = client.get("/api/v1/media/jobs", headers=H)
    assert any(item["id"] == job_id for item in listing.json()["items"])


def test_jobs_are_not_visible_from_another_workspace():
    media._jobs.clear_memory()
    media._specs.clear_memory()
    media._assets.clear_memory()
    created = client.post("/api/v1/media/cards", headers=H,
                          json={"post": POST, "formats": ["1x1"]}).json()
    job_id = created["items"][0]["job_id"]
    with tenancy.bind("ws_другой"):
        assert media.job(job_id) is None
        assert media.jobs() == []
    assert media.job(job_id) is not None


def test_summary_counts_jobs():
    media._jobs.clear_memory()
    client.post("/api/v1/media/cards", headers=H, json={"post": POST, "formats": ["1x1"]})
    summary = client.get("/api/v1/media/summary", headers=H).json()
    assert summary["jobs"] >= 1
    assert summary["renderer"] == media.RENDERER
