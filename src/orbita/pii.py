"""PII-маскирование до отправки в LLM и в логах (152-ФЗ).

Зачем: в Инбокс попадают комментарии с ФИО, телефонами, ИНН, номерами договоров и карт.
Отправлять это в зарубежную модель нельзя, а без маскирования нельзя и качественно
отвечать. Решение: необратимое для модели псевдонимизирование с восстановлением
плейсхолдеров **только** в тексте ответа на нашей стороне.

Правила — регулярные выражения + NER-эвристика для ФИО (последовательности
«Фамилия Имя [Отчество]» в именительном падеже со словарём окончаний). Полноценная
модель NER подключается как ``PII_NER_URL`` (см. ``_ner_remote``); без неё работает
эвристика — она намеренно консервативна: лучше замаскировать лишнее, чем упустить.

Плейсхолдеры вида ``[PHONE_1]``, ``[PERSON_2]``: восстанавливаются и в ответе, и в
сгенерированном тексте перед отдачей клиенту (``restore``).
"""
from __future__ import annotations

import logging
import os
import re

log = logging.getLogger("orbita.pii")

PLACEHOLDER_RE = re.compile(r"\[(PHONE|EMAIL|INN|SNILS|PASSPORT|CARD|PERSON|ACCOUNT)_(\d+)\]")

# ФИО: «Иванов Иван Иванович», «Иванова А. П.»
_PERSON_FULL = re.compile(
    r"\b([А-ЯЁ][а-яё]{2,}(?:ов|ев|ёв|ин|ын|ский|цкий|ая|ова|ева|ина|ына|ская|цкая))"
    r"\s+([А-ЯЁ][а-яё]{1,}(?:\s+[А-ЯЁ][а-яё]{1,})?)"
)
_PERSON_SHORT = re.compile(r"\b([А-ЯЁ][а-яё]{2,}(?:ов|ев|ин|ский|ко))\s+[А-ЯЁ]\.\s*(?:[А-ЯЁ]\.)?")
# «Иван Петров» — имя, затем фамилия (без ключевого слова)
_PERSON_NAME_SURNAME = re.compile(
    r"\b([А-ЯЁ][а-яё]{2,})\s+([А-ЯЁ][а-яё]{2,}(?:ов|ев|ёв|ин|ын|ский|цкий|ова|ева|ина|ына|ская|ко))\b"
)
# «меня зовут Иван Петров» — обратный порядок
_PERSON_REVERSED = re.compile(
    r"\b(зовут|это|пишет|обращается|клиент)\s+([А-ЯЁ][а-яё]{1,})\s+"
    r"([А-ЯЁ][а-яё]{1,}(?:ов|ев|ин|ский|ко))"
)

_PHONE = re.compile(r"(?:\+7|8)[\s\-(]*\d{3}[\s\-)]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}\b")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]{2,}\b")
# ИНН: 10 или 12 цифр; с префиксом «ИНН» — приоритетнее прочих номеров
_INN = re.compile(r"\b(?:ИНН[:\s]*)?(?:\d{10}|\d{12})\b")
_SNILS = re.compile(r"\b\d{3}-\d{3}-\d{3}[\s-]?\d{2}\b")
# паспорт: серия и номер с разделителем ИЛИ явное слово «паспорт» рядом
_PASSPORT = re.compile(r"\b\d{2}[\s-]\d{2}\s?№?\s?\d{6}\b|"
                       r"(?:паспорт[а-яё]*\s*(?:серия\s*)?)(\d{4}\s?\d{6})", re.I)
_CARD = re.compile(r"\b(?:\d{4}[ -]){3}\d{1,4}\b")
_ACCOUNT = re.compile(r"\b(?:р/с|расч[её]т|сч[её]т)[^\d]{0,8}(\d{20})\b")


def enabled() -> bool:
    return os.getenv("PII_MASKING", "1") not in ("0", "false", "False")


def _ner_remote(text: str) -> list[str]:
    """Опциональный внешний NER (например, Natasha в контуре РФ). Без URL — пусто."""
    url = os.getenv("PII_NER_URL", "").strip()
    if not url:
        return []
    try:
        import httpx

        resp = httpx.post(url, json={"text": text}, timeout=5.0)
        resp.raise_for_status()
        return [str(t) for t in resp.json().get("persons", [])]
    except Exception as exc:                       # noqa: BLE001 — NER не должен ломать ответ
        log.warning("NER недоступен (%s), работаем на эвристике", exc)
        return []


