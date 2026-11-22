"""Ролики 9:16: спека, таймлайн, кадры, кодирование (этап C, ADR 0023).

Здесь проверяется то, что нельзя увидеть на одном скриншоте:

1. **Кадр — функция от времени.** Один и тот же ``t`` даёт побайтово тот же SVG, разные ``t`` —
   разный кадр, титры подсвечивают слово по таймингу, а не «примерно».
2. **Таймлайн сходится.** Сумма сцен равна заявленной длительности, кадры не пересекаются,
   ролик не выходит за лимиты каналов.
3. **Проверки работают до рендера.** 900 кадров дороже, чем арифметика: плохая сцена
   отвергается, а не «рендерится как получилось».
4. **Файл проверен, а не «собрался».** Длительность сверяется с таймлайном, `moov` должен быть
   в начале (иначе лента не начнёт проигрывание), размер — в пределах лимита канала.
"""
from __future__ import annotations

import os
import struct
import time
import zlib
from xml.etree import ElementTree as ET

import pytest
from fastapi.testclient import TestClient

from orbita import media, media_reel, media_storage, media_video
from orbita.main import app

client = TestClient(app)
H = {"X-Actor-Role": "owner"}

POST = {
    "title": "Один договор — 29 площадок",
    "eyebrow": "контент-конвейер",
    "items": ["Один пост — все каналы без копипаста",
              "Лимиты площадок проверяются до публикации"],
    "note": "Ролик собирается из уже согласованного поста.",
    "footer": "posiz.ru",
    "cta": "Смотреть конвейер",
}
BRAND = {"name": "dogovor.expert", "accent": "#7c5cff"}
FFMPEG = media_video.detect()["available"]


@pytest.fixture(autouse=True)
def clean(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_SPOOL_DIR", str(tmp_path / "media"))
    monkeypatch.setenv("MEDIA_STORAGE", "spool")
    monkeypatch.setenv("MEDIA_URL_SECRET", "test-media-url-secret")
    for collection in (media_reel._reels, media_reel._specs, media._assets, media._jobs,
                       media._specs):
        collection.clear_memory()
    yield


def _spec(**kwargs) -> dict:
    return media_reel.build_reel_spec(kwargs.pop("post", POST), brand=kwargs.pop("brand", BRAND),
                                      **kwargs)


def _codes(spec: dict) -> list[str]:
    return [issue["code"] for issue in media_reel.validate_reel_spec(spec)]


def _short_spec(scene_duration: float = 2.0, fps: int = 25) -> dict:
    """Короткий валидный ролик на две сцены: 4 секунды, без «тяжёлых» сцен."""
    return _spec(scenes=[
        {"template": "hook-typed", "duration": scene_duration,
         "props": {"headline": "Короткий хук"}},
        {"template": "cta-card", "duration": scene_duration,
         "props": {"cta": "Читать", "footer": "posiz.ru"}},
    ], fps=fps)


def _solid_png(width: int = 1080, height: int = 1920, color: bytes = b"\x09\x09\x0b") -> bytes:
    """Один сплошной PNG нужного размера — для теста кодирования, без растеризатора."""
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + kind + payload +
                struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    row = b"\x00" + color * width
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) +
            chunk(b"IDAT", zlib.compress(row * height, 9)) + chunk(b"IEND", b""))


# ------------------------------------------------------------------ спека
def test_build_produces_valid_spec():
    spec = _spec()
    assert media_reel.validate_reel_spec(spec) == []
    templates = [scene["template"] for scene in spec["scenes"]]
    assert templates[0] == "hook-typed" and templates[-1] == "cta-card"
    assert spec["spec_version"] == media_reel.SPEC_VERSION
    assert spec["format"] == "9x16"
    assert 4 <= spec["duration"] <= media_reel.MAX_DURATION


def test_duration_follows_text_volume_not_fixed_step():
    """Длительность сцены зависит от объёма текста: иначе длинный хук «не успевает»."""
    short = _spec(post={**POST, "title": "Договор"})
    long = _spec(post={**POST, "title": "Один договор закрывает сразу двадцать девять "
                                            "площадок и снимает ручную работу"})
    hook_short = short["scenes"][0]["duration"]
    hook_long = long["scenes"][0]["duration"]
    assert hook_long > hook_short


def test_unknown_scene_is_rejected():
    spec = _spec()
    spec["scenes"][0]["template"] = "scene-3d-explosion"
    assert "scene_unknown" in _codes(spec)


