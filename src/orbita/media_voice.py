"""Озвучка ролика: текст диктора → аудио + тайминги слов (этап D, ADR 0023).

Что здесь решается, а не «вызывается TTS»:

**1. Как бренд звучит вслух.** «38-ФЗ», «erid», «ЕРИР», «ИНН» модель прочитает как угодно, а
диктор — как получится. Поэтому текст для синтеза проходит через словарь произношения
(:func:`normalize`), а в кадр и в подпись уходит **исходное** написание. Это единственный способ
не получить в ролике «тридцать восемь тире эф зэ» на экране.

**2. Сколько стоит.** SpeechKit тарифицируется за знаки; смета считается до запроса, в рублях,
и попадает в задачу ролика — как смета канала перед публикацией.

**3. Как синхронизировать титры.** Настоящее выравнивание (faster-whisper) — это зависимость
на 1,5 ГБ моделей, поэтому оно опционально: если распознавание доступно, тайминги берутся из
него, иначе слова раскладываются по длительности готового аудио пропорционально их длине. Второй
вариант хуже на ±120 мс, но работает всегда и не превращает ролик в «титры отстают на секунду».

**4. Чего нет — того нет честно.** Без ключа SpeechKit провайдер ``yandex`` возвращает
``ok: False`` и причину, а не тишину под видом озвучки. В песочнице и тестах работают ``mock``
(тишина нужной длины) и ``file`` (готовый файл) — тот же путь кодирования, что в проде.
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import media_video

#: тариф Yandex SpeechKit (синтез, API v1): 1 342 ₽ за 1 млн знаков с НДС
RUB_PER_1000_CHARS = 1.342
#: словарь произношения: слева — как написано, справа — как читается вслух
PRONUNCIATION: dict[str, str] = {
    "38-ФЗ": "тридцать восемь эф зэ",
    "152-ФЗ": "сто пятьдесят два эф зэ",
    "erid": "эрид",
    "ЕРИР": "ЕРИР",
    "ОРД": "о эр дэ",
    "ИНН": "и эн эн",
    "ОГРН": "о гэ эр эн",
    "ФНС": "эф эн эс",
    "НДС": "эн дэ эс",
    "СБИС": "сбис",
    "УПД": "у пэ дэ",
    "API": "а пи ай",
    "SaaS": "саас",
    "CRM": "си эр эм",
    "SEO": "с э о",
    "KPI": "ка пи ай",
    # символы заменяем с пробелами с двух сторон: иначе «ОРД&erid» склеивается в «иэрид»,
    # а лишние пробелы всё равно схлопываются ниже
    "₽": " рублей ",
    "№": " номер ",
    "%": " процентов ",
    "&": " и ",
}
DEFAULT_VOICE = "ermil"
MAX_CHARS = 3000
#: темп чтения по умолчанию, если длительность аудио неизвестна
FALLBACK_WORDS_PER_SECOND = 2.6
MIN_WORD_SECONDS = 0.18


def normalize(text: str) -> str:
    """Текст для синтеза: словарь произношения, без разметки и лишних пробелов."""
    result = str(text or "")
    for written, spoken in PRONUNCIATION.items():
        result = result.replace(written, spoken)
    for char in ("*", "_", "#", "`", "~", "|"):
        result = result.replace(char, " ")
    return " ".join(result.split())


#: идентификаторы (erid-хэши, номера) вслух не читаем: «эрид» — да, «2Vtzqw9X» — нет
_IDENTIFIER = re.compile(r"\b(?=\w*\d)(?=\w*[A-Za-z])[\w-]{8,}\b")


def drop_identifiers(text: str) -> str:
    """Убрать из текста для синтеза машинные идентификаторы.

    Диктор, читающий вслух «два вэ тэ зэт кью…», — это не маркировка, а издевательство.
    Маркировка остаётся в кадре и в описании; вслух звучит только слово «эрид».
    """
    return " ".join(_IDENTIFIER.sub(" ", text).split())


def price_per_1000(provider: str = "yandex") -> float:
    """Цена провайдера за 1000 знаков — из реестра, с переопределением окружением.

    ``MEDIA_TTS_YANDEX_RUB_PER_1000`` меняет тариф без деплоя: провайдеры пересматривают
    цены, а смета клиента не должна зависеть от даты релиза кода.
    """
    row = PROVIDERS.get(provider) or {}
    raw = os.getenv(f"MEDIA_TTS_{provider.upper()}_RUB_PER_1000", "").strip()
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return float(row.get("price_per_1000") or 0.0)


def estimate_cost(chars: int, provider: str = "yandex") -> float:
    """Смета озвучки в рублях с НДС — до запроса, чтобы её можно было показать клиенту."""
    return round(max(0, chars) / 1000 * price_per_1000(provider), 3)


def cloud_provider() -> str | None:
    """Основной облачный провайдер из реестра: по нему считается «смета как в проде».

    В песочнице и демо озвучка часто локальная (`mock`/`file`) и стоит 0 ₽, но клиенту нужно
    видеть, во сколько обойдётся боевой синтез — поэтому в задаче ролика две цифры, а не одна.
    """
    return next((code for code, row in PROVIDERS.items() if row.get("mode") == "cloud"), None)


def yandex_configured() -> dict:
    """Ключ SpeechKit: ``YANDEX_TTS_API_KEY`` либо ``YANDEX_TTS_IAM_TOKEN``."""
    api_key = (os.getenv("YANDEX_TTS_API_KEY") or "").strip()
    iam = (os.getenv("YANDEX_TTS_IAM_TOKEN") or "").strip()
    folder = (os.getenv("YANDEX_TTS_FOLDER_ID") or "").strip()
    if not api_key and not iam:
        return {"available": False,
                "reason": "нет YANDEX_TTS_API_KEY / YANDEX_TTS_IAM_TOKEN: озвучка недоступна"}
    return {"available": True, "mode": "api_key" if api_key else "iam", "folder": folder,
            "reason": None}


def elevenlabs_configured() -> dict:
    """Ключ ElevenLabs: ``ELEVENLABS_API_KEY``; голос задаётся ``ELEVENLABS_VOICE_ID`` или запросом."""
    key = (os.getenv("ELEVENLABS_API_KEY") or "").strip()
    model = (os.getenv("ELEVENLABS_MODEL") or "eleven_multilingual_v2").strip()
    voice = (os.getenv("ELEVENLABS_VOICE_ID") or "").strip()
    if not key:
        return {"available": False, "model": model,
                "reason": "нет ELEVENLABS_API_KEY: кинематографичная озвучка недоступна"}
    return {"available": True, "model": model, "voice": voice or None, "reason": None}


def steosvoice_configured() -> dict:
    """Ключ SteosVoice: ``STEOSVOICE_TOKEN`` и голос ``STEOSVOICE_VOICE``."""
    token = (os.getenv("STEOSVOICE_TOKEN") or "").strip()
    voice = (os.getenv("STEOSVOICE_VOICE") or "").strip()
    url = (os.getenv("STEOSVOICE_API_URL") or "https://api.steosvoice.com/api/v1/synthesis").strip()
    if not token:
        return {"available": False, "url": url,
                "reason": "нет STEOSVOICE_TOKEN: русская озвучка с ударениями недоступна"}
    return {"available": True, "url": url, "voice": voice or None, "reason": None}


def local_tts_configured() -> dict:
    """Свой TTS-сервер (Fish Speech / F5-TTS / Piper): ``MEDIA_LOCAL_TTS_URL``."""
    url = (os.getenv("MEDIA_LOCAL_TTS_URL") or "").strip()
    if not url:
        return {"available": False,
                "reason": ("нет MEDIA_LOCAL_TTS_URL: локальный синтез — для тех, кто "
                           "разворачивает модель сам, клиенту сервиса он не предлагается")}
    return {"available": True, "url": url, "reason": None}


def available() -> dict:
    """Что реально доступно на узле: провайдеры, ключи, подачи, ffmpeg для длительности.

    Self-hosted движки (Fish Speech, F5-TTS, Piper) показываются **отдельным списком**: они
    нужны тем, кто разворачивает модель у себя, а не клиенту сервиса — иначе витрина обещает
    «бесплатную озвучку», которой у клиента нет.
    """
    states = {"yandex": yandex_configured(), "elevenlabs": elevenlabs_configured(),
              "steosvoice": steosvoice_configured(), "local": local_tts_configured()}
    ready = [code for code, row in PROVIDERS.items() if row["check"](states)]
    audience = {code: row.get("audience", "client") for code, row in PROVIDERS.items()}
    return {"providers": [code for code in ready if audience.get(code) in {"client", "internal"}],
            "self_hosted": [code for code in ready if audience.get(code) == "self-hosted"],
            "internal": [code for code in ready if audience.get(code) == "internal"],
            "yandex": states["yandex"], "elevenlabs": states["elevenlabs"],
            "steosvoice": states["steosvoice"], "local": states["local"],
            "registry": [{key: value for key, value in row.items()
                          if key not in {"handler", "check"}}
                         for code, row in PROVIDERS.items() if row["check"](states)],
            "prices": {code: price_per_1000(code) for code in PROVIDERS},
            "ffmpeg": media_video.detect()["available"],
            "default_voice": DEFAULT_VOICE,
            "cloud_provider": cloud_provider(),
            "rub_per_1000_chars": RUB_PER_1000_CHARS,
            "performances": performance_catalog(),
            "default_performance": DEFAULT_PERFORMANCE,
            "models": ELEVENLABS_MODELS,
            "voice_presets": VOICE_PRESETS,
            "alignment": "faster-whisper" if _whisper_available() else "proportional"}


def _whisper_available() -> bool:
    try:
        import faster_whisper  # noqa: F401, PLC0415 — необязательная зависимость
        return True
    except Exception:                                     # noqa: BLE001
        return False


# ------------------------------------------------------------------ подача
#: Подача — это данные, а не «промпт в никуда». Кинематографичный баритон отличается от
#: нейтрального диктора тремя измеримыми вещами: паузы длиннее (pause_scale), темп чуть ниже
#: (speed) и у провайдера — конкретные значения выразительности (stability/style). Из этого
#: набора меняется и длительность сцен: драматическая пауза — это время в кадре, а не только
#: «красиво звучит».
PERFORMANCES: dict[str, dict] = {
    "neutral": {
        "title": "Нейтральный диктор",
        "pause_scale": 1.0, "speed": 1.0, "stability": 0.5, "style": 0.0,
        "description": "Ровная подача без драмы: подходит для инструкций и новостей",
    },
    "cinematic-baritone": {
        "title": "Кинематографичный баритон",
        "pause_scale": 1.45, "speed": 0.95, "stability": 0.4, "style": 0.2,
        "description": ("Глубокая подача с драматическими паузами: стабильность 35–45 %, "
                        "выразительность 15–25 % — выше 70 % голос становится монотонным"),
    },
    "documentary": {
        "title": "Документальный рассказ",
        "pause_scale": 1.2, "speed": 0.9, "stability": 0.55, "style": 0.15,
        "description": "Медленнее и спокойнее: даты, имена, длинные перечисления",
    },
    "energetic": {
        "title": "Энергичный продающий",
        "pause_scale": 0.85, "speed": 1.08, "stability": 0.35, "style": 0.35,
        "description": "Короче паузы, быстрее темп: акции, запуски, короткие офферы",
    },
}
#: подача по умолчанию — нейтральная: кинематографичный баритон меняет длительность ролика
#: на 10–15 %, и включать его без запроса клиента нельзя. Платформенный дефолт задаётся
#: `MEDIA_TTS_PERFORMANCE` (например, `cinematic-baritone` для сторителлинг-аккаунтов)
DEFAULT_PERFORMANCE = "neutral"
#: границы настроек, за которые движок не пускает: это не «вкус», а диапазоны, где голос
#: перестаёт быть собой (стабильность 100 % — робот, выразительность 100 % — крик)
SETTINGS_RANGES = {"stability": (0.0, 1.0), "style": (0.0, 1.0), "similarity_boost": (0.0, 1.0)}
#: модели ElevenLabs: multilingual v2 передаёт интонации и паузы на русском лучше Turbo/Flash —
#: быстрые модели экономят задержку, но теряют подачу, поэтому выбор модели — поле запроса
ELEVENLABS_MODELS = {
    "eleven_multilingual_v2": {"title": "Multilingual v2", "languages": "многоязычная",
                               "quality": 5, "latency": "обычная"},
    "eleven_turbo_v2_5": {"title": "Turbo v2.5", "languages": "многоязычная",
                          "quality": 3, "latency": "низкая"},
    "eleven_flash_v2_5": {"title": "Flash v2.5", "languages": "многоязычная",
                          "quality": 2, "latency": "минимальная"},
}
#: публичные голоса ElevenLabs, которые на русском звучат как дикторский баритон. Это имена
#: из их библиотеки, а не наши «персоны»: клиент может передать любой свой `voice_id`
VOICE_PRESETS = {
    "elevenlabs": {
        "adam": {"title": "Adam", "character": "низкий, спокойный", "storytelling": True},
        "brian": {"title": "Brian", "character": "глубокий, уверенный", "storytelling": True},
        "george": {"title": "George", "character": "дикторский баритон", "storytelling": True},
        "callum": {"title": "Callum", "character": "тёмный, медленный", "storytelling": True},
    },
}


def resolve_performance(code: str | None) -> tuple[str, dict, list[dict[str, str]]]:
    """Подача по коду. Неизвестная — ошибка, а не тихий откат на нейтральную."""
    if not code:
        code = (os.getenv("MEDIA_TTS_PERFORMANCE") or "").strip().lower() or DEFAULT_PERFORMANCE
        row = PERFORMANCES.get(code) or PERFORMANCES[DEFAULT_PERFORMANCE]
        return (code if code in PERFORMANCES else DEFAULT_PERFORMANCE), row, []
    key = str(code).strip().lower()
    row = PERFORMANCES.get(key)
    if row is None:
        return key, PERFORMANCES[DEFAULT_PERFORMANCE], [{
            "code": "performance_unknown", "field": "performance",
            "message": f"подача «{key}» не из {sorted(PERFORMANCES)}",
        }]
    return key, row, []


def performance_catalog() -> list[dict]:
    return [{"code": code, **row} for code, row in sorted(PERFORMANCES.items())]


def pause_scale(performance: str | None) -> float:
    return float(resolve_performance(performance)[1]["pause_scale"])


#: драматическая пауза ставится там, где её ставит диктор: тире, многоточие, граница фразы.
#: Запятая — не пауза, а дыхание: растягивать её в кадре нельзя, иначе титры «плывут».
_PAUSE_MARKS = {"—": 0.55, "–": 0.5, "…": 0.6, "!": 0.35, "?": 0.35, ".": 0.3}
DEFAULT_PAUSE = 0.3


def pause_plan(text: str, *, performance: str | None = None) -> list[dict]:
    """Разбить текст на фразы и посчитать паузы после каждой.

    Возвращает сегменты `{text, pause}` — по ним считаются длительности сцен, и по ним же
    видно, что «кинематографичность» — это +2–3 секунды в ролике, а не бесплатный эффект.
    """
    scale = pause_scale(performance)
    segments: list[dict] = []
    current = ""
    for char in str(text or ""):
        current += char
        if char in _PAUSE_MARKS:
            body = current.strip()
            if body:
                segments.append({"text": body, "pause": round(_PAUSE_MARKS[char] * scale, 3)})
            current = ""
    tail = current.strip()
    if tail:
        segments.append({"text": tail, "pause": 0.0})
    return segments


def pauses_seconds(text: str, *, performance: str | None = None) -> float:
    """Суммарная длина пауз в тексте — добавляется к времени чтения сцены."""
    return round(sum(segment["pause"] for segment in pause_plan(text, performance=performance)), 3)


def strip_stress(text: str) -> str:
    """Убрать метки ударения (`+` перед гласной): в кадре и в подписи их быть не должно."""
    return re.sub(r"\+(?=[аеёиоуыэюяАЕЁИОУЫЭЮЯa-zA-Z])", "", str(text or ""))


def apply_stress(text: str, marks: dict[str, str] | None = None) -> str:
    """Расставить ударения для синтеза: `{"Дагестана": "Дагест+ана"}`.

    Нужно для имён и топонимов: ударение, которое «знает» русский движок (SteosVoice), западная
    модель угадывает. Метка живёт только в тексте для синтеза — :func:`strip_stress` убирает её
    из кадра, иначе зритель увидит «Дагест+ана».
    """
    result = str(text or "")
    for word, marked in (marks or {}).items():
        result = result.replace(word, marked)
    return result


def settings_for(provider: str, *, performance: str | None = None,
                 stability: float | None = None, style: float | None = None,
                 model: str | None = None) -> dict:
    """Настройки подачи под конкретного провайдера, с честным списком того, что не применилось.

    Одна и та же подача у разных движков выражается по-разному: ElevenLabs принимает
    stability/style, SteosVoice — скорость и ударения, локальные модели — только скорость.
    Молча выбросить настройку нельзя: клиент должен видеть, что именно не применилось.
    """
    code, profile, issues = resolve_performance(performance)
    row = PROVIDERS.get(provider) or {}
    supported = set(row.get("settings") or [])
    wanted = {"stability": stability if stability is not None else profile["stability"],
              "style": style if style is not None else profile["style"],
              "speed": profile["speed"], "model": model or row.get("default_model")}
    applied: dict = {}
    skipped: list[str] = []
    problems: list[dict] = list(issues)
    for key, value in wanted.items():
        if value is None or key not in supported:
            if value is not None and key in {"stability", "style"}:
                skipped.append(key)
            continue
        if key in SETTINGS_RANGES:
            low, high = SETTINGS_RANGES[key]
            if not low <= float(value) <= high:
                problems.append({"code": "setting_out_of_range", "field": key,
                                 "message": f"{key} вне {low}…{high}: {value}"})
                continue
        applied[key] = value
    return {"ok": not problems, "performance": code, "provider": provider,
            "applied": applied, "skipped": skipped, "issues": problems}


# ------------------------------------------------------------------ синтез
def synthesize(text: str, *, provider: str | None = None, voice: str | None = None,
               rate: float = 1.0, path: str | None = None,
               performance: str | None = None, stability: float | None = None,
               style: float | None = None, model: str | None = None,
               stress: dict[str, str] | None = None,
               pitch: float | None = None) -> dict:
    """Озвучить текст. Возвращает аудио, длительность, тайминги слов, смету и подачу.

    Подача (`performance`) — это данные: паузы, темп и настройки выразительности. Неизвестная
    подача — отказ, а не «прочитаем нейтрально»: клиент выбрал баритон с драмой, и молча
    получить ровный дикторский голос хуже, чем увидеть ошибку.
    """
    perf_code, perf, perf_issues = resolve_performance(performance)
    if perf_issues:
        return {"ok": False, "reason": perf_issues[0]["message"], "issues": perf_issues}
    provider = (provider or ("yandex" if yandex_configured()["available"] else "mock")).lower()
    settings_state = settings_for(provider, performance=perf_code, stability=stability,
                                  style=style, model=model)
    if pitch is not None:
        settings_state["applied"]["pitch"] = float(pitch)
    rate = max(0.5, min(2.0, float(rate) * float(perf["speed"])))
    marked = apply_stress(text, stress)
    # метки ударения уходят в синтез только тем движкам, которые их понимают; остальным —
    # чистый текст, иначе в аудио появится «плюс» перед гласной
    stress_ok = bool((PROVIDERS.get(provider) or {}).get("stress"))
    spoken = drop_identifiers(normalize(marked if stress_ok else strip_stress(marked)))
    if not spoken:
        return {"ok": False, "reason": "пустой текст: озвучивать нечего"}
    if len(spoken) > MAX_CHARS:
        return {"ok": False, "reason": f"текст длиннее {MAX_CHARS} знаков — это не шортс"}
    if provider not in PROVIDERS:
        return {"ok": False, "reason": f"провайдер {provider!r} не поддерживается: "
                                      f"{sorted(PROVIDERS)}"}

    chars = len(spoken)
    cost = estimate_cost(chars, provider)
    cloud = cloud_provider()
    cost_cloud = estimate_cost(chars, cloud) if cloud else 0.0
    started = time.time()
    # обработчик берётся из реестра: новый провайдер — запись, а не ветка в синтезе
    result = PROVIDERS[provider]["handler"](
        # голос не подставляем здесь: у каждого движка свой дефолт (`ELEVENLABS_VOICE_ID`,
        # `STEOSVOICE_VOICE`, `DEFAULT_VOICE`). Раньше сюда уезжал `ermil` — имя голоса
        # SpeechKit, и ElevenLabs получал запрос на несуществующий голос
        {"text": spoken, "voice": voice, "rate": rate, "path": path,
         "settings": settings_state["applied"]})

    if not result.get("ok"):
        return {**result, "provider": provider, "chars": chars, "cost_rub": cost,
                "cost_cloud_rub": cost_cloud, "cloud_provider": cloud,
                "performance": perf_code, "settings": settings_state,
                "screen_text": strip_stress(marked)}

    duration = audio_duration(result["audio"]) or result.get("duration") or \
        round(len(spoken.split()) / FALLBACK_WORDS_PER_SECOND, 2)
    words = align(text, duration)
    return {"ok": True, "provider": provider, "audio": result["audio"],
            "mime": result.get("mime", "audio/mpeg"), "chars": chars, "cost_rub": cost,
            "cost_cloud_rub": cost_cloud, "cloud_provider": cloud,
            "duration": round(float(duration), 3), "spoken_text": spoken,
            # исходный текст без меток ударения: он идёт в кадр, субтитры и описание
            "screen_text": strip_stress(marked),
            "performance": perf_code, "performance_title": perf["title"],
            "settings": settings_state,
            "pauses": pause_plan(text, performance=perf_code),
            "pause_seconds": pauses_seconds(text, performance=perf_code),
            "rate": round(rate, 3),
            "words": words, "aligned": "proportional",
            "elapsed_ms": int((time.time() - started) * 1000)}


def _yandex(text: str, voice: str, rate: float) -> dict:
    state = yandex_configured()
    if not state["available"]:
        return {"ok": False, "reason": state["reason"]}
    query = {"text": text, "lang": "ru-RU", "voice": voice or DEFAULT_VOICE,
             "format": "mp3",
             "speed": f"{max(0.5, min(2.0, rate)):.2f}"}
    if state["mode"] == "iam" and state["folder"]:
        query["folderId"] = state["folder"]
    url = "https://tts.api.cloud.yandex.net/speech/v1/tts:synthesize?" + \
        urllib.parse.urlencode(query)
    request = urllib.request.Request(url, data=b"", method="POST")
    if state["mode"] == "api_key":
        request.add_header("Authorization", f'Api-Key {os.getenv("YANDEX_TTS_API_KEY", "")}')
    else:
        request.add_header("Authorization", f'Bearer {os.getenv("YANDEX_TTS_IAM_TOKEN", "")}')
    try:
        with urllib.request.urlopen(request, timeout=30) as response:   # noqa: S310 — наш URL
            audio = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        return {"ok": False, "reason": f"SpeechKit {exc.code}: {detail}"}
    except (urllib.error.URLError, TimeoutError) as exc:
        return {"ok": False, "reason": f"SpeechKit недоступен: {exc}"}
    if not audio:
        return {"ok": False, "reason": "SpeechKit вернул пустой ответ"}
    return {"ok": True, "audio": audio, "mime": "audio/mpeg"}


def _elevenlabs(call: dict) -> dict:
    """ElevenLabs: ``POST /v1/text-to-speech/{voice_id}`` с моделью и настройками подачи.

    Голос — id из библиотеки (или клон клиента), а не «имя»: имена Adam/George — ярлыки
    библиотеки, у каждого свой id, и подставлять вместо id слово «george» бессмысленно.
    Если id не задан ни в запросе, ни в ``ELEVENLABS_VOICE_ID`` — честный отказ с подсказкой.
    """
    state = elevenlabs_configured()
    if not state["available"]:
        return {"ok": False, "reason": state["reason"]}
    voice_id = call.get("voice") or state.get("voice")
    if not voice_id or voice_id in (VOICE_PRESETS.get("elevenlabs") or {}):
        return {"ok": False,
                "reason": ("нужен id голоса ElevenLabs (ELEVENLABS_VOICE_ID или voice в запросе); "
                           "Adam/George/Callum — названия из библиотеки, не id")}
    settings = call.get("settings") or {}
    payload = {
        "text": call["text"],
        "model_id": settings.get("model") or state["model"],
        "voice_settings": {
            "stability": float(settings.get("stability", 0.4)),
            "style": float(settings.get("style", 0.2)),
            "similarity_boost": float(settings.get("similarity_boost", 0.75)),
            "use_speaker_boost": True,
        },
    }
    url = (f"https://api.elevenlabs.io/v1/text-to-speech/{urllib.parse.quote(str(voice_id))}"
           f"?output_format=mp3_44100_128")
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"xi-api-key": os.getenv("ELEVENLABS_API_KEY", ""),
                 "content-type": "application/json", "accept": "audio/mpeg"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:   # noqa: S310
            audio = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        return {"ok": False, "reason": f"ElevenLabs {exc.code}: {detail}"}
    except (urllib.error.URLError, TimeoutError) as exc:
        return {"ok": False, "reason": f"ElevenLabs недоступен: {exc}"}
    if not audio:
        return {"ok": False, "reason": "ElevenLabs вернул пустой ответ"}
    return {"ok": True, "audio": audio, "mime": "audio/mpeg",
            "model": payload["model_id"], "voice_settings": payload["voice_settings"]}


def _steosvoice(call: dict) -> dict:
    """SteosVoice: русская фонетика и ударения, скорость и высота тона.

    Контракт сервиса задаётся ``STEOSVOICE_API_URL``: он возвращает либо аудио сразу, либо
    JSON со ссылкой на файл — поддерживаем оба варианта, потому что это различие версий API,
    а не наша логика. Ударения передаются метками ``+`` перед гласной, как принято в сервисе.
    """
    state = steosvoice_configured()
    if not state["available"]:
        return {"ok": False, "reason": state["reason"]}
    voice = call.get("voice") or state.get("voice")
    if not voice:
        return {"ok": False,
                "reason": "нужен голос SteosVoice (STEOSVOICE_VOICE или voice в запросе)"}
    settings = call.get("settings") or {}
    payload = {"token": os.getenv("STEOSVOICE_TOKEN", ""), "text": call["text"],
               "voice": voice, "speed": float(settings.get("speed", 1.0)),
               "pitch": float(settings.get("pitch", 1.0)), "format": "mp3"}
    request = urllib.request.Request(
        state["url"], data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"content-type": "application/json", "accept": "audio/mpeg, application/json"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:   # noqa: S310
            body = response.read()
            mime = response.headers.get("content-type", "")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        return {"ok": False, "reason": f"SteosVoice {exc.code}: {detail}"}
    except (urllib.error.URLError, TimeoutError) as exc:
        return {"ok": False, "reason": f"SteosVoice недоступен: {exc}"}
    if "json" in mime.lower():
        try:
            published = json.loads(body.decode("utf-8"))
        except ValueError:
            return {"ok": False, "reason": "SteosVoice вернул нечитаемый JSON"}
        link = published.get("path") or published.get("url") or published.get("result")
        if not link:
            return {"ok": False, "reason": f"SteosVoice не отдал ссылку на аудио: {published}"}
        try:
            with urllib.request.urlopen(str(link), timeout=60) as audio_response:  # noqa: S310
                body = audio_response.read()
        except (urllib.error.URLError, TimeoutError) as exc:
            return {"ok": False, "reason": f"аудио SteosVoice не скачалось: {exc}"}
    if not body:
        return {"ok": False, "reason": "SteosVoice вернул пустой ответ"}
    return {"ok": True, "audio": body, "mime": "audio/mpeg", "voice": voice}


def _local_tts(call: dict) -> dict:
    """Свой TTS-сервер (Fish Speech / F5-TTS / Piper) по HTTP: аудио в ответ на JSON.

    Живёт на узле клиента: ни цены, ни лимитов по знакам нет — это и есть смысл локальных
    моделей. Клонирование голоса делает сам сервер (10–15 секунд чистого аудио), движок лишь
    передаёт текст, голос и скорость.
    """
    state = local_tts_configured()
    if not state["available"]:
        return {"ok": False, "reason": state["reason"]}
    settings = call.get("settings") or {}
    payload = {"text": call["text"], "voice": call.get("voice") or DEFAULT_VOICE,
               "speed": float(settings.get("speed", 1.0)), "format": "mp3"}
    request = urllib.request.Request(
        state["url"], data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"content-type": "application/json", "accept": "audio/mpeg, audio/wav"})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310
            audio = response.read()
            mime = response.headers.get("content-type", "audio/mpeg")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        return {"ok": False, "reason": f"локальный TTS {exc.code}: {detail}"}
    except (urllib.error.URLError, TimeoutError) as exc:
        return {"ok": False, "reason": f"локальный TTS недоступен: {exc}"}
    if not audio:
        return {"ok": False, "reason": "локальный TTS вернул пустой ответ"}
    return {"ok": True, "audio": audio, "mime": mime.split(";")[0] or "audio/mpeg"}


def _mock(text: str, rate: float) -> dict:
    """Тишина нужной длины: тесты и песочница проверяют конвейер, а не голос."""
    seconds = len(text.split()) / max(FALLBACK_WORDS_PER_SECOND * rate, 0.1)
    state = media_video.detect()
    if not state["available"]:
        return {"ok": False, "reason": "ffmpeg недоступен: тишину сгенерировать нечем"}
    with tempfile.TemporaryDirectory(prefix="posiz-tts-") as tmp:
        out = Path(tmp) / "voice.mp3"
        cmd = [state["ffmpeg"] or "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
               "-f", "lavfi", "-i", f"anullsrc=channel_layout=mono:sample_rate=44100",
               "-t", f"{seconds:.3f}", "-c:a", "libmp3lame", "-b:a", "64k", str(out)]
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=60)
        except (subprocess.SubprocessError, OSError) as exc:
            return {"ok": False, "reason": f"не удалось создать тишину: {exc}"}
        return {"ok": True, "audio": out.read_bytes(), "mime": "audio/mpeg",
                "duration": round(seconds, 3)}


def _from_file(path: str | None) -> dict:
    """Готовый файл: демо, ручная озвучка диктором, чужой синтез."""
    if not path:
        return {"ok": False, "reason": "для провайдера file нужен path"}
    target = Path(path)
    if not target.is_file():
        return {"ok": False, "reason": f"файл {path} не найден"}
    data = target.read_bytes()
    if not data:
        return {"ok": False, "reason": "файл озвучки пустой"}
    return {"ok": True, "audio": data,
            "mime": "audio/mpeg" if target.suffix.lower() in {".mp3", ".m4a"} else "audio/wav"}


def audio_duration(data: bytes) -> float | None:
    """Длительность аудио через ffprobe (быстро и точно); без него — ``None``."""
    if not data:
        return None
    state = media_video.detect()
    if not state["available"]:
        return None
    with tempfile.NamedTemporaryFile(suffix=".mp3") as handle:
        handle.write(data)
        handle.flush()
        probe = media_video.probe_file(handle.name)
    return float(probe.get("duration") or 0) or None


# ------------------------------------------------------------------ тайминги
def align(text: str, duration: float, *, words: list[dict] | None = None) -> list[dict]:
    """Разложить слова по длительности аудио.

    Пропорционально длине слова, а не «поровну»: «договор» звучит заметно дольше «и», и
    равномерная сетка даёт подсветку, которая врёт на каждом длинном слове. Если
    распознавание доступно и вернуло тайминги — используются они.
    """
    if words:
        return words
    tokens = str(text or "").split()
    if not tokens:
        return []
    duration = max(0.2, float(duration or 0))
    weights = [max(2, len(token.strip(".,:;!?…«»\"'()—-"))) for token in tokens]
    total = sum(weights)
    plan: list[dict] = []
    cursor = 0.0
    for token, weight in zip(tokens, weights):
        span = max(MIN_WORD_SECONDS, duration * weight / total)
        plan.append({"word": token, "start": round(cursor, 3),
                     "end": round(min(duration, cursor + span), 3)})
        cursor += span
    # последнее слово дотягиваем до конца аудио: иначе финальный кадр «молчит»
    if plan:
        plan[-1]["end"] = round(duration, 3)
    return plan


def words_from_whisper(audio_path: str) -> list[dict] | None:
    """Пословные тайминги из распознавания, если faster-whisper установлен."""
    try:
        from faster_whisper import WhisperModel         # noqa: PLC0415
    except Exception:                                   # noqa: BLE001
        return None
    model = WhisperModel("small", device="cpu", compute_type="int8")
    segments, _ = model.transcribe(audio_path, word_timestamps=True, language="ru")
    words: list[dict] = []
    for segment in segments:
        for word in segment.words or []:
            words.append({"word": word.word.strip(), "start": round(word.start, 3),
                          "end": round(word.end, 3)})
    return words or None


def srt(words: list[dict], *, line_chars: int = 42) -> str:
    """Субтитры `.srt` из таймингов: тот же список, что уходит в кадр (не второй источник)."""
    if not words:
        return ""
    lines: list[list[dict]] = [[]]
    for word in words:
        candidate = " ".join([w["word"] for w in lines[-1]] + [word["word"]])
        if lines[-1] and len(candidate) > line_chars:
            lines.append([word])
        else:
            lines[-1].append(word)

    def stamp(seconds: float) -> str:
        milliseconds = int(round(seconds * 1000))
        hours, rest = divmod(milliseconds, 3600_000)
        minutes, rest = divmod(rest, 60_000)
        secs, millis = divmod(rest, 1000)
        return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"

    blocks = []
    for index, line in enumerate(lines, start=1):
        blocks.append(f"{index}\n{stamp(line[0]['start'])} --> {stamp(line[-1]['end'])}\n"
                      f"{' '.join(w['word'] for w in line)}\n")
    return "\n".join(blocks)


# ------------------------------------------------------------------ реестр провайдеров
def _check_yandex(state: dict) -> bool:
    return bool(state.get("available"))


def _check_local(_state: dict) -> bool:
    return True


def _check_elevenlabs(states: dict) -> bool:
    return bool((states.get("elevenlabs") or {}).get("available"))


def _check_steosvoice(states: dict) -> bool:
    return bool((states.get("steosvoice") or {}).get("available"))


def _check_local_tts(states: dict) -> bool:
    return bool((states.get("local") or {}).get("available"))


#: Провайдеры синтеза — реестр, а не ветка `if`. Новый провайдер (ElevenLabs, локальный
#: Piper, клон голоса клиента с согласием — `11-video-engine.md` §6.3) добавляется записью:
#: цена, языки, режим работы, проверка доступности и обработчик. Смета и витрина
#: (`available()`) собираются из этого же реестра, поэтому «провайдер есть, а сметы нет»
#: невозможно по построению.
PROVIDERS = {
    "yandex": {
        "title": "Yandex SpeechKit",
        "mode": "cloud",
        "audience": "client",
        "price_per_1000": RUB_PER_1000_CHARS,
        "currency": "RUB",
        "needs": ["YANDEX_TTS_API_KEY", "YANDEX_TTS_IAM_TOKEN"],
        "languages": ["ru", "en", "kk", "uz", "de", "fr"],
        # SpeechKit принимает скорость и голос, но не «выразительность»: настройки подачи
        # применяются частично, и это видно в ответе `settings_for(...).skipped`
        "settings": ["speed"],
        "expressiveness": 3,
        "stress": False,
        "clone": False,
        "handler": lambda call: _yandex(call["text"], call["voice"], call["rate"]),
        "check": _check_yandex,
    },
    "elevenlabs": {
        "title": "ElevenLabs",
        "mode": "cloud",
        "audience": "client",
        # тариф Creator: $22 / 1 млн знаков ≈ 1 760 ₽ по курсу 80 ₽ за доллар; меняется
        # окружением MEDIA_TTS_ELEVENLABS_RUB_PER_1000, потому что тарифы меняются чаще релизов
        "price_per_1000": 1.76,
        "currency": "RUB",
        "needs": ["ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID"],
        "languages": ["ru", "en", "de", "fr", "es", "it", "pl", "uk", "tr", "hi", "ja", "ko"],
        "settings": ["stability", "style", "similarity_boost", "speed", "model"],
        "default_model": "eleven_multilingual_v2",
        "expressiveness": 5,
        "stress": True,
        "clone": True,
        "handler": lambda call: _elevenlabs(call),
        "check": _check_elevenlabs,
    },
    "steosvoice": {
        "title": "SteosVoice",
        "mode": "cloud",
        "audience": "client",
        # тариф озвучивания: ~0,3 ₽ за 1000 знаков; уточняется при подключении ключа
        "price_per_1000": 0.3,
        "currency": "RUB",
        "needs": ["STEOSVOICE_TOKEN", "STEOSVOICE_VOICE"],
        "languages": ["ru", "en"],
        "settings": ["speed", "pitch"],
        "expressiveness": 4,
        # движок обучен на русской речи: ударения в топонимах и исторических именах — его
        # сильная сторона, поэтому метки ударения поддерживаются штатно
        "stress": True,
        "clone": True,
        "handler": lambda call: _steosvoice(call),
        "check": _check_steosvoice,
    },
    "local": {
        "title": "Свой TTS-сервер (Fish Speech / F5-TTS / Piper)",
        "mode": "local",
        # self-hosted: клиенту сервиса не предлагается, нужен тем, кто разворачивает модель сам
        "audience": "self-hosted",
        "price_per_1000": 0.0,
        "currency": "RUB",
        "needs": ["MEDIA_LOCAL_TTS_URL"],
        "languages": ["*"],
        "settings": ["speed"],
        "expressiveness": 3,
        "stress": True,
        "clone": True,
        "handler": lambda call: _local_tts(call),
        "check": _check_local_tts,
    },
    "mock": {
        "title": "Тишина нужной длины (тесты и песочница)",
        "mode": "local",
        "audience": "internal",
        "price_per_1000": 0.0,
        "currency": "RUB",
        "needs": [],
        "languages": ["*"],
        "handler": lambda call: _mock(call["text"], call["rate"]),
        "check": _check_local,
    },
    "file": {
        "title": "Готовая озвучка: диктор, демо, чужой TTS",
        "mode": "local",
        "audience": "internal",
        "price_per_1000": 0.0,
        "currency": "RUB",
        "needs": [],
        "languages": ["*"],
        "handler": lambda call: _from_file(call["path"]),
        "check": _check_local,
    },
}
