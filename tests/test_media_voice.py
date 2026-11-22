"""Озвучка ролика: произношение, смета, тайминги, аудио в MP4 (этап D, ADR 0023).

Озвучка — единственная стадия, где ошибка слышна, а не видна. Поэтому проверяется не «файл
создался», а четыре обещания:

1. **Бренд читается правильно.** «38-ФЗ», «erid», «ОРД» уходят в синтез как слова, а не как
   аббревиатуры, при этом в кадр и в субтитры попадает исходное написание.
2. **Смета известна до запроса.** Знаки × тариф, в рублях — как смета канала перед публикацией.
3. **Титры синхронны.** Тайминги слов монотонны, покрывают весь текст и заканчиваются вместе с
   аудио; длинное слово держится дольше короткого.
4. **Нет ключа — нет озвучки.** Провайдер `yandex` без ключа возвращает причину, а не тишину под
   видом голоса; `mock`/`file` проверяют тот же путь кодирования.
"""
from __future__ import annotations

import struct
import zlib

import pytest
from fastapi.testclient import TestClient

from orbita import media, media_reel, media_storage, media_video, media_voice
from orbita.main import app

client = TestClient(app)
H = {"X-Actor-Role": "owner"}

FFMPEG = media_video.detect()["available"]
NARRATION = ("Один договор закрывает сразу двадцать девять площадок. "
             "Маркировка рекламы ставится до публикации, а не после.")


@pytest.fixture(autouse=True)
def clean(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_SPOOL_DIR", str(tmp_path / "media"))
    monkeypatch.setenv("MEDIA_STORAGE", "spool")
    monkeypatch.setenv("MEDIA_URL_SECRET", "test-media-url-secret")
    monkeypatch.delenv("YANDEX_TTS_API_KEY", raising=False)
    monkeypatch.delenv("YANDEX_TTS_IAM_TOKEN", raising=False)
    for collection in (media_reel._reels, media_reel._specs, media._assets):
        collection.clear_memory()
    yield


def _solid_png(width: int = 1080, height: int = 1920) -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + kind + payload +
                struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    row = b"\x00" + b"\x09\x09\x0b" * width
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) +
            chunk(b"IDAT", zlib.compress(row * height, 6)) + chunk(b"IEND", b""))


# ------------------------------------------------------- произношение и смета
def test_pronunciation_dictionary_covers_brand_terms():
    spoken = media_voice.normalize("Реклама erid, 38-ФЗ, ОРД, ИНН и 100% договоров")
    assert "erid" not in spoken and "эрид" in spoken
    assert "38-ФЗ" not in spoken and "тридцать восемь эф зэ" in spoken
    assert "о эр дэ" in spoken and "и эн эн" in spoken
    assert "100 процентов" in spoken


def test_identifiers_stay_on_screen_but_are_not_read_aloud():
    """В кадре — исходное написание и хэш; в синтез хэш не уходит, звучит только «эрид»."""
    spec = media_reel.build_reel_spec({"title": "Маркировка erid до публикации"},
                                      compliance={"ad": True, "erid": "2Vtzqw9DemoErID",
                                                  "advertiser": "ООО «Тест»"})
    assert "2Vtzqw9DemoErID" in media_reel.frame_at(spec, 1.0)
    spoken = media_voice.drop_identifiers(media_voice.normalize(
        "Реклама erid 2Vtzqw9DemoErID, ООО «Тест»"))
    assert "2Vtzqw9DemoErID" not in spoken and "эрид" in spoken


def test_cost_is_chars_times_tariff():
    assert media_voice.estimate_cost(1000) == pytest.approx(media_voice.RUB_PER_1000_CHARS)
    assert media_voice.estimate_cost(600) == pytest.approx(0.805, abs=0.001)
    assert media_voice.estimate_cost(0) == 0