def test_wrong_fps_is_rejected():
    spec = _spec(fps=17)
    assert "fps_invalid" in _codes(spec)


def test_duration_mismatch_is_rejected():
    spec = _spec()
    spec["duration"] = spec["duration"] + 5
    assert "duration_mismatch" in _codes(spec)


def test_too_short_scene_is_rejected():
    spec = _spec(scenes=[{"template": "hook-typed", "duration": 0.5,
                          "props": {"headline": "Хук"}},
                         {"template": "cta-card", "duration": 4,
                          "props": {"cta": "Читать"}}])
    assert "scene_too_short" in _codes(spec)


def test_hook_word_limit_is_enforced():
    spec = _spec(scenes=[{"template": "hook-typed", "duration": 3,
                          "props": {"headline": "Один два три четыре пять шесть семь восемь "
                                                "девять десять"}},
                         {"template": "cta-card", "duration": 2, "props": {"cta": "Читать"}}])
    assert "hook_too_long" in _codes(spec)


def test_chip_length_limit_is_enforced():
    spec = _spec(scenes=[{"template": "chips-flow", "duration": 4,
                          "props": {"items": ["я" * (media_reel.MAX_CHIP_CHARS + 1)]}},
                         {"template": "cta-card", "duration": 2, "props": {"cta": "Читать"}}])
    assert "chip_too_long" in _codes(spec)


def test_captions_without_text_are_rejected():
    spec = _spec(scenes=[{"template": "captions-stack", "duration": 3, "props": {}},
                         {"template": "cta-card", "duration": 2, "props": {"cta": "Читать"}}])
    assert "prop_required" in _codes(spec)


def test_ad_without_erid_is_rejected_and_marked_when_present():
    bad = media_reel.build_reel_spec(POST, brand=BRAND, compliance={"ad": True})
    assert "erid_required" in _codes(bad)

    good = media_reel.build_reel_spec(POST, brand=BRAND, compliance={
        "ad": True, "erid": "2Vtzqw9ReelErID", "advertiser": "ООО «Тест»", "inn": "7700000000"})
    assert media_reel.validate_reel_spec(good) == []
    svg = media_reel.frame_at(good, good["duration"] - 0.2)
    assert "2Vtzqw9ReelErID" in svg and "Реклама" in svg


# ------------------------------------------------------------------ таймлайн
def test_timeline_is_contiguous_and_sums_to_duration():
    spec = _spec()
    rows = media_reel.timeline(spec)
    assert rows[0]["start"] == 0
    for previous, current in zip(rows, rows[1:]):
        assert current["start"] == previous["end"]
    assert abs(rows[-1]["end"] - spec["duration"]) < 1e-6
    assert sum(row["frames"] for row in rows) == media_reel.frames_count(spec)


def test_frame_is_deterministic_and_time_dependent():
    spec = _spec()
    first = media_reel.frame_at(spec, 1.5)
    assert first == media_reel.frame_at(spec, 1.5)      # тот же кадр — тот же SVG
    assert first != media_reel.frame_at(spec, 6.0)      # разное время — разный кадр


def test_frame_is_valid_svg_of_vertical_size():
    spec = _spec()
    svg = media_reel.frame_at(spec, 0.8)
    root = ET.fromstring(svg)
    assert root.get("width") == "1080" and root.get("height") == "1920"
    assert "dogovor.expert" in svg


def test_frame_beyond_end_shows_last_scene():
    spec = _spec()
    assert media_reel.frame_at(spec, spec["duration"] + 10) == \
        media_reel.frame_at(spec, spec["duration"])
    assert media_reel.frame_at(spec, -5) == media_reel.frame_at(spec, 0)


def test_text_is_escaped_in_frames():
    spec = _spec(post={**POST, "title": "Договор <script>&«кавычки»"})
    svg = media_reel.frame_at(spec, 1.0)
    assert "<script>" not in svg and "&lt;script&gt;" in svg


def test_caption_highlight_moves_with_time():
    spec = _spec(scenes=[{"template": "captions-stack", "duration": 4,
                          "props": {"captions": "Первое слово второе слово третье слово"}},
                         {"template": "cta-card", "duration": 2, "props": {"cta": "Читать"}}],
                 fps=25)
    assert media_reel.validate_reel_spec(spec) == []
    early = media_reel.frame_at(spec, 0.2)
    later = media_reel.frame_at(spec, 2.4)
    assert early != later
    words = spec["scenes"][0]["props"]["words"]
    assert media_reel.word_at(words, 0.0) == 0
    assert media_reel.word_at(words, words[-1]["end"] + 0.1) == len(words) - 1