def _counters_from(mapping: dict[str, str]) -> dict[str, int]:
    """Продолжить нумерацию там, где остановились: иначе два письма дают два разных
    ``[PHONE_1]`` для разных номеров — и восстановление возвращает чужой номер."""
    counters: dict[str, int] = {}
    for ph in mapping:
        kind, _, num = ph.strip("[]").rpartition("_")
        if num.isdigit():
            counters[kind] = max(counters.get(kind, 0), int(num))
    return counters


def mask(text: str, mapping: dict[str, str] | None = None) -> tuple[str, dict[str, str]]:
    """Заменить PII на плейсхолдеры. Возвращает (текст, {плейсхолдер: оригинал}).

    ``mapping`` — уже накопленные плейсхолдеры (например, по всему диалогу Инбокса).
    Без него каждый вызов начинал нумерацию с ``[PHONE_1]``, и в переписке разные люди
    получали одинаковый плейсхолдер: восстановление подставляло чужой телефон, а в LLM
    уходил контекст, где два разных клиента склеивались в одного.
    """
    if not text or not text.strip():
        return text, dict(mapping or {})
    mapping = dict(mapping or {})
    counters = _counters_from(mapping)

    def placeholder(kind: str, value: str) -> str:
        for ph, original in mapping.items():
            if original == value:
                return ph
        counters[kind] = counters.get(kind, 0) + 1
        ph = f"[{kind}_{counters[kind]}]"
        mapping[ph] = value
        return ph

    out = text
    # порядок важен: телефоны раньше «голых» чисел, чтобы не съесть их как ИНН
    # порядок важен: специфичные форматы раньше «голых» чисел
    for kind, pattern in (("EMAIL", _EMAIL), ("PHONE", _PHONE), ("SNILS", _SNILS),
                          ("CARD", _CARD), ("ACCOUNT", _ACCOUNT), ("INN", _INN),
                          ("PASSPORT", _PASSPORT)):
        out = pattern.sub(lambda m, k=kind: placeholder(k, m.group(0)), out)

    # Порядок важен: сначала локальная эвристика ФИО, затем внешний NER.
    # Раньше было наоборот — NER получал текст, где ФИО ещё не замаскированы, то есть
    # имена уходили внешнему сервису (152-ФЗ: передача ПДн за контур).
    out = _PERSON_NAME_SURNAME.sub(
        lambda m: placeholder("PERSON", f"{m.group(1)} {m.group(2)}"), out)
    out = _PERSON_FULL.sub(lambda m: placeholder("PERSON", f"{m.group(1)} {m.group(2)}"), out)
    out = _PERSON_SHORT.sub(lambda m: placeholder("PERSON", m.group(0)), out)
    out = _PERSON_REVERSED.sub(
        lambda m: f"{m.group(1)} " + placeholder("PERSON", f"{m.group(2)} {m.group(3)}"), out)

    # Внешний NER видит уже обезличенный текст и ищет только то, что не поймала эвристика.
    for person in _ner_remote(out):
        if person and person in out:
            out = out.replace(person, placeholder("PERSON", person))
    return out, mapping


def restore(text: str, mapping: dict[str, str]) -> str:
    """Вернуть оригиналы на место плейсхолдеров."""
    if not text or not mapping:
        return text
    out = text
    for ph, original in mapping.items():
        out = out.replace(ph, original)
    return out


def contains_pii(text: str) -> bool:
    masked, mapping = mask(text)
    return bool(mapping)


def audit_report(text: str) -> dict:
    """Что будет замаскировано — для инбокса и логов, без самих значений."""
    _, mapping = mask(text)
    kinds: dict[str, int] = {}
    for ph in mapping:
        kind = ph.strip("[]").rsplit("_", 1)[0]
        kinds[kind] = kinds.get(kind, 0) + 1
    return {"found": sum(kinds.values()), "kinds": kinds}


class Masker:
    """Маскировщик одного диалога/сессии: плейсхолдеры сквозные, нумерация не сбрасывается.

    ``Masker().mask(a); Masker().mask(b)`` — разные номера получают разные плейсхолдеры,
    а ``restore`` возвращает исходные значения. Используется воркерами Инбокса и LLM.
    """

    def __init__(self, mapping: dict[str, str] | None = None):
        self.mapping: dict[str, str] = dict(mapping or {})

    def mask(self, text: str) -> tuple[str, dict[str, str]]:
        masked, self.mapping = mask(text, self.mapping)
        return masked, self.mapping

    def restore(self, text: str) -> str:
        return restore(text, self.mapping)

    @property
    def found(self) -> dict[str, int]:
        kinds: dict[str, int] = {}
        for ph in self.mapping:
            kind = ph.strip("[]").rsplit("_", 1)[0]
            kinds[kind] = kinds.get(kind, 0) + 1
        return kinds
