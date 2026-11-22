"""Кинематографичная озвучка: провайдеры, подача, паузы и ударения (этап H, ADR 0023).

Что здесь проверяется — и почему именно это:

1. **Провайдеры — данные, а не ветки.** ElevenLabs, SteosVoice и свой TTS-сервер добавляются
   записью в реестр: цена в рублях, языки, режим, выразительность, поддержка ударений.
2. **Подача измеряема.** «Бархатный баритон с драматическими паузами» — это `pause_scale`,
   `speed`, `stability` 35–45 % и `style` 15–25 %. Из этого меняется **длительность ролика**:
   паузы — это время в кадре, а не только «красиво звучит».
3. **Чего движок не умеет — говорит вслух.** SpeechKit не принимает stability/style — они
   попадают в `settings.skipped`, а не исчезают. Нет ключа ElevenLabs — отказ с именем
   переменной, а не тишина под видом озвучки.
4. **Ударения не попадают в кадр.** Метка `+` живёт в тексте для синтеза и вычищается из
   `screen_text`; у провайдеров без поддержки ударений она не уходит и в синтез.
5. **Self-hosted отдельно.** Fish Speech / F5-TTS / Piper — это для тех, кто разворачивает
   модель у себя; клиенту сервиса «бесплатная озвучка» не обещается.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from orbita import media_reel, media_voice
from orbita.main import app

client = TestClient(app)
H = {"X-Actor-Role": "owner"}

POST = {
    "title": "Он приехал в горы Дагестана — и остался навсегда",
    "eyebrow": "История · Дагестан",
    "items": ["Высота 2 000 метров — и тишина", "Дорога занимает четыре часа"],
    "note": "Так начинается история, о которой не рассказывают в путеводителях.",
    "cta": "Смотреть целиком",
}


class _Response:
    """Минимальный ответ urllib: тело, content-type и чтение."""

    def __init__(self, body: bytes, ctype: str = "audio/mpeg"):
        self._body = body
        self.headers = {"content-type": ctype}

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for name in ("ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID", "ELEVENLABS_MODEL",
                 "STEOSVOICE_TOKEN", "STEOSVOICE_VOICE", "STEOSVOICE_API_URL",
                 "MEDIA_LOCAL_TTS_URL", "MEDIA_TTS_PERFORMANCE"):
        monkeypatch.delenv(name, raising=False)
    media_reel._reels.clear_memory()
    media_reel._specs.clear_memory()
    yield


# ------------------------------------------------------------------ реестр
def test_new_providers_are_in_registry_with_prices():
    for code in ("elevenlabs", "steosvoice", "local"):
        assert code in media_voice.PROVIDERS, code
        row = media_voice.PROVIDERS[code]
        assert row["price_per_1000"] >= 0
        assert row["languages"]
        assert callable(row["handler"]) and callable(row["check"])


def test_expressiveness_order_is_data():
    """ElevenLabs выразительнее SpeechKit — это поле реестра, а не мнение в коде."""
    score = {code: row.get("expressiveness", 0)
             for code, row in media_voice.PROVIDERS.items()}
    assert score["elevenlabs"] > score["yandex"]
    assert score["steosvoice"] > score["yandex"]
    assert score["elevenlabs"] == max(score.values())


def test_local_engine_is_self_hosted_not_client_facing(monkeypatch):
    assert media_voice.PROVIDERS["local"]["audience"] == "self-hosted"
    monkeypatch.setenv("MEDIA_LOCAL_TTS_URL", "http://127.0.0.1:9/tts")
    state = media_voice.available()
    assert "local" in state["self_hosted"]
    assert "local" not in state["providers"]


def test_cloud_provider_stays_yandex_for_cost_estimate():
    assert media_voice.cloud_provider() == "yandex"


def test_price_override_by_env(monkeypatch):
    monkeypatch.setenv("MEDIA_TTS_ELEVENLABS_RUB_PER_1000", "2.5")
    assert media_voice.price_per_1000("elevenlabs") == 2.5
    assert media_voice.estimate_cost(1000, "elevenlabs") == 2.5


def test_elevenlabs_models_prefer_multilingual_v2():
    """Multilingual v2 — дефолт: быстрые Turbo/Flash теряют подачу и паузы."""
    row = media_voice.PROVIDERS["elevenlabs"]
    assert row["default_model"] == "eleven_multilingual_v2"
    quality = {code: meta["quality"] for code, meta in media_voice.ELEVENLABS_MODELS.items()}
    assert quality["eleven_multilingual_v2"] == max(quality.values())


# ------------------------------------------------------------------ подача
def test_performance_catalog_has_cinematic_baritone():
    catalog = {row["code"]: row for row in media_voice.performance_catalog()}
    assert {"neutral", "cinematic-baritone", "documentary", "energetic"} <= set(catalog)
    baritone = catalog["cinematic-baritone"]
    # значения из практики: стабильность 35–45 %, выразительность 15–25 %
    assert 0.35 <= baritone["stability"] <= 0.45
    assert 0.15 <= baritone["style"] <= 0.25
    assert baritone["pause_scale"] > 1.0 and baritone["speed"] < 1.0


def test_unknown_performance_is_rejected():
    code, profile, issues = media_voice.resolve_performance("melodrama")
    assert code == "melodrama" and issues[0]["code"] == "performance_unknown"
    assert profile["title"] == media_voice.PERFORMANCES[media_voice.DEFAULT_PERFORMANCE]["title"]


def test_platform_default_performance_from_env(monkeypatch):
    monkeypatch.setenv("MEDIA_TTS_PERFORMANCE", "cinematic-baritone")
    assert media_voice.resolve_performance(None)[0] == "cinematic-baritone"
    monkeypatch.setenv("MEDIA_TTS_PERFORMANCE", "unknown")
    assert media_voice.resolve_performance(None)[0] == media_voice.DEFAULT_PERFORMANCE


def test_pauses_come_from_punctuation_not_from_commas():
    text = "Он приехал — и остался. Это было… неожиданно! А потом, почти шёпотом, сказал."
    segments = media_voice.pause_plan(text, performance="neutral")
    assert [segment["text"][:12] for segment in segments][0].startswith("Он приехал")
    assert len(segments) == 5
    assert all(segment["pause"] >= 0 for segment in segments)
    # запятые не создают пауз: иначе титры «плывут» на каждом перечислении
    assert not any(segment["text"].endswith(",") for segment in segments)


def test_cinematic_pauses_are_longer_than_neutral():
    text = "Он приехал — и остался. Это было неожиданно!"
    neutral = media_voice.pauses_seconds(text, performance="neutral")
    cinematic = media_voice.pauses_seconds(text, performance="cinematic-baritone")
    assert cinematic > neutral > 0


def test_spec_duration_grows_with_performance():
    neutral = media_reel.build_reel_spec(POST, fps=30, performance="neutral")
    cinematic = media_reel.build_reel_spec(POST, fps=30, performance="cinematic-baritone")
    energetic = media_reel.build_reel_spec(POST, fps=30, performance="energetic")
    assert cinematic["performance"] == "cinematic-baritone"
    assert cinematic["duration"] > neutral["duration"] > energetic["duration"]


def test_spec_with_unknown_performance_fails_validation():
    spec = media_reel.build_reel_spec(POST, fps=30)
    spec["performance"] = "melodrama"
    codes = [issue["code"] for issue in media_reel.validate_reel_spec(spec)]
    assert "performance_unknown" in codes


def test_default_performance_does_not_change_old_timings():
    """По умолчанию — нейтральная подача: иначе длина всех прежних роликов поехала бы."""
    spec = media_reel.build_reel_spec(POST, fps=30)
    assert spec["performance"] == "neutral"


# ------------------------------------------------------------------ настройки
def test_elevenlabs_gets_stability_and_style():
    state = media_voice.settings_for("elevenlabs", performance="cinematic-baritone")
    assert state["ok"] is True
    assert state["applied"]["stability"] == pytest.approx(0.4)
    assert state["applied"]["style"] == pytest.approx(0.2)
    assert state["applied"]["model"] == "eleven_multilingual_v2"
    assert state["skipped"] == []


def test_speechkit_reports_skipped_settings_instead_of_dropping_them():
    state = media_voice.settings_for("yandex", performance="cinematic-baritone")
    assert set(state["skipped"]) == {"stability", "style"}
    assert "speed" in state["applied"]


def test_settings_out_of_range_are_refused():
    state = media_voice.settings_for("elevenlabs", performance="neutral", stability=1.4)
    assert state["ok"] is False
    assert state["issues"][0]["code"] == "setting_out_of_range"


def test_custom_values_override_performance_profile():
    state = media_voice.settings_for("elevenlabs", performance="cinematic-baritone",
                                     stability=0.35, style=0.15)
    assert state["applied"]["stability"] == 0.35 and state["applied"]["style"] == 0.15


# ------------------------------------------------------------------ ударения
def test_stress_marks_are_stripped_for_screen():
    assert media_voice.strip_stress("в горах Дагест+ана") == "в горах Дагестана"
    assert media_voice.apply_stress("в горах Дагестана", {"Дагестана": "Дагест+ана"}) \
        == "в горах Дагест+ана"


def test_marks_do_not_reach_provider_without_stress_support():
    """SpeechKit не понимает `+`: в синтез уходит чистый текст, иначе услышим «плюс»."""
    result = media_voice.synthesize("в горах Дагест+ана", provider="mock",
                                    stress={"Дагестана": "Дагест+ана"})
    assert result["ok"] is True
    assert "+" not in result["spoken_text"]
    assert result["screen_text"] == "в горах Дагестана"


def test_marks_reach_provider_with_stress_support(monkeypatch):
    seen: list[str] = []

    def handler(call: dict) -> dict:
        seen.append(call["text"])
        return {"ok": False, "reason": "тест: только проверяем текст"}

    monkeypatch.setitem(media_voice.PROVIDERS, "stress-tts", {
        "title": "Движок с ударениями", "mode": "cloud", "audience": "client",
        "price_per_1000": 0.5, "currency": "RUB", "needs": [], "languages": ["ru"],
        "settings": ["speed"], "expressiveness": 4, "stress": True, "clone": False,
        "handler": handler, "check": media_voice._check_local,
    })
    media_voice.synthesize("Дагест+ана", provider="stress-tts")
    assert seen and seen[0] == "Дагест+ана"


# ------------------------------------------------------------------ провайдеры
def test_elevenlabs_without_key_is_honest():
    result = media_voice.synthesize("Проверка", provider="elevenlabs")
    assert result["ok"] is False
    assert "ELEVENLABS_API_KEY" in result["reason"]


def test_elevenlabs_requires_voice_id_not_library_name(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "key")
    result = media_voice.synthesize("Проверка", provider="elevenlabs", voice="george")
    assert result["ok"] is False
    assert "id голоса" in result["reason"]


def test_elevenlabs_request_shape(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "key")
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "voice_abc")
    captured: dict = {}

    def fake_urlopen(request, timeout=0):
        captured["url"] = request.full_url
        captured["body"] = request.data.decode("utf-8")
        captured["headers"] = {key.lower(): value for key, value in request.header_items()}
        return _Response(b"ID3fake-mp3")

    monkeypatch.setattr(media_voice.urllib.request, "urlopen", fake_urlopen)
    result = media_voice.synthesize("Он приехал — и остался.", provider="elevenlabs",
                                    performance="cinematic-baritone")
    assert result["ok"] is True
    assert "/text-to-speech/voice_abc" in captured["url"]
    assert "eleven_multilingual_v2" in captured["body"]
    assert '"stability": 0.4' in captured["body"] and '"style": 0.2' in captured["body"]
    assert captured["headers"].get("xi-api-key") == "key"
    assert result["settings"]["applied"]["model"] == "eleven_multilingual_v2"


def test_steosvoice_without_token_is_honest():
    result = media_voice.synthesize("Проверка", provider="steosvoice")
    assert result["ok"] is False and "STEOSVOICE_TOKEN" in result["reason"]


def test_steosvoice_accepts_audio_and_json_link(monkeypatch):
    monkeypatch.setenv("STEOSVOICE_TOKEN", "token")
    monkeypatch.setenv("STEOSVOICE_VOICE", "baritone")
    bodies = [b'{"status":"success","path":"https://cdn.example/voice.mp3"}', b"ID3real"]
    calls: list[str] = []

    def fake_urlopen(request, timeout=0):
        calls.append(getattr(request, "full_url", str(request)))
        if len(calls) == 1:
            return _Response(bodies[0], "application/json")
        return _Response(bodies[1], "audio/mpeg")

    monkeypatch.setattr(media_voice.urllib.request, "urlopen", fake_urlopen)
    result = media_voice.synthesize("Он приехал", provider="steosvoice")
    assert result["ok"] is True
    assert calls[1] == "https://cdn.example/voice.mp3"


def test_local_tts_needs_endpoint_and_explains_audience():
    result = media_voice.synthesize("Проверка", provider="local")
    assert result["ok"] is False
    assert "MEDIA_LOCAL_TTS_URL" in result["reason"]
    assert "клиенту сервиса" in result["reason"]


def test_local_tts_posts_json(monkeypatch):
    monkeypatch.setenv("MEDIA_LOCAL_TTS_URL", "http://127.0.0.1:8123/tts")
    captured: dict = {}

    def fake_urlopen(request, timeout=0):
        captured["url"] = request.full_url
        captured["body"] = (request.data or b"").decode("utf-8")
        return _Response(b"RIFFfake-wav", "audio/wav")

    monkeypatch.setattr(media_voice.urllib.request, "urlopen", fake_urlopen)
    result = media_voice.synthesize("Проверка", provider="local",
                                    performance="documentary")
    assert result["ok"] is True
    assert captured["url"].endswith("/tts")
    assert '"speed": 0.9' in captured["body"]


def test_synthesize_reports_performance_and_pauses():
    result = media_voice.synthesize("Он приехал — и остался. Это было неожиданно!",
                                    provider="mock", performance="cinematic-baritone")
    assert result["ok"] is True
    assert result["performance"] == "cinematic-baritone"
    assert result["pause_seconds"] > 0
    assert result["rate"] == pytest.approx(0.95)


def test_unknown_performance_in_voice_is_refused():
    result = media_voice.synthesize("Проверка", provider="mock", performance="melodrama")
    assert result["ok"] is False
    assert result["issues"][0]["code"] == "performance_unknown"


# ------------------------------------------------------------------ API
def test_api_voice_catalog_separates_self_hosted():
    body = client.get("/api/v1/media/voice", headers=H).json()
    assert "mock" in body["providers"]
    assert isinstance(body["self_hosted"], list)
    assert [row["code"] for row in body["performances"]][0] == "cinematic-baritone"
    assert body["settings_ranges"]["stability"] == [0.0, 1.0]


def test_api_reel_records_performance():
    response = client.post("/api/v1/media/reels", headers=H,
                           json={"post": {"title": "Он приехал в горы — и остался"},
                                 "performance": "cinematic-baritone", "render": False})
    assert response.status_code == 201
    assert response.json()["spec"]["performance"] == "cinematic-baritone"


def test_api_rejects_unknown_performance():
    response = client.post("/api/v1/media/reels", headers=H,
                           json={"post": {"title": "Он приехал в горы"},
                                 "performance": "melodrama", "render": False})
    assert response.status_code == 422
    assert [issue["code"] for issue in response.json()["detail"]["issues"]] == \
        ["performance_unknown"]


def test_api_voice_performance_reaches_spec_and_synthesis():
    response = client.post("/api/v1/media/reels", headers=H,
                           json={"post": {"title": "Он приехал в горы — и остался"},
                                 "voice": {"provider": "mock",
                                           "performance": "cinematic-baritone"},
                                 "render": False})
    body = response.json()
    assert response.status_code == 201
    assert body["spec"]["performance"] == "cinematic-baritone"
    assert body["spec"]["voice"]["performance"] == "cinematic-baritone"
    assert body["spec"]["voice"]["pause_seconds"] > 0


def test_api_voice_without_key_returns_honest_reason():
    response = client.post("/api/v1/media/reels", headers=H,
                           json={"post": {"title": "Он приехал в горы"},
                                 "voice": {"provider": "elevenlabs",
                                           "performance": "cinematic-baritone"},
                                 "render": False})
    body = response.json()
    assert response.status_code == 201
    assert body["reel"]["voice_ok"] is False
    assert "ELEVENLABS_API_KEY" in body["reel"]["voice"]["reason"]


def test_api_templates_include_performances():
    reels = client.get("/api/v1/media/templates", headers=H).json()["reels"]
    assert "cinematic-baritone" in [row["code"] for row in reels["performances"]]
    assert reels["default_performance"] == media_voice.DEFAULT_PERFORMANCE