def test_word_plan_covers_text_and_respects_length():
    words = media_reel.plan_words("Короткое длинноетестовоеслово ещё")
    assert [word["word"] for word in words] == ["Короткое", "длинноетестовоеслово", "ещё"]
    assert words[0]["start"] == 0
    for previous, current in zip(words, words[1:]):
        assert current["start"] == previous["end"]
    long_span = words[1]["end"] - words[1]["start"]
    short_span = words[2]["end"] - words[2]["start"]
    assert long_span > short_span            # длинное слово держится дольше


def test_word_plan_total_matches_reading_speed():
    """Регрессия: план слов растягивал 23 слова на 41 с — титры замирали на первом слове."""
    text = " ".join(["слово"] * 26)
    words = media_reel.plan_words(text, words_per_second=2.6)
    span = words[-1]["end"] - words[0]["start"]
    assert 26 / 2.6 * 0.8 <= span <= 26 / 2.6 * 1.2


def test_captions_window_follows_active_word():
    """Длинная сцена: окно из трёх строк едет за речью, а не показывает начало."""
    text = ("Маркировка рекламы в кадре и в тексте. Ролик собирается из уже согласованного "
            "поста, а не с нуля: сцены и титры считает код.")
    spec = _spec(scenes=[{"template": "captions-stack", "duration": 9,
                          "props": {"captions": text}},
                         {"template": "cta-card", "duration": 2.4,
                          "props": {"cta": "Смотреть"}}])
    words = spec["scenes"][0]["props"]["words"]
    early, late = media_reel.frame_at(spec, 0.4), media_reel.frame_at(spec, 8.0)
    assert early != late
    for moment in (0.4, 4.0, 8.0):
        active = words[media_reel.word_at(words, moment)]["word"]
        assert active in media_reel.frame_at(spec, moment)


def test_storyboard_has_one_shot_per_scene():
    spec = _spec()
    shots = media_reel.storyboard(spec)
    assert len(shots) == len(spec["scenes"])
    assert all(shot["svg"].startswith("<svg") for shot in shots)
    assert [shot["t"] for shot in shots] == sorted(shot["t"] for shot in shots)


def test_cost_estimate_counts_frames_and_rubles():
    spec = _spec(fps=30)
    cost = media_reel.cost_estimate(spec)
    assert cost["frames"] == media_reel.frames_count(spec)
    assert cost["cpu_rub"] > 0 and cost["cpu_seconds"] > 0


def test_spec_hash_changes_with_content():
    first = _spec()
    second = _spec(post={**POST, "title": "Другой заголовок"})
    assert media_reel.spec_hash(first) != media_reel.spec_hash(second)


# ------------------------------------------------------------------ видео
def test_bitrate_fits_channel_limit():
    short = media_video.target_bitrate(10, limit_mb=20)
    long = media_video.target_bitrate(60, limit_mb=20)
    assert long < short
    assert long * 60 / 8 / 1024 / 1024 < 20          # в лимит влезает


def test_probe_bytes_rejects_non_video():
    probe = media_video.probe_bytes(b"not an mp4 at all")
    assert probe["duration"] is None and probe["width"] is None


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg недоступен на узле")
def test_encode_produces_playable_mp4():
    frames = [_solid_png() for _ in range(10)]
    result = media_video.encode(frames, fps=25)
    assert result["ok"], result.get("reason")
    probe = result["probe"]
    assert probe["width"] == 1080 and probe["height"] == 1920
    assert abs(probe["duration"] - 0.4) <= 0.25
    assert probe["faststart"] is True
    assert media_video.checks(probe, expected_duration=0.4, fps=25) == []


def test_checks_catch_defects():
    problems = media_video.checks({"duration": 9.0, "bytes": 30 * 1024 * 1024,
                                   "faststart": False, "width": 720, "height": 1280},
                                  expected_duration=6.0, fps=25)
    text = " ".join(problems)
    assert "длительность" in text and "МБ" in text
    assert "moov" in text and "ширина" in text


def test_encode_without_ffmpeg_fails_honestly(monkeypatch):
    monkeypatch.setattr(media_video, "detect", lambda: {"ffmpeg": None, "ffprobe": None,
                                                        "available": False})
    result = media_video.encode([_solid_png()], fps=25)
    assert result["ok"] is False and "ffmpeg" in result["reason"]


