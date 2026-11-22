"""Маркировка рекламы в кадре: правило — данные рынка, плашка — в каждом кадре (этап E).

Что здесь проверяется и почему именно так:

1. **Правило задаётся пакетом, а не рендерером.** Для РФ обязательны `erid`, рекламодатель и
   ИНН; в ЕС достаточно раскрытия; по FTC — «paid partnership». Если это зашито в вёрстку,
   каждый рынок требует релиза движка — тесты фиксируют обратное.
2. **Неизвестный пакет не откатывается на умолчание.** Тихий фолбэк — худший вариант: ролик
   соберётся «как-нибудь» и уйдёт без правильной маркировки.
3. **Плашка в кадре, а не в описании.** Проверяется не «функция вернула текст», а текст в SVG
   на нескольких моментах времени: маркировка не может появиться только в одной сцене.
4. **Плашка внутри зоны контента.** Нижние 420 px вертикали закрывает интерфейс Shorts/клипов:
   маркировка под ними формально есть, фактически — нет.
5. **Описание и кадр не разъезжаются.** Одни и те же данные дают и плашку, и текст для описания.
"""
from __future__ import annotations

import json
import re

import pytest
from fastapi.testclient import TestClient

from orbita import media, media_compliance, media_reel, media_video
from orbita.main import app

client = TestClient(app)
H = {"X-Actor-Role": "owner"}

AD = {"pack": "ru-ad", "ad": True, "erid": "2Vtzqw9DemoErID",
      "advertiser": "ООО «Тест»", "inn": "7700000000", "min_age": 18}
POST = {"title": "Один договор — 29 площадок", "footer": "posiz.ru",
        "items": ["Один пост — все каналы", "Лимиты проверяются до публикации"]}


