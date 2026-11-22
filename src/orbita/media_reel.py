"""Ролики 9:16 из поста: спека, таймлайн, кадры (этап C, ADR 0023).

Медиа-конвейер до этого умел только статику: карточка → PNG (`media.py` + `media_raster.py`).
Ролик — это **время**: у него есть сцены, длительности, прогресс-бар и титры, которые меняются
каждые 200–400 мс. Поэтому здесь отдельный модуль, а не «карточка с флагом video».

Три принципа, которые определяют реализацию:

1. **Кадр — чистая функция от времени.** ``frame_at(spec, t)`` не имеет состояния: тот же ``t``
   даёт побайтово тот же SVG. Из этого следует всё остальное — кадр можно отрендерить параллельно
   в пуле, переснять один дефектный участок, не пересобирая ролик, и сравнить два рендера в тесте.
2. **Модель не пишет анимацию.** Сцена — шаблон из закрытого набора (``hook-typed``,
   ``chips-flow``, ``captions-stack``, ``cta-card``); модель заполняет текст и выбирает сцену.
   Тайминги считает код: длительность сцены зависит от объёма текста и скорости его прочтения.
3. **Озвучки на этом этапе нет.** Титры синхронизируются с «планом прочтения» (2,6 слова/с), а не
   с аудио. Когда появится TTS (этап D), ``words`` просто придут из распознавания — контракт
   кадра не изменится, потому что он и сейчас смотрит только на тайминги слов.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable
from xml.sax.saxutils import escape

from . import media, media_compliance, media_locale, media_raster, media_storage, media_voice
from .docs import Collection, DEMO_WORKSPACE

SPEC_VERSION = "reel.spec.v2"
RENDERER = "inhouse-reel@1"

#: закрытая библиотека сцен — по ней же лежат прототипы в `20-DESIGN/video/frame.html`
SCENES: dict[str, dict[str, Any]] = {
    "hook-typed": {
        "title": "Хук: заголовок печатается посимвольно",
        "props": ("headline", "eyebrow"),
        "min_seconds": 2.0,
    },
    "chips-flow": {
        "title": "Пункты: чипы появляются по одному",
        "props": ("eyebrow", "items"),
        "min_seconds": 3.0,
    },
    "captions-stack": {
        "title": "Титры: слово подсвечивается по таймингу",
        "props": ("captions",),
        "min_seconds": 3.0,
    },
    "cta-card": {
        "title": "Финал: бренд и призыв",
        "props": ("cta", "footer"),
        "min_seconds": 2.0,
    },
}

FPS_CHOICES = (25, 30)
MIN_DURATION = 4.0

#: значения по умолчанию; фактические лимиты читаются из окружения (`limits()`),
#: потому что «60 секунд для всех» — это лимит движка, а не клиента и не канала
DEFAULT_LIMITS = {"max_duration": 60.0, "max_scenes": 6, "min_duration": 4.0,
                  "max_frames": 3000}


def limits() -> dict:
    """Лимиты ролика: дефолт + переопределение окружением (тариф, канал, рендер-нода).

    ``MEDIA_REEL_MAX_DURATION``, ``MEDIA_REEL_MAX_SCENES``, ``MEDIA_REEL_MIN_DURATION``,
    ``MEDIA_REEL_MAX_FRAMES``. Читаются на каждый вызов, поэтому меняются без рестарта —
    и без релиза движка.
    """
    result = dict(DEFAULT_LIMITS)
    for key in list(result):
        raw = os.getenv(f"MEDIA_REEL_{key.upper()}", "").strip()
        if not raw:
            continue
        try:
            result[key] = float(raw) if "." in raw else int(raw)
        except ValueError:
            continue
    return result


MAX_DURATION = DEFAULT_LIMITS["max_duration"]      # оставлено для обратной совместимости
MAX_SCENES = DEFAULT_LIMITS["max_scenes"]
MAX_CHIP_CHARS = 48
MAX_CAPTION_CHARS = 42
MAX_HOOK_WORDS = 9
#: скорость чтения для плана таймингов: субтитры не должны мелькать быстрее, чем читается
WORDS_PER_SECOND = 2.6
MIN_WORD_SECONDS = 0.22

#: формат ролика — поле спеки, а не константа рендерера: одна спека → лента, Shorts, YouTube
DEFAULT_FORMAT = "9x16"
REEL_FORMATS = ("9x16", "4x5", "1x1", "16x9")
#: высота зоны контента 9:16 — база, от которой масштабируется типографика других форматов
BASE_CONTENT_HEIGHT = 1280

#: высота строки плашки маркировки и зазор между плашками (1080×1920, кегль 30)
PLATE_LINE = 44
PLATE_GAP = 18
#: сколько места должна оставить маркировка, чтобы смысловой текст не наезжал на неё
MAX_PLATE_SHARE = 0.45

TEXT_COLOR = media.TEXT_COLOR
TEXT_MUTED = media.TEXT_MUTED

_reels = Collection("reel_jobs")
_specs = Collection("reel_specs")


def _uid(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------ спека
def _sentence_split(text: str) -> list[str]:
    parts, current = [], ""
    for ch in text:
        current += ch
        if ch in ".!?…":
            parts.append(current.strip())
            current = ""
    if current.strip():
        parts.append(current.strip())
    return [p for p in parts if p]


def plan_words(text: str, *, words_per_second: float | None = None,
               locale: str | None = None, start: float = 0.0) -> list[dict]:
    """План таймингов слов: равномерно по длине слова, а не «все по секунде».

    Это не распознавание речи, а читальная сетка: длинное слово держится на экране дольше
    короткого. Когда придёт TTS, эти значения заменятся реальными — формат тот же.
    """
    if words_per_second is None:
        words_per_second = (media_locale.words_per_second(locale) if locale
                            else WORDS_PER_SECOND)
    words = text.split()
    if not words:
        return []
    # Вся сцена длится words / скорость_чтения, а слово получает долю по своей длине.
    # Первая версия считала длину слова как «символы / скорость» и растягивала 23 слова
    # на 41 секунду: титры показывали первое слово несколько секунд подряд.
    duration = len(words) / max(words_per_second, 0.1)
    weights = [max(2, len(word.strip(".,:;!?…«»\"'()-—"))) for word in words]
    total = sum(weights)
    plan: list[dict] = []
    cursor = start
    for word, weight in zip(words, weights):
        length = max(MIN_WORD_SECONDS, duration * weight / total)
        plan.append({"word": word, "start": round(cursor, 3), "end": round(cursor + length, 3)})
        cursor += length
    return plan


def reading_seconds(text: str, *, words_per_second: float | None = None,
                    locale: str | None = None) -> float:
    """Сколько секунд нужно, чтобы прочитать текст в комфортном темпе.

    Темп зависит от языка (`media_locale.LOCALES`): 2,6 слова/с для русского — это не «константа
    чтения», а свойство конкретного языка. Английский читается быстрее, и та же фраза в `en`
    не должна висеть на экране лишнюю секунду.
    """
    if words_per_second is None:
        words_per_second = (media_locale.words_per_second(locale) if locale
                            else WORDS_PER_SECOND)
    words = len(text.split())
    return round(max(1.2, words / max(words_per_second, 0.1)), 2)


def build_reel_spec(post: dict, *, brand: dict | None = None, compliance: dict | None = None,
                    fps: int = 30, scenes: Iterable[dict] | None = None,
                    voice: dict | None = None, fmt: str = DEFAULT_FORMAT,
                    locale: str | None = None, performance: str | None = None) -> dict:
    """Пост → ``reel.spec.v2``: хук, пункты, титры, финал.

    Длительности не «по 5 секунд на сцену», а по объёму текста: хук держится ровно столько,
    сколько нужно, чтобы его прочитали, пункты — по числу пунктов. Итог не выходит за
    ``MAX_DURATION``: лишнее уходит в «список» титрами, а не растягивает ролик.
    """
    locale_code = media_locale.resolve(locale)[0]
    # подача диктора — тоже часть спеки: драматические паузы занимают время в кадре
    perf_code, perf, _perf_issues = media_voice.resolve_performance(performance)
    pause_k = float(perf.get("pause_scale") or 1.0)

    def scene_seconds(text: str, pad: float) -> float:
        """Время сцены: чтение + паузы диктора + переход. Паузы — из подачи, а не «по вкусу»."""
        pauses = media_voice.pauses_seconds(text, performance=perf_code)
        return reading_seconds(text, locale=locale_code) + pauses + pad * pause_k
    title = str(post.get("title") or post.get("headline") or "").strip()
    eyebrow = str(post.get("eyebrow") or "").strip()
    note = str(post.get("note") or post.get("subtitle") or "").strip()
    items = [str(item).strip() for item in (post.get("items") or []) if str(item).strip()][:5]
    footer = str(post.get("footer") or (brand or {}).get("name") or "posiz.ru").strip()
    cta = str(post.get("cta") or "Читать целиком").strip()

    if scenes:
        raw = list(scenes)
    else:
        raw = []
        hook_text = title or note or footer
        raw.append({"template": "hook-typed",
                    "props": {"headline": hook_text, "eyebrow": eyebrow},
                    "duration": max(2.0, round(scene_seconds(hook_text, 0.6), 3))})
        chips = items or [s for s in _sentence_split(note)][:5]
        if chips:
            raw.append({"template": "chips-flow",
                        "props": {"eyebrow": eyebrow or "коротко", "items": chips[:5]},
                        "duration": max(3.0, round(scene_seconds(" ".join(chips), 0.8), 3))})
        captions_text = note if note and not items else (title if not items else "")
        if captions_text:
            raw.append({"template": "captions-stack",
                        "props": {"captions": captions_text},
                        "duration": max(3.0, round(scene_seconds(captions_text, 0.6), 3))})
        raw.append({"template": "cta-card",
                    "props": {"cta": cta, "footer": footer},
                    "duration": max(2.0, round(scene_seconds(cta, 1.2), 3))})

    spec = {
        "spec_version": SPEC_VERSION,
        # формат сохраняем как просили: неизвестный должен стать ошибкой, а не тихим 9:16
        "format": str(fmt or DEFAULT_FORMAT),
        "fps": int(fps),
        # язык — часть спеки: от него зависят тайминги титров и предельная длина слова
        "locale": locale_code,
        "performance": perf_code,
        "brand": dict(brand or {}),
        "voice": dict(voice or {}),
        "compliance": dict(compliance or {}),
        "scenes": raw,
    }
    for index, scene in enumerate(spec["scenes"]):
        scene.setdefault("id", _scene_id(scene, index))
    spec["duration"] = round(sum(float(s.get("duration") or 0) for s in spec["scenes"]), 3)
    _fill_caption_timings(spec)
    return spec


def _scene_id(scene: dict, index: int) -> str:
    """Идентификатор сцены из её содержимого, а не случайный.

    Случайный `sc_…` ломал идемпотентность: два вызова с одинаковым постом давали разные спеки,
    значит разный `spec_hash`, значит повторный рендер того же ролика и второй файл в хранилище.
    Теперь одинаковый вход даёт побайтово ту же спеку — и «тот же ролик» узнаётся по хешу.
    """
    payload = json.dumps({"template": scene.get("template"), "props": scene.get("props") or {},
                          "index": index}, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"))
    return "sc_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:10]


def _fill_caption_timings(spec: dict) -> None:
    """Посчитать тайминги слов для сцен-титров относительно начала сцены."""
    for scene in spec["scenes"]:
        if scene.get("template") != "captions-stack":
            continue
        props = scene.setdefault("props", {})
        text = str(props.get("captions") or "")
        if text and not props.get("words"):
            props["words"] = plan_words(text, locale=spec.get("locale"), start=0.0)


def localize(spec: dict, *, locale: str, texts: dict) -> dict:
    """Переведённые строки → та же спека на другом языке, с пересчитанными таймингами.

    Перевод приходит **данными** (`texts`), а не генерируется здесь: движок не переводит, он
    проверяет, что переведённое влезает в кадр, и пересчитывает время чтения по профилю языка.
    Ключи `texts`: `headline`, `eyebrow`, `items`, `captions`, `cta`, `footer` — сцена сама
    берёт те, что у неё есть в `props`, поэтому перевод не привязан к порядку сцен.

    При переполнении возвращается `ok: false` и список замечаний, а спека остаётся исходной:
    «перевели, но обрезали» — это не локализация.
    """
    code, profile, issues = media_locale.resolve(locale)
    if issues:
        return {"ok": False, "spec": spec, "issues": issues, "changes": []}

    local = json.loads(json.dumps(spec))
    local["localized_from"] = spec.get("locale") or media_locale.DEFAULT_LOCALE
    local.setdefault("performance", spec.get("performance"))
    local["locale"] = code
    changes: list[str] = []

    for scene in local.get("scenes") or []:
        template = scene.get("template")
        props = scene.setdefault("props", {})
        pads = {"hook-typed": 0.6, "chips-flow": 0.8, "captions-stack": 0.6, "cta-card": 1.2}
        if template == "hook-typed":
            for key in ("headline", "eyebrow"):
                if str(texts.get(key) or "").strip():
                    props[key] = str(texts[key]).strip()
                    changes.append(f"{key} → {props[key]}")
        elif template == "chips-flow":
            if texts.get("items"):
                props["items"] = [str(item).strip() for item in texts["items"] if str(item).strip()]
                changes.append(f"items → {len(props['items'])}")
            if str(texts.get("eyebrow") or "").strip():
                props["eyebrow"] = str(texts["eyebrow"]).strip()
        elif template == "captions-stack":
            if str(texts.get("captions") or "").strip():
                props["captions"] = str(texts["captions"]).strip()
                props.pop("words", None)          # тайминги пересчитает код, а не переводчик
                changes.append(f"captions → {props['captions'][:32]}…")
        elif template == "cta-card":
            for key in ("cta", "footer"):
                if str(texts.get(key) or "").strip():
                    props[key] = str(texts[key]).strip()
                    changes.append(f"{key} → {props[key]}")

        # длительность — по объёму текста в этом языке, но не меньше минимума шаблона
        spoken = props.get("captions") or " ".join([
            str(props.get("headline") or ""), str(props.get("cta") or ""),
            " ".join(str(item) for item in props.get("items") or []),
        ]).strip()
        if template == "cta-card":
            spoken = str(props.get("cta") or "")
        minimum = SCENES[template]["min_seconds"] if template in SCENES else 2.0
        scene["duration"] = round(max(minimum, reading_seconds(spoken, locale=code)
                                      + pads.get(template, 0.6)), 3)

    _fill_caption_timings(local)
    local["duration"] = round(sum(float(scene.get("duration") or 0)
                                  for scene in local.get("scenes") or []), 3)
    report = check(local)
    return {"ok": report["ok"], "spec": local if report["ok"] else spec,
            "issues": report["issues"], "changes": changes,
            "locale": code, "profile": profile}


def apply_voice(spec: dict, voice: dict) -> dict:
    """Подогнать таймлайн под готовое аудио и разложить слова по сценам титров.

    Два действия, и оба обязательны. **Первое**: длительность ролика должна совпадать с
    длительностью озвучки — иначе голос договаривает в тишине или обрывается на полуслове.
    Сцены масштабируются пропорционально, а не «подтягиваются по последней». **Второе**:
    титры получают тайминги от речи, поэтому подсветка слова совпадает с голосом, а не с
    читальной сеткой.
    """
    audio_seconds = float(voice.get("duration") or 0)
    words = voice.get("words") or []
    if audio_seconds > 0 and spec.get("scenes"):
        current = sum(float(scene.get("duration") or 0) for scene in spec["scenes"])
        fps = int(spec.get("fps") or 30)
        if current > 0:
            factor = audio_seconds / current
            for scene in spec["scenes"]:
                # минимум сцены соблюдаем даже при быстрой речи: сцена на 0,8 с — это мигание.
                # Если из-за минимумов ролик стал длиннее речи, тишину в конце добирает apad.
                floor = float(SCENES.get(scene.get("template"), {}).get("min_seconds") or 1.0)
                frames = max(round(float(scene.get("duration") or 0) * factor * fps),
                             round(floor * fps))
                scene["duration"] = round(frames / fps, 3)
            # речь не должна обрываться: если аудио длиннее сцен, дотягиваем последнюю
            total = sum(float(scene.get("duration") or 0) for scene in spec["scenes"])
            if audio_seconds - total > 0.05 and spec["scenes"]:
                last = spec["scenes"][-1]
                last["duration"] = round(float(last["duration"]) + (audio_seconds - total), 3)
        spec["duration"] = round(sum(float(scene.get("duration") or 0)
                                     for scene in spec["scenes"]), 3)

    # слова раскладываем по сценам-титрам их временными окнами
    rows = timeline(spec)
    for row, scene in zip(rows, spec["scenes"]):
        if scene.get("template") != "captions-stack":
            continue
        window = [word for word in words
                  if float(word.get("end", 0)) > row["start"] and float(word.get("start", 0)) < row["end"]]
        if not window:
            continue
        scene.setdefault("props", {})["words"] = [
            {"word": word["word"], "start": round(max(0.0, float(word["start"]) - row["start"]), 3),
             "end": round(max(0.0, float(word["end"]) - row["start"]), 3)}
            for word in window]

    spec["voice"] = {
        "provider": voice.get("provider"), "duration": round(audio_seconds, 3),
        "chars": voice.get("chars"), "cost_rub": voice.get("cost_rub"),
        "aligned": voice.get("aligned"), "spoken_text": voice.get("spoken_text"),
        # подача — часть артефакта: по ней видно, почему ролик на 2 секунды длиннее
        "performance": voice.get("performance") or spec.get("performance"),
        "settings": voice.get("settings"),
        "pause_seconds": voice.get("pause_seconds"),
        "words": words,
    }
    return spec


# ------------------------------------------------------------------ проверки
def _issue(code: str, field: str, message: str) -> dict[str, str]:
    return {"code": code, "field": field, "message": message}


def validate_reel_spec(spec: dict) -> list[dict]:
    """Проверки до рендера: они дешевле, чем 900 кадров, и находят дефект раньше глаза."""
    issues: list[dict] = []
    if not isinstance(spec, dict):
        return [_issue("spec_type", "spec", "спека ролика — объект")]
    if spec.get("spec_version") != SPEC_VERSION:
        issues.append(_issue("spec_version", "spec_version",
                             f"ожидается {SPEC_VERSION}"))
    issues.extend(media.validate_brand(spec.get("brand")))
    fmt = spec.get("format") or DEFAULT_FORMAT
    if fmt not in REEL_FORMATS:
        issues.append(_issue("format_unknown", "format",
                             f"формат {fmt!r} не из {list(REEL_FORMATS)}"))
    issues.extend(media_locale.resolve(spec.get("locale"))[2])
    issues.extend(media_voice.resolve_performance(spec.get("performance"))[2])
    caption_limit = media_locale.max_caption_chars(spec.get("locale"))
    if int(spec.get("fps") or 0) not in FPS_CHOICES:
        issues.append(_issue("fps_invalid", "fps", f"fps из {list(FPS_CHOICES)}"))

    scenes = spec.get("scenes") or []
    if not isinstance(scenes, list) or not scenes:
        issues.append(_issue("scenes_empty", "scenes", "в ролике нет ни одной сцены"))
        return issues
    config = limits()
    if len(scenes) > config["max_scenes"]:
        issues.append(_issue("scenes_too_many", "scenes",
                             f"сцен не больше {config['max_scenes']}: иначе ролик разваливается"))

    box = media.content_box(fmt if fmt in media.FORMATS else DEFAULT_FORMAT)
    total = 0.0
    for index, scene in enumerate(scenes):
        template = scene.get("template")
        if template not in SCENES:
            issues.append(_issue("scene_unknown", f"scenes[{index}].template",
                                 f"шаблон {template!r} не из библиотеки"))
            continue
        duration = float(scene.get("duration") or 0)
        total += duration
        if duration < SCENES[template]["min_seconds"]:
            issues.append(_issue("scene_too_short", f"scenes[{index}].duration",
                                 f"{template}: минимум {SCENES[template]['min_seconds']} с"))
        props = scene.get("props") or {}
        if template == "hook-typed":
            headline = str(props.get("headline") or "").strip()
            if not headline:
                issues.append(_issue("prop_required", f"scenes[{index}].props.headline",
                                     "хук без текста"))
            if len(headline.split()) > MAX_HOOK_WORDS:
                issues.append(_issue("hook_too_long", f"scenes[{index}].props.headline",
                                     f"хук — до {MAX_HOOK_WORDS} слов"))
            if len(headline) > media.MAX_HEADLINE_CHARS:
                issues.append(_issue("headline_too_long", f"scenes[{index}].props.headline",
                                     f"заголовок до {media.MAX_HEADLINE_CHARS} знаков"))
            size = int(round(_hook_size(headline) * canvas(spec)["scale"]))
            for line in media._wrap(headline, size, box["width"], bold=True):
                if media.text_width(line, size, bold=True) > box["width"]:
                    issues.append(_issue("out_of_frame", f"scenes[{index}].props.headline",
                                         "строка хука шире зоны контента"))
        if template == "chips-flow":
            items = props.get("items") or []
            if not 1 <= len(items) <= media.MAX_ITEMS:
                issues.append(_issue("items_too_many", f"scenes[{index}].props.items",
                                     f"пунктов от 1 до {media.MAX_ITEMS}"))
            for item in items:
                if len(str(item)) > MAX_CHIP_CHARS:
                    issues.append(_issue("chip_too_long", f"scenes[{index}].props.items",
                                         f"пункт до {MAX_CHIP_CHARS} знаков"))
        if template == "captions-stack":
            words = props.get("words") or []
            if not words:
                issues.append(_issue("prop_required", f"scenes[{index}].props.captions",
                                     "титры без текста"))
            for word in words:
                if len(str(word.get("word") or "")) > caption_limit:
                    issues.append(_issue("caption_too_long", f"scenes[{index}].props.words",
                                         f"слово титров до {caption_limit} знаков "
                                         f"для языка {spec.get('locale') or media_locale.DEFAULT_LOCALE}"))
        if template == "cta-card" and not str(props.get("cta") or "").strip():
            issues.append(_issue("prop_required", f"scenes[{index}].props.cta",
                                 "финал без призыва"))

    if total < config["min_duration"]:
        issues.append(_issue("duration_range", "duration",
                             f"ролик короче {MIN_DURATION} с не публикуем"))
    if total > config["max_duration"]:
        issues.append(_issue("duration_range", "duration",
                             f"ролик длиннее {config['max_duration']} с: сократите сцены"))
    declared = spec.get("duration")
    if declared is not None and abs(float(declared) - total) > 0.05:
        issues.append(_issue("duration_mismatch", "duration",
                             f"в спеке {declared} с, по сценам {round(total, 2)} с"))

    # маркировка проверяется пакетом рынка: один список кодов на карточки и ролики
    compliance = spec.get("compliance") or {}
    issues.extend(media_compliance.validate(compliance))

    # плашка занимает низ кадра: если под неё не остаётся места, текст всё равно наедет
    scale = canvas(spec)["scale"]
    plates = media_compliance.plates(compliance)
    reserve = media_compliance.reserve_height(plates, line_height=PLATE_LINE * scale,
                                              gap=PLATE_GAP * scale)
    if reserve:
        free = media.content_box(fmt if fmt in media.FORMATS else DEFAULT_FORMAT)["height"]
        if reserve > free * MAX_PLATE_SHARE:
            issues.append(_issue("compliance_no_room", "compliance",
                                 f"маркировка занимает {reserve} px при зоне {free} px: "
                                 f"сократите дисклеймер"))
    width = media.content_box(fmt if fmt in media.FORMATS else DEFAULT_FORMAT)["width"]
    for row in plates:
        base = 30 if row["kind"] == "ad" else 26
        size = max(1, int(round(base * scale)))
        for line in row["lines"]:
            if media.text_width(line, size) > width - int(12 * scale):
                issues.append(_issue("plate_too_long", "compliance",
                                     f"строка маркировки не влезает в кадр: «{line[:40]}…»"))
    return issues


def check(spec: dict) -> dict:
    issues = validate_reel_spec(spec)
    return {"ok": not issues, "issues": issues,
            "duration": spec.get("duration") if isinstance(spec, dict) else None}


# ------------------------------------------------------------------ таймлайн
def timeline(spec: dict) -> list[dict]:
    """Сцены с абсолютными границами кадров: по этому списку рендерятся кадры."""
    fps = int(spec.get("fps") or 30)
    cursor = 0.0
    rows: list[dict] = []
    for index, scene in enumerate(spec.get("scenes") or []):
        duration = float(scene.get("duration") or 0)
        rows.append({
            "index": index, "id": scene.get("id"), "template": scene.get("template"),
            "start": round(cursor, 3), "end": round(cursor + duration, 3),
            "duration": round(duration, 3),
            "first_frame": int(round(cursor * fps)),
            "frames": max(1, int(round(duration * fps))),
        })
        cursor += duration
    return rows


def frames_count(spec: dict) -> int:
    """Сколько всего кадров. Считается по сценам, а не по общей длительности.

    Разница принципиальная: ``round(10.34 * 30) = 310``, а сумма покадровых округлений по
    сценам — 311. Ролик, собранный «по общей длительности», теряет кадр на стыке сцен, и
    титры уезжают на один кадр. Поэтому источник истины — таймлайн.
    """
    fps = int(spec.get("fps") or 30)
    if not spec.get("scenes"):
        return max(1, int(round(float(spec.get("duration") or 0) * fps)))
    return sum(max(1, int(round(float(scene.get("duration") or 0) * fps)))
               for scene in spec["scenes"])


def scene_at(spec: dict, moment: float) -> tuple[dict, float]:
    """Сцена и локальный прогресс 0..1 на момент времени. После последней — финальный кадр."""
    rows = timeline(spec)
    if not rows:
        raise ValueError("в спеке нет сцен")
    for row, scene in zip(rows, spec["scenes"]):
        if moment < row["end"] or row is rows[-1]:
            span = max(row["duration"], 1e-6)
            progress = min(1.0, max(0.0, (moment - row["start"]) / span))
            return {"row": row, "scene": scene, "progress": progress,
                    "local": round(max(0.0, moment - row["start"]), 3)}, progress
    raise ValueError("недостижимо")


def word_at(words: list[dict], local: float) -> int:
    """Индекс активного слова по локальному времени сцены (для подсветки титров)."""
    for index, word in enumerate(words):
        if float(word.get("start", 0)) <= local < float(word.get("end", 0)):
            return index
    if words and local >= float(words[-1].get("end", 0)):
        return len(words) - 1
    return 0


# ------------------------------------------------------------------ кадр
def _ease(value: float) -> float:
    value = min(1.0, max(0.0, value))
    return value * value * (3 - 2 * value)


def _hook_size(text: str) -> int:
    if len(text) <= 22:
        return 96
    if len(text) <= 40:
        return 78
    return 64


def _fit_text(text: str, max_width: int, *, sizes: tuple[int, ...] = (42, 36, 32, 28, 26),
              max_lines: int = 2, bold: bool = False) -> tuple[int, list[str]]:
    """Подобрать размер шрифта так, чтобы текст влез в ширину без обрезки по краю кадра.

    Важно именно уменьшать кегль, а не обрезать: «Маркировка рекламы в кадре и в те…»
    читается как баг вёрстки, а не как текст.
    """
    for size in sizes:
        lines = media._wrap(text, size, max_width, bold=bold)
        if len(lines) <= max_lines and all(
                media.text_width(line, size, bold=bold) <= max_width for line in lines):
            return size, lines
    smallest = sizes[-1]
    return smallest, media._wrap(text, smallest, max_width, bold=bold)[:max_lines]


def _palette(spec: dict) -> dict[str, str]:
    return media._palette({"brand": spec.get("brand") or {}})


def _defs(palette: dict[str, str], scale: float = 1.0) -> list[str]:
    grid = max(24, int(round(120 * scale)))
    line = max(0.02, 0.035)                       # сетка одна на все форматы
    return [
        "<defs>",
        f'<linearGradient id="glow" x1="0" y1="0" x2="1" y2="1">'
        f'<stop offset="0" stop-color="{palette["accent"]}" stop-opacity="0.22"/>'
        f'<stop offset="1" stop-color="{palette["accent"]}" stop-opacity="0"/>'
        f"</linearGradient>",
        f'<pattern id="grid" width="{grid}" height="{grid}" patternUnits="userSpaceOnUse">'
        f'<path d="M {grid} 0 L 0 0 0 {grid}" fill="none" stroke="{palette["scrim"]}" '
        f'stroke-opacity="{line}"/>'
        f"</pattern>",
        "</defs>",
    ]


def canvas(spec: dict) -> dict:
    """Холст кадра: формат, зона контента, масштаб типографики и brand kit.

    Один ролик должен выходить в 9:16, 4:5, 1:1 и 16:9 **без отдельной вёрстки на каждый
    формат**. Поэтому кегли и отступы считаются от высоты зоны контента 9:16 (1280 px):
    для 16:9 множитель ≈0,70, для 1:1 ≈0,69, для 4:5 ≈0,90. Переносы строк при этом идут по
    реальной ширине зоны — иначе на широком кадре получилась бы узкая вертикальная колонка
    с гигантскими полями.
    """
    fmt = str(spec.get("format") or DEFAULT_FORMAT)
    if fmt not in media.FORMATS:
        fmt = DEFAULT_FORMAT
    width, height = media.FORMATS[fmt]
    box = media.content_box(fmt)
    kit = media.brand_kit(spec.get("brand") or {})
    scale = (box["height"] / BASE_CONTENT_HEIGHT) * float(kit.get("scale") or 1.0)
    return {"format": fmt, "width": width, "height": height, "box": box,
            "scale": scale, "kit": kit}


def frame_at(spec: dict, moment: float) -> str:
    """Кадр ролика на момент ``moment`` секунд. Чистая функция: тот же вход — тот же SVG."""
    plan = canvas(spec)
    width, height, s = plan["width"], plan["height"], plan["scale"]
    palette = _palette(spec)
    font = palette["font"]

    def px(value: float) -> int:
        """Кегль/отступ в пикселях кадра: считаем от базы 9:16, а не «на глаз»."""
        return max(1, int(round(value * s)))

    box = dict(plan["box"])
    # маркировка резервирует место заранее: плашка не наезжает на текст, текст — на плашку
    plates = media_compliance.plates(spec.get("compliance") or {})
    plate_line, plate_gap = px(PLATE_LINE), px(PLATE_GAP)
    reserve = media_compliance.reserve_height(plates, line_height=plate_line, gap=plate_gap)
    plate_box = dict(box)
    if reserve:
        box = {**box, "height": box["height"] - reserve}
    # время зажимаем: кадр «после конца» — это последний кадр, а не другой прогресс-бар
    moment = max(0.0, min(float(moment), float(spec.get("duration") or 0)))
    state, progress = scene_at(spec, moment)
    scene, row, local = state["scene"], state["row"], state["local"]
    template = scene["template"]
    props = scene.get("props") or {}
    brand = spec.get("brand") or {}
    total = float(spec.get("duration") or row["end"] or 1)

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="{escape(str(props.get("headline") or props.get("cta") or template))[:100]}">',
        *_defs(palette, s),
        f'<rect width="{width}" height="{height}" fill="{palette["bg"]}"/>',
        f'<rect width="{width}" height="{height}" fill="url(#grid)"/>',
        f'<ellipse cx="{int(width * 0.76)}" cy="{int(-height * 0.05)}" '
        f'rx="{int(width * 0.5)}" ry="{int(height * 0.22)}" fill="url(#glow)"/>',
    ]

    # бренд — всегда сверху слева, внутри safe zone
    brand_name = str(brand.get("name") or "posiz.ru")
    parts.append(
        f'<text x="{box["x"]}" y="{box["y"] + px(30)}" fill="{palette["muted"]}" '
        f'font-size="{px(30)}" font-weight="600" letter-spacing="0.4" '
        f'font-family="{font}">{escape(brand_name)}</text>'
    )

    if template == "hook-typed":
        size = px(_hook_size(str(props.get("headline") or "")))
        lines = media._wrap(str(props.get("headline") or ""), size, box["width"], bold=True)
        reveal = min(1.0, _ease(progress / 0.55))
        all_chars = sum(len(line) for line in lines)
        visible = int(round(all_chars * reveal))
        top = box["y"] + int(box["height"] * 0.18)
        parts.append(f'<rect x="{box["x"] - px(30)}" y="{top - px(8)}" width="{px(8)}" '
                     f'height="{int(size * 1.4)}" rx="{px(4)}" fill="{palette["accent"]}"/>')
        cursor_y = top
        left = visible
        last_shown, last_y = "", top
        for line in lines:
            shown = line[:max(0, left)]
            left -= len(line)
            cursor_y += size
            if not shown:
                break
            last_shown, last_y = shown, cursor_y
            parts.append(
                f'<text x="{box["x"]}" y="{cursor_y}" fill="{palette["text"]}" font-size="{size}" '
                f'font-weight="800" font-family="{font}">{escape(shown)}</text>'
            )
        if reveal < 1.0:                                  # курсор печати — там, где печатается
            caret_on = int(local * 2.4) % 2 == 0
            if caret_on:
                measured = media.text_width(last_shown, size, bold=True)
                parts.append(f'<rect x="{box["x"] + measured + px(8)}" y="{last_y - size + px(8)}" '
                             f'width="{px(8)}" height="{size}" fill="{palette["accent"]}"/>')
        eyebrow = str(props.get("eyebrow") or "")
        if eyebrow:
            parts.append(
                f'<text x="{box["x"]}" y="{top - px(60)}" fill="{palette["muted"]}" '
                f'font-size="{px(34)}" font-weight="600" letter-spacing="1.2" '
                f'font-family="{font}">{escape(eyebrow.upper())}</text>'
            )

    elif template == "chips-flow":
        items = [str(item) for item in (props.get("items") or [])][:media.MAX_ITEMS]
        eyebrow = str(props.get("eyebrow") or "коротко")
        parts.append(
            f'<text x="{box["x"]}" y="{box["y"] + px(96)}" fill="{palette["muted"]}" '
            f'font-size="{px(34)}" font-weight="600" letter-spacing="1.2" '
            f'font-family="{font}">{escape(eyebrow.upper())}</text>'
        )
        step = 0.72 / max(len(items), 1)
        top = box["y"] + int(box["height"] * 0.14)
        cursor = top
        for index, item in enumerate(items):
            size, lines = _fit_text(item[:MAX_CHIP_CHARS], box["width"] + px(24) - px(96),
                                    sizes=tuple(px(value) for value in (42, 36, 32, 28, 26)))
            height = px(96) + (len(lines) - 1) * px(44)
            appear = _ease((progress - index * step) / 0.25)
            if appear > 0:
                dx = int((1 - appear) * px(60))
                parts.append(
                    f'<g opacity="{round(appear, 3)}" transform="translate({dx},0)">'
                    f'<rect x="{box["x"] - px(24)}" y="{cursor}" width="{box["width"] + px(48)}" '
                    f'height="{height}" rx="{px(28)}" fill="{palette["scrim"]}" fill-opacity="0.045"/>'
                    f'<circle cx="{box["x"] + px(26)}" cy="{cursor + height / 2}" r="{px(16)}" '
                    f'fill="{palette["accent"]}"/>'
                )
                line_y = cursor + height / 2 - (len(lines) - 1) * px(22)
                for line_index, line in enumerate(lines):
                    parts.append(
                        f'<text x="{box["x"] + px(70)}" y="{line_y + line_index * px(44) + size * 0.34}" '
                        f'fill="{palette["text"]}" font-size="{size}" font-weight="600" '
                        f'font-family="{font}">{escape(line)}</text>'
                    )
                parts.append("</g>")
            cursor += height + px(40)

    elif template == "captions-stack":
        size = px(58)
        words = props.get("words") or plan_words(str(props.get("captions") or ""))
        active = word_at(words, local)
        lines: list[list[dict]] = [[]]
        for word in words:
            candidate = " ".join([w["word"] for w in lines[-1]] + [word["word"]])
            if lines[-1] and media.text_width(candidate, size, bold=True) > box["width"]:
                lines.append([word])
            else:
                lines[-1].append(word)
        lines = [line for line in lines if line]
        # Окно из трёх строк вокруг активного слова, а не «первые три строки сцены»:
        # иначе на длинной сцене хвост текста пропадал, и титры расходились с голосом.
        line_of_word: list[int] = []
        for index, line in enumerate(lines):
            line_of_word.extend([index] * len(line))
        active_line = line_of_word[active] if 0 <= active < len(line_of_word) else 0
        first_line = max(0, min(active_line - 1, len(lines) - 3))
        visible_lines = lines[first_line:first_line + 3]
        top = box["y"] + int(box["height"] * 0.52)
        for line_index, line in enumerate(visible_lines):
            y = top + line_index * px(96)
            x = box["x"]
            for word in line:
                highlight = words.index(word) == active
                span = media.text_width(word["word"], size, bold=True)
                if highlight:
                    parts.append(f'<rect x="{x - px(8)}" y="{y - px(52)}" width="{span + px(16)}" '
                                 f'height="{px(72)}" rx="{px(14)}" fill="{palette["accent"]}" '
                                 f'fill-opacity="0.9"/>')
                parts.append(
                    f'<text x="{x}" y="{y}" fill="{palette["scrim"] if highlight else palette["text"]}" '
                    f'font-size="{size}" font-weight="800" font-family="{font}">'
                    f"{escape(word['word'])}</text>"
                )
                x += span + px(22)

    elif template == "cta-card":
        appear = _ease(progress / 0.4)
        zoom = 0.96 + 0.04 * appear
        cta = str(props.get("cta") or "Читать целиком")
        footer = str(props.get("footer") or brand_name)
        size, lines = _fit_text(cta, box["width"],
                                sizes=tuple(px(value) for value in (76, 68, 60, 52)),
                                max_lines=3, bold=True)
        # центрируем блок по вертикали: в 9:16 это прежние 0,36, в широких форматах — середина
        block_h = len(lines) * int(size * 1.2) + px(24)
        top = box["y"] + max(0, int((box["height"] - block_h) / 2))
        parts.append(f'<g opacity="{round(appear, 3)}" '
                     f'transform="translate({int(width * (1 - zoom) / 2)},{int(height * (1 - zoom) / 2)}) '
                     f'scale({round(zoom, 4)})">')
        for index, line in enumerate(lines):
            parts.append(
                f'<text x="{box["x"]}" y="{top + index * int(size * 1.2)}" fill="{palette["text"]}" '
                f'font-size="{size}" font-weight="800" font-family="{font}">'
                f"{escape(line)}</text>"
            )
        parts.append(f'<rect x="{box["x"]}" y="{top + len(lines) * int(size * 1.2) + px(18)}" '
                     f'width="{px(64)}" height="{px(6)}" '
                     f'rx="{px(3)}" fill="{palette["accent"]}"/></g>')
        parts.append(
            f'<text x="{box["x"]}" y="{box["y"] + box["height"] - px(20)}" fill="{palette["muted"]}" '
            f'font-size="{px(34)}" font-weight="600" '
            f'font-family="{font}">{escape(footer)}</text>'
        )

    # маркировка рекламы: внутри зоны контента, а не под интерфейсом канала — иначе
    # плашку закрывает полоса с кнопками, и маркировки формально «нет»
    bar_h = px(6)
    bar_y = plate_box["y"] + plate_box["height"] - bar_h
    cursor = bar_y - plate_gap
    for row in plates:
        if row["kind"] == "age":
            continue                                  # возрастная метка уходит в угол
        row_lines = row["lines"]
        block_h = len(row_lines) * plate_line
        top = cursor - block_h
        if row["kind"] == "ad":
            color, weight, size = palette["plate"], "600", px(30)
            text_w = max(media.text_width(line, size) for line in row_lines) if row_lines else 0
            parts.append(
                f'<rect x="{plate_box["x"] - px(14)}" y="{top - px(8)}" '
                f'width="{text_w + px(40)}" height="{block_h + px(18)}" rx="{px(16)}" '
                f'fill="{palette["plate"]}" fill-opacity="0.10" '
                f'stroke="{palette["plate"]}" stroke-opacity="0.45"/>')
        else:
            color, weight, size = palette["muted"], "500", px(26)
        for index, line in enumerate(row_lines):
            line_y = top + (index + 1) * plate_line - px(14)
            parts.append(
                f'<text x="{plate_box["x"] + px(6)}" y="{line_y}" fill="{color}" '
                f'font-size="{size}" font-weight="{weight}" '
                f'font-family="{font}">{escape(line)}</text>')
        cursor = top - plate_gap

    for row in plates:
        if row["kind"] != "age":
            continue
        text = " ".join(row["lines"])
        text_w = media.text_width(text, px(30))
        badge_x = plate_box["x"] + plate_box["width"] - text_w - px(24)
        badge_y = plate_box["y"] + px(6)
        parts.append(
            f'<rect x="{badge_x - px(12)}" y="{badge_y - px(6)}" width="{text_w + px(24)}" '
            f'height="{px(46)}" rx="{px(10)}" fill="{palette["scrim"]}" fill-opacity="0.08" '
            f'stroke="{palette["scrim"]}" stroke-opacity="0.22"/>'
            f'<text x="{badge_x}" y="{badge_y + px(28)}" fill="{palette["muted"]}" '
            f'font-size="{px(30)}" font-weight="700" '
            f'font-family="{font}">{escape(text)}</text>')

    # прогресс-бар: понятно, сколько осталось (досмотр выше)
    bar_w = int(width * min(1.0, (moment / total) if total else 1.0))
    parts.append(f'<rect x="0" y="{bar_y}" width="{width}" height="{bar_h}" '
                 f'fill="{palette["scrim"]}" fill-opacity="0.12"/>')
    parts.append(f'<rect x="0" y="{bar_y}" width="{bar_w}" height="{bar_h}" '
                 f'fill="{palette["accent"]}"/>')
    parts.append("</svg>")
    return "".join(parts)


def storyboard(spec: dict, count: int | None = None) -> list[dict]:
    """Ключевые кадры: по одному на сцену (или ``count`` штук, если попросили больше)."""
    rows = timeline(spec)
    moments = [round(row["start"] + row["duration"] * 0.75, 3) for row in rows]
    if count and count > len(moments):
        total = float(spec.get("duration") or 0)
        extra = [round(total * (index + 1) / (count - len(moments) + 1), 3)
                 for index in range(count - len(moments))]
        moments = sorted(moments + extra)
    if count:
        moments = moments[:count]
    return [{"t": moment, "svg": frame_at(spec, moment)} for moment in moments]


def spec_hash(spec: dict) -> str:
    """Хеш спеки: одинаковый ролик — один артефакт, без повторного рендера кадров."""
    payload = json.dumps({"renderer": RENDERER, "spec": spec}, ensure_ascii=False,
                         sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def cost_estimate(spec: dict) -> dict:
    """Смета до рендера: кадры, секунды CPU и рубли. Показывается до запуска."""
    fps = int(spec.get("fps") or 30)
    frames = frames_count(spec)
    seconds = float(spec.get("duration") or 0)
    # кадр Chromium ≈ 0,35 с CPU: цифра из замера на 1080×1920, а не «на глаз»
    cpu_seconds = round(frames * 0.35, 1)
    return {"frames": frames, "fps": fps, "duration": round(seconds, 2),
            "cpu_seconds": cpu_seconds, "cpu_rub": round(cpu_seconds * 0.0009, 2),
            "storage_mb": round(seconds * 0.55, 1)}


# ------------------------------------------------------------------ рендер
def render_frames(spec: dict, *, limit: int | None = None, workers: int = 6
                 ) -> tuple[list[bytes] | None, str | None]:
    """Кадры в PNG: SVG считается в памяти, растр — в пуле потоков.

    Растеризатор — внешний бинарь, поэтому потоки работают: большую часть времени процесс
    ждёт его, а не считает. Кадры рендерятся по одному разу, но каждый — независимо:
    дефектный участок можно переснять, не трогая остальное.
    """
    from concurrent.futures import ThreadPoolExecutor

    fps = int(spec.get("fps") or 30)
    total = min(frames_count(spec), int(limits()["max_frames"]))
    if limit is not None:
        total = min(total, limit)
    plan = canvas(spec)
    width, height = plan["width"], plan["height"]
    svgs = [frame_at(spec, index / fps) for index in range(total)]

    # быстрый путь: один браузер на весь ролик (~50 мс на кадр)
    fast, backend = media_raster.rasterize_many(svgs, width, height)
    if fast:
        return fast, backend

    # запасной: по кадру на процесс, зато работает даже там, где нет playwright
    def _one(svg: str) -> bytes | None:
        png, _ = media_raster.rasterize(svg, width, height)
        return png

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        results = list(pool.map(_one, svgs))
    if any(png is None for png in results):
        return None, None
    found = media_raster.detect()
    return results, (found[0] if found else None)


# ------------------------------------------------------------------ задачи
def submit(spec: dict, *, render: bool = True, limit: int | None = None,
           workspace_id: str | None = None, voice: dict | None = None) -> dict:
    """Принять спеку ролика: проверить, отрендерить кадры и собрать MP4.

    Возвращает задачу с честным статусом: ``ready`` (mp4 в хранилище), ``storyboard``
    (растр есть, ffmpeg нет — отдаём кадры и превью), ``rejected`` (проверки не прошли).
    """
    from . import media_video

    # Озвучка до проверок: её длительность меняет длительности сцен, а значит и валидность
    voice_result = None
    if voice:
        voice_result = media_voice.synthesize(
            voice.get("text") or _narration_text(spec), provider=voice.get("provider"),
            voice=voice.get("voice"), rate=float(voice.get("rate") or 1.0),
            path=voice.get("path"),
            # подача и настройки идут в синтез тем же объектом, что и в спеку: один источник
            performance=voice.get("performance") or spec.get("performance"),
            stability=voice.get("stability"), style=voice.get("style"),
            model=voice.get("model"), stress=voice.get("stress"), pitch=voice.get("pitch"))
        if voice_result.get("ok"):
            spec = apply_voice(spec, voice_result)

    report = check(spec)
    compliance_state = media_compliance.resolve(spec.get("compliance") or {})
    job = {
        "id": _uid("reel_"), "kind": "reel", "spec_hash": spec_hash(spec),
        "compliance": compliance_state,
        "status": "rejected" if not report["ok"] else "accepted",
        "issues": report["issues"], "cost": cost_estimate(spec),
        "created_at": _now(), "workspace_id": workspace_id or "ws_de",
        "fps": int(spec.get("fps") or 30), "duration": spec.get("duration"),
    }
    if voice_result is not None:
        job["voice"] = {key: value for key, value in voice_result.items() if key != "audio"}
        job["voice_ok"] = bool(voice_result.get("ok"))
    remember_spec(job["id"], spec)                    # кадр по запросу нужен и «только смете»
    if not report["ok"]:
        return {"reel": _reels.put(job), "spec": spec, "checks": report, "asset": None}

    existing = next((item for item in _reels.all()
                     if item.get("spec_hash") == job["spec_hash"] and item.get("status") in
                     {"ready", "storyboard"}), None)
    if existing is not None:
        return {"reel": existing, "spec": spec, "checks": report, "asset": existing,
                "reused": True}

    if not render:
        job["status"] = "planned"
        return {"reel": _reels.put(job), "spec": spec, "checks": report, "asset": None}

    frames, backend = render_frames(spec, limit=limit)
    if frames is None:
        job["status"] = "failed"
        job["error"] = media_raster.available().get("reason") or "растеризатор недоступен"
        return {"reel": _reels.put(job), "spec": spec, "checks": report, "asset": None}

    job["frames_rendered"] = len(frames)
    job["raster_backend"] = backend
    job["spec_id"] = job["id"]
    key_base = f"media/{job['workspace_id']}/reel/{job['id']}"
    # кадр в конце хука — самый информативный постер: там уже есть заголовок целиком
    poster = frames[min(len(frames) - 1, max(0, int(job["fps"] * min(2.0, job["duration"] or 2.0))))]
    remember_spec(job["id"], spec)

    audio = (voice_result or {}).get("audio")
    video = media_video.encode(frames, fps=job["fps"], audio=audio)
    if video.get("ok"):
        plan_dims = canvas(spec)
        defects = media_video.checks(video["probe"], expected_duration=float(job["duration"] or 0),
                                     fps=job["fps"],
                                     size=(plan_dims["width"], plan_dims["height"]))
        job["video"] = video["probe"]
        if defects:
            # файл собрался, но не проходит проверки: публиковать нельзя, показываем причину
            job.update({"status": "storyboard", "video_defects": defects,
                        "video_error": "; ".join(defects)})
        else:
            stored = media_storage.put(f"{key_base}.mp4", video["bytes"], mime="video/mp4")
            job.update({"status": "ready", "video_key": f"{key_base}.mp4",
                        "video_bytes": stored["bytes"], "video_defects": []})
    else:
        job.update({"status": "storyboard", "video_error": video.get("reason"),
                    "video": {"frames": len(frames), "fps": job["fps"]}})

    if voice_result and voice_result.get("ok"):
        audio_info = media_storage.put(f"{key_base}.mp3", voice_result["audio"],
                                       mime=voice_result.get("mime") or "audio/mpeg")
        job["audio_key"] = f"{key_base}.mp3"
        job["audio_bytes"] = audio_info["bytes"]
        subtitles = media_voice.srt(voice_result.get("words") or [])
        if subtitles:
            media_storage.put(f"{key_base}.srt", subtitles.encode("utf-8"),
                              mime="application/x-subrip")

    # маркировка дублируется в описании: в кадре — плашка, в публикации — текст
    if compliance_state.get("description"):
        media_storage.put(f"{key_base}.txt", compliance_state["description"].encode("utf-8"),
                          mime="text/plain; charset=utf-8")
        job["compliance_key"] = f"{key_base}.txt"

    if poster:
        media_storage.put(f"{key_base}.png", poster, mime="image/png")
        job["poster_key"] = f"{key_base}.png"
        job["poster_bytes"] = len(poster)

    return {"reel": _reels.put(job), "spec": spec, "checks": report, "asset": job}


def reel(reel_id: str) -> dict | None:
    return _reels.get(reel_id)


def reels(limit: int = 50) -> list[dict]:
    rows = sorted(_reels.all(), key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return rows[:limit]


def content(reel_id: str, fmt: str = "mp4") -> tuple[bytes, str] | None:
    """Байты ролика, постера или кадра — из хранилища, без пересборки."""
    item = _reels.get(reel_id)
    if not item:
        return None
    if fmt == "mp4":
        key = item.get("video_key")
        data = media_storage.get(key) if key else None
        return (data, "video/mp4") if data else None
    if fmt in {"mp3", "srt"}:
        key = item.get("audio_key")
        if not key:
            return None
        # рядом с .mp3 лежит .srt с теми же таймингами, что уходят в кадр
        data = media_storage.get(key.rsplit(".", 1)[0] + f".{fmt}")
        mime = "audio/mpeg" if fmt == "mp3" else "application/x-subrip"
        return (data, mime) if data else None
    if fmt == "txt":
        key = item.get("compliance_key")
        data = media_storage.get(key) if key else None
        return (data, "text/plain; charset=utf-8") if data else None
    if fmt == "png":
        key = item.get("poster_key")
        data = media_storage.get(key) if key else None
        return (data, "image/png") if data else None
    if fmt == "svg":
        key = item.get("storyboard_key")
        data = media_storage.get(key) if key else None
        if data is None:
            spec = spec_of(item.get("spec_id") or item["id"])
            return (frame_at(spec, float(spec.get("duration") or 0)).encode("utf-8"),
                    "image/svg+xml") if spec else None
        return data, "image/svg+xml"
    return None


def frame_svg(reel_id: str, moment: float) -> str | None:
    """Кадр по запросу: превью на любой секунде без хранения 900 файлов."""
    spec = spec_of(reel_id)
    if not spec:
        return None
    return frame_at(spec, max(0.0, min(float(moment), float(spec.get("duration") or 0))))



def spec_of(reel_id: str) -> dict | None:
    doc = _specs.get(reel_id)
    return doc.get("spec") if doc else None


def remember_spec(reel_id: str, spec: dict) -> None:
    _specs.put({"id": reel_id, "spec": spec, "workspace_id": "ws_de"})


def _narration_text(spec: dict) -> str:
    """Текст диктора из спеки: хук + титры + финал, без дублей и подписей бренда."""
    parts: list[str] = []
    for scene in spec.get("scenes") or []:
        props = scene.get("props") or {}
        for key in ("headline", "captions", "cta"):
            value = str(props.get(key) or "").strip()
            if value:
                parts.append(value)
        for item in props.get("items") or []:
            parts.append(str(item).strip())
    unique: list[str] = []
    for part in parts:
        if part and part not in unique:
            unique.append(part)
    return ". ".join(unique)


def purge(ttl_days: int = 14) -> dict:
    """Удалить задачи роликов старше TTL. Файлы чистит хранилище по mtime.

    Документ ролика удаляем вместе с файлом: иначе в списке остаются «готовые» ролики,
    у которых нет видео, и клиент видит 404 на собственный артефакт.
    """
    cutoff = time.time() - ttl_days * 86400
    removed = 0
    for item in list(_reels.all()):
        created = item.get("created_at")
        stamp = created.timestamp() if hasattr(created, "timestamp") else None
        if stamp is not None and stamp < cutoff:
            _reels.delete(item["id"])
            _specs.delete(item["id"])
            removed += 1
    return {"removed": removed, "ttl_days": ttl_days}


def summary() -> dict:
    rows = _reels.all()
    by_status: dict[str, int] = {}
    for item in rows:
        by_status[item.get("status")] = by_status.get(item.get("status"), 0) + 1
    ready = [item for item in rows if item.get("status") == "ready"]
    return {"reels": len(rows), "by_status": by_status,
            "ready": len(ready),
            "video_bytes": sum(int(item.get("video_bytes") or 0) for item in ready),
            "scenes": sorted(SCENES), "fps": list(FPS_CHOICES),
            "formats": [fmt for fmt in REEL_FORMATS if fmt in media.FORMATS],
            "limits": limits()}