# ------------------------------------------------------------------ задачи
def test_plan_only_does_not_render():
    result = media_reel.submit(_spec(), render=False)
    assert result["reel"]["status"] == "planned"
    assert result["reel"]["cost"]["frames"] > 0


def test_rejected_spec_is_not_rendered():
    spec = _spec()
    spec["scenes"][0]["template"] = "scene-3d"
    result = media_reel.submit(spec)
    assert result["reel"]["status"] == "rejected"
    assert result["asset"] is None


def test_submit_ready_stores_video_and_is_idempotent(monkeypatch):
    if not FFMPEG:
        pytest.skip("ffmpeg недоступен: ролик останется сторибордом")
    spec = _short_spec()
    frames_needed = media_reel.frames_count(spec)
    png = _solid_png()
    monkeypatch.setattr(media_reel, "render_frames",
                        lambda s, limit=None, workers=6: ([png] * frames_needed, "chromium"))
    result = media_reel.submit(spec)
    job = result["reel"]
    assert job["status"] == "ready", job.get("video_error")
    assert job["video_bytes"] > 1024 and job["video_defects"] == []
    stored, mime = media_reel.content(job["id"], "mp4")
    assert mime == "video/mp4" and len(stored) == job["video_bytes"]
    again = media_reel.submit(spec)
    assert again.get("reused") and again["reel"]["id"] == job["id"]


def test_purge_removes_old_reels():
    job = media_reel.submit(_spec(), render=False)["reel"]
    old = time.time() - 30 * 86400
    media_reel._reels.put({**job, "created_at": media_reel._now().fromtimestamp(old)})
    removed = media_reel.purge(ttl_days=7)
    assert removed["removed"] >= 1
    assert media_reel.reel(job["id"]) is None


# ---------------------------------------------------------------------- API
def test_api_reel_plan_and_validation_error():
    response = client.post("/api/v1/media/reels", headers=H,
                           json={"post": POST, "fps": 25, "render": False})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["reel"]["status"] == "planned" and body["reel_id"].startswith("reel_")

    bad = client.post("/api/v1/media/reels", headers=H, json={
        "spec": {"spec_version": "reel.spec.v2", "format": "9x16", "fps": 25, "scenes": []}})
    assert bad.status_code == 422 and bad.json()["detail"]["kind"] == "reel_rejected"


def test_api_frame_endpoint_returns_svg_without_rendering_video():
    reel_id = client.post("/api/v1/media/reels", headers=H,
                          json={"post": POST, "fps": 25, "render": False}).json()["reel_id"]
    frame = client.get(f"/api/v1/media/reels/{reel_id}/frame?t=1.2", headers=H)
    assert frame.status_code == 200
    assert frame.headers["content-type"].startswith("image/svg+xml")
    assert b"<svg" in frame.content


def test_api_video_conflict_when_not_rendered():
    reel_id = client.post("/api/v1/media/reels", headers=H,
                          json={"post": POST, "fps": 25, "render": False}).json()["reel_id"]
    response = client.get(f"/api/v1/media/reels/{reel_id}.mp4", headers=H)
    assert response.status_code == 409
    assert response.json()["detail"]["kind"] == "video_unavailable"


def test_api_ready_reel_video_and_public_link(monkeypatch):
    if not FFMPEG:
        pytest.skip("ffmpeg недоступен: видео не собрать")
    spec = _short_spec()
    frames_needed = media_reel.frames_count(spec)
    png = _solid_png()
    monkeypatch.setattr(media_reel, "render_frames",
                        lambda s, limit=None, workers=6: ([png] * frames_needed, "chromium"))
    created = client.post("/api/v1/media/reels", headers=H,
                          json={"spec": spec, "fps": 25}).json()
    reel_id = created["reel_id"]
    assert created["reel"]["status"] == "ready"

    video = client.get(f"/api/v1/media/reels/{reel_id}.mp4", headers=H)
    assert video.status_code == 200 and video.headers["content-type"] == "video/mp4"
    poster = client.get(f"/api/v1/media/reels/{reel_id}.png", headers=H)
    assert poster.status_code == 200 and poster.headers["content-type"] == "image/png"

    # канал забирает ролик по подписанной ссылке, без наших заголовков
    assert media.best_format(reel_id) == "mp4"
    token = media.signed_token(reel_id, "mp4")
    public = client.get(f"/api/v1/media/public/{token}")
    assert public.status_code == 200 and public.headers["content-type"] == "video/mp4"

    summary = client.get("/api/v1/media/summary", headers=H).json()
    assert summary["reels"]["ready"] >= 1