@pytest.fixture(autouse=True)
def clean(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_SPOOL_DIR", str(tmp_path / "media"))
    monkeypatch.setenv("MEDIA_STORAGE", "spool")
    monkeypatch.setenv("MEDIA_URL_SECRET", "test-media-url-secret")
    monkeypatch.delenv("MEDIA_COMPLIANCE_PACKS", raising=False)
    media_compliance.reload_packs()
    for collection in (media_reel._reels, media_reel._specs, media._assets, media._jobs,
                       media._specs):
        collection.clear_memory()
    yield
    media_compliance.reload_packs()


def _codes(compliance) -> list[str]:
    return [issue["code"] for issue in media_compliance.validate(compliance)]


def _reel(compliance=None, post=None) -> dict:
    return media_reel.build_reel_spec(post or POST, fps=25, compliance=compliance)


def _text_y(svg: str, needle: str) -> int:
    """Y-координата текста, в котором встречается ``needle``: основа проверки safe zone."""
    index = svg.find(needle)
    assert index != -1, f"в кадре нет текста {needle!r}"
    start = svg.rfind("<text", 0, index)
    match = re.search(r'(?<=\s)y="(\d+)"', svg[start:index])
    assert match, "не нашли координату y в теге text"
    return int(match.group(1))


# ------------------------------------------------------------------ пакеты правил
def test_registry_has_packs_for_different_markets():
    rows = {row["code"]: row for row in media_compliance.packs()}
    assert {"ru-ad", "ru-info", "eu-disclosure", "us-ftc", "generic"} <= set(rows)
    assert rows["ru-ad"]["requires"] == ["erid", "advertiser", "inn"]
    assert "erid" not in rows["eu-disclosure"]["requires"]
    assert media_compliance.DEFAULT_PACK in rows


def test_ru_ad_needs_erid_advertiser_and_inn():
    assert {"erid_required", "advertiser_required", "inn_invalid"} <= set(_codes({"ad": True}))


def test_correct_marking_passes_without_issues():
    assert media_compliance.validate(AD) == []


def test_unknown_pack_is_rejected_not_defaulted():
    """Неизвестный пакет — ошибка, а не «возьмём ru-ad на всякий случай»."""
    assert _codes({"pack": "kz-ad", "ad": True}) == ["pack_unknown"]
    assert media_compliance.plates({"pack": "kz-ad", "ad": True, "erid": "x"}) == []
    assert media_compliance.description({"pack": "kz-ad", "ad": True}) is None


def test_market_pack_can_be_added_by_configuration(tmp_path, monkeypatch):
    """Новый рынок — файл, а не правка движка (контракт гибкости)."""
    path = tmp_path / "packs.json"
    path.write_text(json.dumps({"kz-ad": {
        "title": "Казахстан: реклама", "jurisdiction": "KZ", "ad_allowed": True,
        "labels": {"ru": "Жарнама", "kk": "Жарнама"}, "default_language": "ru",
        "erid_required": False, "advertiser_required": True, "inn_required": False,
        "age_values": [0, 12, 16, 18], "description": "{label}. {advertiser}",
    }}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("MEDIA_COMPLIANCE_PACKS", str(path))
    media_compliance.reload_packs()

    assert media_compliance.validate({"pack": "kz-ad", "ad": True,
                                      "advertiser": "ТОО «Ромашка»"}) == []
    plates = media_compliance.plates({"pack": "kz-ad", "ad": True, "advertiser": "ТОО «Ромашка»"})
    assert plates[0]["lines"][0] == "Жарнама"
    assert media_compliance.pack("kz-ad")["jurisdiction"] == "KZ"


def test_label_follows_pack_language():
    assert media_compliance.plates({"pack": "ru-ad", "ad": True, "erid": "2Vtzqw9X",
                                    "advertiser": "ООО «Т»", "inn": "7700000000"})[0]["lines"][0] \
        .startswith("Реклама")
    assert media_compliance.plates({"pack": "eu-disclosure", "ad": True,
                                    "advertiser": "ACME"})[0]["lines"][0] == "Ad"
    assert media_compliance.plates({"pack": "eu-disclosure", "ad": True, "advertiser": "ACME",
                                    "lang": "de"})[0]["lines"][0] == "Anzeige"
    assert media_compliance.plates({"pack": "us-ftc", "ad": True})[0]["lines"][0] \
        == "Paid partnership"


def test_age_categories_come_from_pack():
    assert _codes({"min_age": 21}) == ["min_age_invalid"]           # в РФ такой метки нет
    assert media_compliance.validate({"pack": "us-ftc", "min_age": 21}) == []
    assert media_compliance.validate({"pack": "eu-disclosure", "min_age": 6}) != []


def test_info_pack_forbids_advertising():
    assert _codes({"pack": "ru-info", "ad": True}) == ["ad_forbidden_by_pack"]


def test_erid_without_ad_flag_is_a_mismatch():
    assert _codes({"erid": "2Vtzqw9X"}) == ["erid_without_ad"]


def test_placeholder_erid_is_not_marking():
    assert _codes({"ad": True, "erid": "____", "advertiser": "ООО «Т»",
                   "inn": "7700000000"}) == ["erid_invalid"]


def test_disclaimer_has_a_length_limit():
    assert _codes({"disclaimer": "я" * (media_compliance.MAX_DISCLAIMER_CHARS + 1)}) \
        == ["disclaimer_too_long"]


# ------------------------------------------------------------------ плашки
def test_plates_order_and_requisites():
    plates = media_compliance.plates({**AD, "disclaimer": "Не консультация"})
    assert [row["kind"] for row in plates] == ["ad", "age", "disclaimer"]
    assert plates[0]["lines"][0] == "Реклама · erid: 2Vtzqw9DemoErID"
    assert plates[0]["lines"][1] == "ООО «Тест» · ИНН 7700000000"
    assert plates[1]["lines"] == ["18+"]
    assert plates[2]["lines"] == ["Не консультация"]


def test_description_duplicates_the_marking():
    text = media_compliance.description(AD)
    assert text.startswith("Реклама. ООО «Тест», ИНН 7700000000. erid: 2Vtzqw9DemoErID")
    assert media_compliance.description({"pack": "generic", "ad": True}) == "Реклама"
    assert media_compliance.description({"min_age": 6}) is None


def test_reserve_ignores_age_badge():
    with_age = media_compliance.reserve_height(media_compliance.plates(AD), line_height=44,
                                               gap=18)
    without_age = media_compliance.reserve_height(
        media_compliance.plates({k: v for k, v in AD.items() if k != "min_age"}),
        line_height=44, gap=18)
    assert with_age == without_age == 44 * 2 + 18


# ------------------------------------------------------------------ карточки
def test_card_renders_marking_from_pack():
    spec = media.build_spec({"title": "Скидка 20%"}, "1x1", compliance=AD)
    assert media.check(spec)["ok"] is True, media.check(spec)
    svg = media.render_svg(spec)
    assert "Реклама · erid: 2Vtzqw9DemoErID" in svg
    assert "ИНН 7700000000" in svg and "18+" in svg


def test_card_marking_stays_inside_content_box():
    spec = media.build_spec({"title": "Скидка 20%"}, "9x16", compliance=AD)
    box = media.content_box("9x16")
    for bbox in media.measure(spec):
        assert bbox["y"] + bbox["height"] <= box["y"] + box["height"] + media._TOLERANCE
        assert bbox["x"] + bbox["width"] <= box["x"] + box["width"] + media._TOLERANCE


def test_eu_pack_marks_ad_without_erid():
    """Гибкость в одну строку: тот же рендер, другой пакет — плашка без erid."""
    spec = media.build_spec({"title": "SALE"}, "1x1",
                            compliance={"pack": "eu-disclosure", "ad": True,
                                        "advertiser": "ACME Ltd"})
    assert media.check(spec)["ok"] is True, media.check(spec)
    svg = media.render_svg(spec)
    assert "Ad" in svg and "ACME Ltd" in svg
    assert "erid" not in svg


def test_card_rejects_erid_without_ad_flag():
    spec = media.build_spec({"title": "Скидка"}, "1x1", compliance={"erid": "2Vtzqw9X"})
    assert "erid_without_ad" in [issue["code"] for issue in media.validate_spec(spec)]


# ------------------------------------------------------------------ ролики
def test_reel_rejects_ad_without_marking():
    spec = _reel({"ad": True})
    codes = [issue["code"] for issue in media_reel.check(spec)["issues"]]
    assert {"erid_required", "advertiser_required"} <= set(codes)


def test_reel_marks_every_frame_not_just_one_scene():
    spec = _reel({**AD, "disclaimer": "Не является консультацией"})
    assert media_reel.check(spec)["ok"] is True, media_reel.check(spec)
    duration = float(spec["duration"])
    for moment in (0.0, duration * 0.2, duration * 0.45, duration * 0.7, duration):
        svg = media_reel.frame_at(spec, moment)
        assert "Реклама · erid: 2Vtzqw9DemoErID" in svg
        assert "18+" in svg
        assert "Не является консультацией" in svg


def test_reel_plate_lives_inside_content_box():
    """Плашка выше нижней зоны интерфейса: иначе её закрывают кнопки канала."""
    spec = _reel(AD)
    box = media.content_box("9x16")
    bottom_edge = box["y"] + box["height"]
    svg = media_reel.frame_at(spec, float(spec["duration"]) * 0.5)
    assert _text_y(svg, "Реклама") < bottom_edge
    assert _text_y(svg, "ООО «Тест»") < bottom_edge
    # прогресс-бар прижат к границе зоны контента, а не уходит под интерфейс
    bar_y = int(re.search(r'y="(\d+)" width="\d+" height="6"', svg).group(1))
    assert bar_y + 6 <= bottom_edge
    assert bottom_edge - bar_y <= 12


def test_reel_reserves_room_for_plate():
    """Текст сцены не наезжает на маркировку: зона контента ужимается заранее."""
    # подпись в финале отличается от имени бренда сверху, иначе тест найдёт не тот текст
    post = {"title": "Скидка 20%", "footer": "конвейер-публикаций.рф"}
    plain = media_reel.build_reel_spec(post, fps=25)
    marked = media_reel.build_reel_spec(post, fps=25, compliance=AD)
    plain_svg = media_reel.frame_at(plain, float(plain["duration"]) * 0.9)
    marked_svg = media_reel.frame_at(marked, float(marked["duration"]) * 0.9)
    plain_footer = _text_y(plain_svg, post["footer"])
    marked_footer = _text_y(marked_svg, post["footer"])
    assert marked_footer < plain_footer, "подпись не сдвинулась вверх под плашку"
    assert plain_footer - marked_footer >= media_compliance.reserve_height(
        media_compliance.plates(AD), line_height=media_reel.PLATE_LINE, gap=media_reel.PLATE_GAP)


def test_plate_that_does_not_fit_is_rejected():
    long_name = "ООО «Очень длинное название рекламодателя для проверки переноса плашки»"
    spec = _reel({**AD, "advertiser": long_name})
    assert "plate_too_long" in [issue["code"] for issue in media_reel.check(spec)["issues"]]


def test_plate_share_guard(monkeypatch):
    monkeypatch.setattr(media_reel, "MAX_PLATE_SHARE", 0.05)
    spec = _reel({**AD, "disclaimer": "Не является консультацией"})
    assert "compliance_no_room" in [issue["code"] for issue in media_reel.check(spec)["issues"]]


# ------------------------------------------------------------------ задача и API
def test_job_keeps_compliance_state_and_description_artifact(tmp_path):
    spec = _reel(AD)
    result = media_reel.submit(spec, render=True, limit=4)
    job = result["reel"]
    assert job["compliance"]["pack"] == "ru-ad"
    assert job["compliance"]["blocked"] is False
    assert job["compliance"]["plates"][0]["kind"] == "ad"
    if not media_video.detect()["available"] or not media_reel.media_raster.available()["available"]:
        pytest.skip("нужен растеризатор и ffmpeg: без них файлы не собираются")
    text = media_reel.content(job["id"], "txt")
    assert text is not None, job.get("video_error")
    data, mime = text
    assert mime.startswith("text/plain")
    assert b"2Vtzqw9DemoErID" in data and "ИНН".encode() in data


def test_api_lists_packs_and_default():
    response = client.get("/api/v1/media/compliance/packs", headers=H)
    assert response.status_code == 200
    body = response.json()
    assert body["default"] == "ru-ad"
    assert {"ru-ad", "eu-disclosure", "generic"} <= {row["code"] for row in body["items"]}
    assert body["limits"]["max_disclaimer_chars"] == media_compliance.MAX_DISCLAIMER_CHARS

    summary = client.get("/api/v1/media/summary", headers=H).json()
    assert summary["compliance"]["default_pack"] == "ru-ad"
    assert "us-ftc" in summary["compliance"]["packs"]


def test_api_rejects_unmarked_ad_with_readable_issues():
    response = client.post("/api/v1/media/reels", headers=H,
                           json={"post": POST, "render": False,
                                 "compliance": {"ad": True}})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["kind"] == "reel_rejected"
    assert "erid_required" in {issue["code"] for issue in detail["issues"]}


def test_api_accepts_marked_ad_and_advertises_description():
    response = client.post("/api/v1/media/reels", headers=H,
                           json={"post": POST, "render": False, "compliance": AD})
    assert response.status_code in {200, 201}, response.text
    body = response.json()
    assert body["reel"]["compliance"]["label"] == "Реклама"
    assert "erid" in body["reel"]["compliance"]["description"]
    assert body["checks"]["ok"] is True
