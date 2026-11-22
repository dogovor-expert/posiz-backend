"""Языковые профили ролика: скорость чтения, ширина строки, голос (этап G, ADR 0023).

Зачем отдельный модуль, а не «перевод строк»:

**1. Язык — это не только текст.** Тот же русский заголовок из 12 слов читается 4,6 секунды,
английский — 3,9: в `en` темп чтения выше, строк в кадре помещается больше, а слово титров
длиннее. Если тайминги считать по русской константе 2,6 слова/с, английская версия получается
вялой (титры отстают), а версия для языка с длинными словами — наоборот, обрезанной.

**2. Поэтому профиль — данные.** `LOCALES` — реестр: темп чтения, лимиты строки и слова титров,
голос синтеза. Новый язык — запись в реестр, а не ветка `if` в рендерере. Неизвестный код —
ошибка `locale_unknown`, а не тихий откат на русский: «перевели на en, а ролик вышел русским»
хуже, чем отказ.

**3. Перевод приходит текстом, тайминги считает код.** `media_reel.localize(spec, locale, texts)`
берёт переведённые строки и пересчитывает длительности по профилю языка. Движок не переводит
(это работа модели) и не выдумывает (это работа данных) — он проверяет, что переведённое
влезает в кадр, и говорит, что именно не влезло.
"""
from __future__ import annotations

from typing import Any

#: профили языков. `words_per_second` — комфортный темп чтения с экрана (не темп речи),
#: `max_line_chars` — сколько знаков влезает в строку кадра, `max_caption_chars` — предельная
#: длина слова в титрах, `voice` — тег для синтеза речи.
LOCALES: dict[str, dict[str, Any]] = {
    "ru": {
        "title": "Русский",
        "words_per_second": 2.6,
        "max_line_chars": 42,
        "max_caption_chars": 42,
        "voice": "ru-RU",
        "sample": "Скидка 20% на весь каталог",
    },
    "en": {
        # английский на экране читается быстрее: 3,1 слова/с против 2,6 у русского
        "title": "English",
        "words_per_second": 3.1,
        "max_line_chars": 46,
        "max_caption_chars": 46,
        "voice": "en-US",
        "sample": "20% off storewide",
    },
    "kk": {
        "title": "Қазақша",
        "words_per_second": 2.4,
        "max_line_chars": 44,
        "max_caption_chars": 44,
        "voice": "kk-KZ",
        "sample": "Барлық каталогқа 20% жеңілдік",
    },
    "uz": {
        "title": "Oʻzbekcha",
        "words_per_second": 2.5,
        "max_line_chars": 44,
        "max_caption_chars": 44,
        "voice": "uz-UZ",
        "sample": "Butun katalogga 20% chegirma",
    },
}

DEFAULT_LOCALE = "ru"


def codes() -> list[str]:
    """Языки реестра — для витрины `/media/templates` и валидации."""
    return sorted(LOCALES)


def profile(code: str | None) -> dict[str, Any] | None:
    """Профиль языка или ``None``. Пустой код = язык по умолчанию."""
    if not code:
        return LOCALES[DEFAULT_LOCALE]
    return LOCALES.get(str(code).strip().lower())


def resolve(code: str | None) -> tuple[str, dict[str, Any], list[dict[str, str]]]:
    """Код, профиль и замечания. Неизвестный язык — отказ, а не откат на русский."""
    if not code:
        return DEFAULT_LOCALE, LOCALES[DEFAULT_LOCALE], []
    key = str(code).strip().lower()
    found = LOCALES.get(key)
    if found is None:
        return key, LOCALES[DEFAULT_LOCALE], [{
            "code": "locale_unknown", "field": "locale",
            "message": f"язык «{key}» не поддержан: доступны {', '.join(codes())}",
        }]
    return key, found, []


def words_per_second(code: str | None) -> float:
    return float((profile(code) or LOCALES[DEFAULT_LOCALE])["words_per_second"])


def max_caption_chars(code: str | None) -> int:
    return int((profile(code) or LOCALES[DEFAULT_LOCALE])["max_caption_chars"])


def catalog() -> list[dict[str, Any]]:
    """Витрина языков: то, что показывает интерфейс вместо «списка констант в коде»."""
    return [{"code": code, **LOCALES[code]} for code in codes()]