def test_yandex_without_key_is_honest():
    state = media_voice.available()
    assert state["yandex"]["available"] is False
    assert "YANDEX_TTS_API_KEY" in state["yandex"]["reason"]
    assert "yandex" not in state["providers"]
    result = media_voice.synthesize("Текст", provider="yandex")
    assert result["ok"] is False and "ключ" in result["reason"] or \
        "YANDEX" in result["reason"]


def test_unknown_provider_is_rejected():
    result = media_voice.synthesize("Текст", provider="elevenlabs-cloud")
    assert result["ok"] is False and "не поддерживается" in result["reason"]


def test_empty_and_too_long_texts_are_rejected():
    assert media_voice.synthesize("   ")["ok"] is False
    long_text = "слово " * (media_voice.MAX_CHARS // 3)
    assert media_voice.synthesize(long_text)["ok"] is False


# ------------------------------------------------------------------ тайминги
def test_align_is_monotonic_and_ends_with_audio():
    words = media_voice.align(NARRATION, 8.0)
    assert [w["word"] for w in words][:2] == ["Один", "договор"]
    assert words[0]["start"] == 0
    for previous, current in zip(words, words[1:]):
        assert current["start"] == previous["end"]
    assert words[-1]["end"] == 8.0            # финальный кадр не «молчит»


def test_long_word_lasts_longer():
    words = media_voice.align("и длинноетестовоеслово", 3.0)
    short_span = words[0]["end"] - words[0]["start"]
    long_span = words[1]["end"] - words[1]["start"]
    assert long_span > short_span


def test_srt_starts_at_first_word_and_ends_at_last():
    words = media_voice.align(NARRATION, 6.0)
    text = media_voice.srt(words)
    assert text.startswith("1\n00:00:00,000 -->")
    blocks = [block for block in text.split("\n\n") if block.strip()]
    assert len(blocks) >= 2
    assert blocks[-1].splitlines()[-1].strip().endswith(".")


# ------------------------------------------------------- синтез (mock/file)
@pytest.mark.skipif(not FFMPEG, reason="ffmpeg недоступен: mock-озвучку не собрать")
def test_mock_synthesis_returns_audio_words_and_cost():
    result = media_voice.synthesize(NARRATION, provider="mock")
    assert result["ok"] is True and result["provider"] == "mock"
    assert len(result["audio"]) > 1000
    assert result["duration"] > 1
    # mock — локальный провайдер: он бесплатный, а «как в проде» считается по облаку
    assert result["cost_rub"] == pytest.approx(media_voice.estimate_cost(result["chars"], "mock"))
    assert result["cost_rub"] == 0
    assert result["cost_cloud_rub"] == pytest.approx(media_voice.estimate_cost(result["chars"]))
    assert [w["word"] for w in result["words"]][:2] == ["Один", "договор"]


def test_file_provider_reads_wav_and_reports_missing(tmp_path):
    source = tmp_path / "voice.mp3"
    source.write_bytes(b"ID3\x03\x00\x00\x00" + b"\x00" * 512)
    result = media_voice.synthesize("Текст", provider="file", path=str(source))
    assert result["ok"] is True and result["mime"] == "audio/mpeg"
    missing = media_voice.synthesize("Текст", provider="file", path=str(tmp_path / "нет.mp3"))
    assert missing["ok"] is False and "не найден" in missing["reason"]


# ------------------------------------------------- время ролика под аудио
def test_apply_voice_stretches_timeline_to_audio():
    spec = media_reel.build_reel_spec({"title": "Хук", "footer": "posiz.ru",
                                       "items": ["Первый пункт"]}, fps=25)
    before = spec["duration"]
    voice = {"duration": before * 2, "words": media_voice.align(NARRATION, before * 2),
             "provider": "mock", "chars": 100, "cost_rub": 0.13}
    stretched = media_reel.apply_voice(spec, voice)
    assert stretched["duration"] > before
    assert abs(stretched["duration"] - before * 2) < 0.3
    assert stretched["voice"]["provider"] == "mock"


def test_apply_voice_keeps_minimum_scene_duration():
    """Быстрая речь не имеет права сжать сцену до мигания."""
    spec = media_reel.build_reel_spec({"title": "Хук", "footer": "posiz.ru",
                                       "items": ["Пункт"]}, fps=25)
    voice = {"duration": 3.0, "words": media_voice.align("короткая речь", 3.0),
             "provider": "mock", "chars": 20, "cost_rub": 0.03}
    stretched = media_reel.apply_voice(spec, voice)
    for scene in stretched["scenes"]:
        floor = media_reel.SCENES[scene["template"]]["min_seconds"]
        assert scene["duration"] >= floor - 0.05
    assert media_reel.validate_reel_spec(stretched) == []


# ------------------------------------------------------------- конвейер/API
def _short_spec() -> dict:
    return media_reel.build_reel_spec({
        "title": "Ролик с озвучкой", "footer": "posiz.ru",
        "items": ["Первый пункт", "Второй пункт"], "cta": "Смотреть"}, fps=25)


def test_submit_with_voice_stores_audio_and_subtitles(monkeypatch):
    if not FFMPEG:
        pytest.skip("ffmpeg недоступен")
    spec = _short_spec()
    voice = media_voice.synthesize(NARRATION, provider="mock")
    assert voice["ok"]
    spec = media_reel.apply_voice(spec, voice)
    frames_needed = media_reel.frames_count(spec)
    png = _solid_png()
    monkeypatch.setattr(media_reel, "render_frames",
                        lambda s, limit=None, workers=6: ([png] * frames_needed, "chromium"))
    job = media_reel.submit(spec, voice={"provider": "mock", "text": NARRATION})["reel"]
    assert job["status"] == "ready", job.get("video_error") or job.get("video_defects")
    assert job["audio_bytes"] > 1000 and job["audio_key"].endswith(".mp3")
    assert job["voice"]["chars"] > 0
    assert job["voice"]["cost_rub"] == 0                      # mock: локальная тишина
    assert job["voice"]["cost_cloud_rub"] > 0                 # «как в проде» по SpeechKit
    audio, mime = media_reel.content(job["id"], "mp3")
    assert mime == "audio/mpeg" and audio.startswith(b"ID3")
    subtitles, srt_mime = media_reel.content(job["id"], "srt")
    assert srt_mime == "application/x-subrip" and b"-->" in subtitles


def test_api_voice_endpoint_and_audio_link(monkeypatch):
    if not FFMPEG:
        pytest.skip("ffmpeg недоступен")
    spec = _short_spec()
    voice = media_voice.synthesize(NARRATION, provider="mock")
    spec = media_reel.apply_voice(spec, voice)
    frames_needed = media_reel.frames_count(spec)
    png = _solid_png()
    monkeypatch.setattr(media_reel, "render_frames",
                        lambda s, limit=None, workers=6: ([png] * frames_needed, "chromium"))
    body = {"spec": spec, "fps": 25, "voice": {"provider": "mock", "text": NARRATION}}
    created = client.post("/api/v1/media/reels", headers=H, json=body)
    assert created.status_code == 201, created.text
    payload = created.json()
    assert payload["reel"]["status"] == "ready"
    assert payload["voice_state"]["rub_per_1000_chars"] > 0
    reel_id = payload["reel_id"]

    audio = client.get(f"/api/v1/media/reels/{reel_id}.mp3", headers=H)
    assert audio.status_code == 200 and audio.headers["content-type"] == "audio/mpeg"
    subs = client.get(f"/api/v1/media/reels/{reel_id}.srt", headers=H)
    assert subs.status_code == 200 and b"-->" in subs.content

    token = media.signed_token(reel_id, "mp3")
    public = client.get(f"/api/v1/media/public/{token}")
    assert public.status_code == 200 and public.headers["content-type"] == "audio/mpeg"


def test_voice_summary_is_visible():
    summary = client.get("/api/v1/media/summary", headers=H).json()
    assert "voice" in summary and "providers" in summary["voice"]
    assert summary["voice"]["alignment"] in {"proportional", "faster-whisper"}
