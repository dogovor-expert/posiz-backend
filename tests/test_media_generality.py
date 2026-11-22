"""Гибкость платформы: формат, brand kit, лимиты и провайдеры — данные, а не код (P0).

Проверяется не «функция вернула словарь», а то, из-за чего платформа превращается в
однонишевый продукт:

1. **Одна спека → четыре формата.** 9:16, 4:5, 1:1 и 16:9 рендерятся тем же кодом, кадр
   выходит в размерах формата, а типографика масштабируется от зоны контента. Незнакомый
   формат — ошибка, а не тихое 9:16.
2. **Brand kit.** Светлая тема, гарнитура и масштаб типографики приходят из спеки; неизвестные
   значения отвергаются, а не подменяются молча.
3. **Лимиты и тарифы — конфигурация.** Длительность, число сцен и цена озвучки меняются
   окружением без релиза: сегодня 60 секунд под Shorts, завтра 180 — и это не правка кода.
4. **Провайдеры TTS — реестр.** Цена, языки, доступность и обработчик в одном месте: новый
   провайдер добавляется записью, а не веткой `if` в синтезе.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from orbita import media, media_reel, media_video, media_voice
from orbita.main import app

client = TestClient(app)
H = {"X-Actor-Role": "owner"}

POST = {"title": "Один пост — все каналы", "footer": "posiz.ru",
        "items": ["Первый пункт", "Второй пункт"]}
AD = {"pack": "ru-ad", "ad": True, "erid": "2Vtzqw9DemoErID",
      "advertiser": "ООО «Тест»", "inn": "7700000000", "min_age": 18}
FFMPEG = media_video.detect()["available"]


@pytest.fixture(autouse=True)
def clean(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_SPOOL_DIR", str(tmp_path / "media"))
    monkeypatch.setenv("MEDIA_STORAGE", "spool")
    monkeypatch.setenv("MEDIA_URL_SECRET", "test-media-url-secret")
    for key in ("MEDIA_REEL_MAX_DURATION", "MEDIA_REEL_MAX_SCENES", "MEDIA_REEL_MIN_DURATION",
                "MEDIA_REEL_MAX_FRAMES", "MEDIA_TTS_YANDEX_RUB_PER_1000"):
        monkeypatch.delenv(key, raising=False)
    for collection in (media_reel._reels, media_reel._specs, media._assets, media._jobs,
                       media._specs):
        collection.clear_memory()
    yield


def _spec(**kwargs) -> dict:
    return media_reel.build_reel_spec(kwargs.pop("post", POST), fps=kwargs.pop("fps", 25), **kwargs)


# ------------------------------------------------------------------ формат как данные
def test_four_formats_are_supported():
    assert set(media_reel.REEL_FORMATS) <= set(media.FORMATS)
    assert media.FORMATS["4x5"] == (1080, 1350)
    assert media.SAFE_ZONES["4x5"]["bottom"] > 0


@pytest.mark.parametrize("fmt", media_reel.REEL_FORMATS)
def test_same_spec_renders_every_format(fmt: str):
    spec = _spec(fmt=fmt)
    assert media_reel.check(spec)["ok"] is True, media_reel.check(spec)
    width, height = media.FORMATS[fmt]
    svg = media_reel.frame_at(spec, float(spec["duration"]) * 0.5)
    assert f'width="{width}"' in svg and f'height="{height}"' in svg
    assert spec["format"] == fmt


def test_unknown_format_is_rejected_not_defaulted():
    spec = _spec(fmt="21x9")
    assert spec["format"] == "21x9", "формат не должен подменяться молча"
    assert "format_unknown" in [issue["code"] for issue in media_reel.check(spec)["issues"]]


def test_canvas_scale_follows_content_height():
    scales = {fmt: media_reel.canvas(_spec(fmt=fmt))["scale"] for fmt in media_reel.REEL_FORMATS}
    assert scales["9x16"] == pytest.approx(1.0, abs=0.01)
    assert scales["16x9"] == pytest.approx(900 / 1280, abs=0.01)
    assert scales["1x1"] == pytest.approx(888 / 1280, abs=0.01)
    assert scales["4x5"] > scales["9x16"] * 0.85


def test_landscape_wraps_by_real_width_not_portrait_column():
    """В 16:9 строка шире: тот же хук переносится иначе, иначем была бы узкая колонка."""
    post = {"title": "Один договор закрывает двадцать девять площадок сразу", "footer": "x.ru"}
    portrait = media_reel.build_reel_spec(post, fps=25, fmt="9x16")
    landscape = media_reel.build_reel_spec(post, fps=25, fmt="16x9")
    size_p = int(round(media_reel._hook_size(post["title"]) * media_reel.canvas(portrait)["scale"]))
    size_l = int(round(media_reel._hook_size(post["title"]) * media_reel.canvas(landscape)["scale"]))
    lines_p = len(media._wrap(post["title"], size_p, media.content_box("9x16")["width"], bold=True))
    lines_l = len(media._wrap(post["title"], size_l, media.content_box("16x9")["width"], bold=True))
    assert lines_l < lines_p


def test_marking_stays_inside_content_box_in_every_format():
    for fmt in media_reel.REEL_FORMATS:
        spec = _spec(fmt=fmt, compliance={**AD, "disclaimer": "Не является офертой"})
        report = media_reel.check(spec)
        assert report["ok"] is True, (fmt, report)
        box = media.content_box(fmt)
        reserve = media_compliance_reserve(spec)
        assert reserve > 0
        assert reserve + 16 <= box["height"], f"{fmt}: плашка не помещается в зону контента"


def media_compliance_reserve(spec: dict) -> int:
    from orbita import media_compliance

    scale = media_reel.canvas(spec)["scale"]
    return media_compliance.reserve_height(media_compliance.plates(spec["compliance"]),
                                           line_height=media_reel.PLATE_LINE * scale,
                                           gap=media_reel.PLATE_GAP * scale)


def test_format_is_part_of_spec_hash():
    portrait = _spec()
    landscape = _spec(fmt="16x9")
    assert media_reel.spec_hash(portrait) != media_reel.spec_hash(landscape)


# ------------------------------------------------------------------ brand kit
def test_light_theme_and_serif_are_applied():
    spec = _spec(brand={"theme": "light", "font": "serif", "name": "shop.ru"})
    assert media_reel.check(spec)["ok"] is True
    svg = media_reel.frame_at(spec, 1.0)
    assert media.THEMES["light"]["bg"] in svg
    assert "Georgia" in svg
    assert "#7c5cff" in svg or "accent" in svg


def test_brand_scale_changes_typography():
    small = media_reel.frame_at(_spec(brand={"scale": 0.8}), 1.0)
    big = media_reel.frame_at(_spec(brand={"scale": 1.3}), 1.0)
    assert len(small) < len(big), "масштаб бренда не влияет на кегли"


def test_unknown_kit_values_are_rejected():
    spec = _spec(brand={"theme": "neon", "font": "comic", "scale": 9})
    codes = {issue["code"] for issue in media_reel.check(spec)["issues"]}
    assert {"theme_invalid", "font_invalid", "brand_scale_invalid"} <= codes


def test_brand_validation_is_shared_between_cards_and_reels():
    brand = {"theme": "neon"}
    card = media.build_spec({"title": "Карточка"}, "1x1", brand=brand)
    reel = _spec(brand=brand)
    assert "theme_invalid" in [i["code"] for i in media.validate_spec(card)]
    assert "theme_invalid" in [i["code"] for i in media_reel.check(reel)["issues"]]


def test_defaults_are_dark_theme_and_sans():
    kit = media.brand_kit({})
    assert kit["theme"] == "dark" and kit["font_code"] == "sans"
    assert kit["scale"] == 1.0


# ------------------------------------------------------------------ лимиты
def test_limits_come_from_environment(monkeypatch):
    monkeypatch.setenv("MEDIA_REEL_MAX_DURATION", "180")
    monkeypatch.setenv("MEDIA_REEL_MAX_SCENES", "9")
    config = media_reel.limits()
    assert config["max_duration"] == 180 and config["max_scenes"] == 9
    scenes = [{"template": "chips-flow", "props": {"items": ["а", "б"]}, "duration": 150.0}]
    long_spec = media_reel.build_reel_spec({"title": "Длинный"}, fps=25, scenes=scenes)
    assert "duration_range" not in [i["code"] for i in media_reel.check(long_spec)["issues"]]


def test_default_limit_still_cuts_long_reels():
    scenes = [{"template": "chips-flow", "props": {"items": ["а", "б"]}, "duration": 150.0}]
    long_spec = media_reel.build_reel_spec({"title": "Длинный"}, fps=25, scenes=scenes)
    assert "duration_range" in [i["code"] for i in media_reel.check(long_spec)["issues"]]


def test_broken_limit_value_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("MEDIA_REEL_MAX_DURATION", "не число")
    assert media_reel.limits()["max_duration"] == media_reel.DEFAULT_LIMITS["max_duration"]


def test_scene_count_limit_is_configurable(monkeypatch):
    monkeypatch.setenv("MEDIA_REEL_MAX_SCENES", "2")
    spec = _spec()          # по умолчанию 3 сцены: хук, пункты, финал
    assert "scenes_too_many" in [i["code"] for i in media_reel.check(spec)["issues"]]


# ------------------------------------------------------------------ провайдеры
def test_provider_registry_has_prices_and_capabilities():
    rows = media_voice.PROVIDERS
    assert {"yandex", "mock", "file"} <= set(rows)
    for code, row in rows.items():
        assert "price_per_1000" in row and "handler" in row and "check" in row, code
    assert media_voice.price_per_1000("yandex") > 0
    assert media_voice.price_per_1000("mock") == 0


def test_price_override_changes_estimate(monkeypatch):
    monkeypatch.setenv("MEDIA_TTS_YANDEX_RUB_PER_1000", "2.5")
    assert media_voice.price_per_1000("yandex") == 2.5
    assert media_voice.estimate_cost(1000) == 2.5
    assert media_voice.estimate_cost(400) == 1.0


def test_unknown_provider_is_rejected_with_registry_list():
    """Неизвестный провайдер — отказ со списком реестра.

    Раньше здесь стоял `elevenlabs` как пример «неизвестного»; после этапа H он в реестре,
    поэтому пример заменён на действительно отсутствующий движок. Тест на «список из реестра»
    должен брать того, кого в реестре нет, — иначе он проверяет не то.
    """
    result = media_voice.synthesize("Проверка", provider="deepgram-tts")
    assert result["ok"] is False
    assert "не поддерживается" in result["reason"]
    assert "elevenlabs" in result["reason"]


def test_new_provider_is_added_by_registry_entry(monkeypatch):
    """Новый TTS — запись в реестре: без правки синтеза и без новой ветки."""
    calls: list[str] = []

    def handler(call: dict) -> dict:
        calls.append(call["text"])
        return media_voice._mock(call["text"], call["rate"])

    monkeypatch.setitem(media_voice.PROVIDERS, "custom", {
        "title": "Кастомный TTS", "mode": "local", "price_per_1000": 0.9, "currency": "RUB",
        "needs": [], "languages": ["ru"], "handler": handler,
        "check": media_voice._check_local,
    })
    result = media_voice.synthesize("Новый провайдер работает", provider="custom")
    assert result["ok"] is True and calls
    assert result["cost_rub"] == media_voice.estimate_cost(len(result["spoken_text"]), "custom")
    assert "custom" in media_voice.available()["providers"]


# ------------------------------------------------------------------ API
def test_api_exposes_formats_brand_and_limits():
    body = client.get("/api/v1/media/templates", headers=H).json()
    assert set(body["reels"]["formats"]) == set(media_reel.REEL_FORMATS)
    assert set(body["brand"]["themes"]) == set(media.THEMES)
    assert body["reels"]["limits"]["max_duration"] == media_reel.limits()["max_duration"]


def test_api_accepts_format_and_rejects_unknown():
    created = client.post("/api/v1/media/reels", headers=H,
                          json={"post": POST, "format": "1x1", "render": False})
    assert created.status_code in {200, 201}, created.text
    assert created.json()["spec"]["format"] == "1x1"

    broken = client.post("/api/v1/media/reels", headers=H,
                         json={"post": POST, "format": "21x9", "render": False})
    assert broken.status_code == 422
    assert broken.json()["detail"]["issues"][0]["code"] == "format_unknown"


def test_api_accepts_brand_kit_and_rejects_unknown_theme():
    ok = client.post("/api/v1/media/reels", headers=H,
                     json={"post": POST, "render": False,
                           "brand": {"name": "shop.ru", "theme": "light", "font": "serif"}})
    assert ok.status_code in {200, 201}, ok.text

    bad = client.post("/api/v1/media/reels", headers=H,
                      json={"post": POST, "render": False, "brand": {"theme": "neon"}})
    assert bad.status_code == 422
    assert "theme_invalid" in {i["code"] for i in bad.json()["detail"]["issues"]}


@pytest.mark.skipif(not FFMPEG, reason="нужен ffmpeg: файл не собрать")
def test_square_reel_encodes_with_square_dimensions():
    spec = _spec(fmt="1x1")
    job = media_reel.submit(spec, render=True, limit=6)["reel"]
    assert job.get("video"), job.get("video_error")
    # кадров меньше, чем нужно, поэтому длительность «не сходится» — размеры проверяем отдельно
    assert job["video"]["width"] == 1080 and job["video"]["height"] == 1080


def test_video_checks_use_size_of_format():
    probe = {"duration": 3.0, "bytes": 1000, "faststart": True, "width": 1080, "height": 1080}
    assert media_video.checks(probe, expected_duration=3.0, fps=25, size=(1080, 1080)) == []
    problems = media_video.checks(probe, expected_duration=3.0, fps=25, size=(1920, 1080))
    assert any("ширина" in item for item in problems)
