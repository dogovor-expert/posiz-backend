"""Маркировка в кадре: правила рынка — данные, рендерер — один (этап E, ADR 0023).

Зачем отдельный модуль, а не блок `if compliance.get("ad")` в рендерере:

* **Правила разные, а рендер один.** В РФ реклама требует пометки «Реклама», рекламодателя,
  ИНН и `erid`; в ЕС — раскрытия «реклама/Anzeige»; по FTC — «Paid partnership». Если правила
  зашиты в вёрстку, каждый рынок — это релиз движка. Здесь рынок — это **пакет правил (rule pack)**:
  какие поля обязательны, какая формулировка пометки, какие возрастные метки допустимы,
  что писать в описании.
* **Плашка в кадре — не украшение.** Штраф за немаркированную рекламу — до 500 тыс. ₽ на
  организацию, поэтому «забыли плашку» должно быть невозможно не «по инструкции», а по коду:
  проверка возвращает error, рендер не стартует (`media_reel.submit` → `rejected`).
* **Один источник правды для кадра и описания.** `plates()` рисует то, что видно на экране,
  `description()` — то, что уходит в описание канала; оба считаются из одной и той же спеки,
  поэтому «в кадре есть, в описании нет» не разъезжается.

Модуль намеренно не импортирует ни `media`, ни `media_reel`: он знает только про данные
маркировки, а координаты и кегли остаются делом рендерера.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

#: пакет по умолчанию: рынок, для которого маркировка обязательна и жёстка
DEFAULT_PACK = "ru-ad"

#: длина подписи-дисклеймера: на телефоне дальше этого не читают
MAX_DISCLAIMER_CHARS = 120
#: возрастные метки, которые вообще существуют как категории (по умолчанию для рынков без своих)
ALL_AGES = (0, 6, 12, 16, 18)

INN_RE = re.compile(r"\d{10}|\d{12}")
#: erid — токен из ОРД: буквы, цифры, дефис, подчёркивание; прочерк и «demo» не считаются
ERID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-]{3,}")

#: встроенные пакеты. Ключ — код, который приходит в спеке (`compliance.pack`).
BUILTIN: dict[str, dict[str, Any]] = {
    "ru-ad": {
        "title": "РФ: реклама (38-ФЗ ст. 18.1, отчисления в ЕРИР)",
        "jurisdiction": "RU",
        "ad_allowed": True,
        "labels": {"ru": "Реклама", "en": "Ad"},
        "default_language": "ru",
        "erid_required": True,
        "erid_prefix": "erid: ",
        "advertiser_required": True,
        "inn_required": True,
        "age_values": (0, 6, 12, 16, 18),
        "description": "{label}. {advertiser}, ИНН {inn}. erid: {erid}",
        "note": "пометка «Реклама», рекламодатель, ИНН и erid — и в кадре, и в описании",
    },
    "ru-info": {
        "title": "РФ: информирование (не реклама)",
        "jurisdiction": "RU",
        "ad_allowed": False,
        "labels": {"ru": "Реклама", "en": "Ad"},
        "default_language": "ru",
        "erid_required": True,
        "erid_prefix": "erid: ",
        "advertiser_required": True,
        "inn_required": True,
        "age_values": (0, 6, 12, 16, 18),
        "description": "{label}. {advertiser}, ИНН {inn}. erid: {erid}",
        "note": "контент без признаков рекламы: флаг ad запрещён, иначе это обход маркировки",
    },
    "eu-disclosure": {
        "title": "ЕС/UK: раскрытие рекламы (UCPD, ASA)",
        "jurisdiction": "EU",
        "ad_allowed": True,
        "labels": {"en": "Ad", "de": "Anzeige", "ru": "Реклама"},
        "default_language": "en",
        "erid_required": False,
        "erid_prefix": "erid: ",
        "advertiser_required": True,
        "inn_required": False,
        "age_values": (0, 12, 16, 18),
        "description": "{label}. {advertiser}",
        "note": "erid не нужен, но рекламодатель и явная пометка — обязательны",
    },
    "us-ftc": {
        "title": "США: FTC endorsement guides",
        "jurisdiction": "US",
        "ad_allowed": True,
        "labels": {"en": "Paid partnership", "ru": "Реклама"},
        "default_language": "en",
        "erid_required": False,
        "erid_prefix": "erid: ",
        "advertiser_required": False,
        "inn_required": False,
        "age_values": (0, 13, 16, 18, 21),
        "description": "{label}",
        "note": "раскрытие связи («paid partnership»), реквизиты не обязательны",
    },
    "generic": {
        "title": "Без обязательной маркировки: пометка только по факту рекламы",
        "jurisdiction": None,
        "ad_allowed": True,
        "labels": {"ru": "Реклама", "en": "Ad"},
        "default_language": "ru",
        "erid_required": False,
        "erid_prefix": "erid: ",
        "advertiser_required": False,
        "inn_required": False,
        "age_values": ALL_AGES,
        "description": "{label}",
        "note": "рынки, где маркировку не регулируют: плашка ставится, но состав полей свободный",
    },
}

_extra_cache: dict[str, dict[str, Any]] | None = None


def _extra_packs() -> dict[str, dict[str, Any]]:
    """Пакеты рынка из ``MEDIA_COMPLIANCE_PACKS`` (JSON-файл) — без правки кода.

    Формат: ``{"kz-ad": {"title": "...", ...}}``. Файл читается один раз; после правки файла
    в рантайме нужен ``reload_packs()`` — при старте процесса кеш пустой, поэтому деплой
    конфигурации ничего дополнительно не требует.
    """
    global _extra_cache
    if _extra_cache is not None:
        return _extra_cache
    path = os.getenv("MEDIA_COMPLIANCE_PACKS", "").strip()
    data: dict[str, dict[str, Any]] = {}
    if path and os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as handle:
                raw = json.load(handle)
            if isinstance(raw, dict):
                data = {str(key): value for key, value in raw.items()
                        if isinstance(value, dict)}
        except (OSError, ValueError):
            data = {}
    _extra_cache = data
    return data


def reload_packs() -> None:
    """Сбросить кеш внешних пакетов: нужно тестам и hot-reload конфигурации."""
    global _extra_cache
    _extra_cache = None


def registry() -> dict[str, dict[str, Any]]:
    """Все пакеты: встроенные + из файла. Файл может переопределить встроенный."""
    return {**BUILTIN, **_extra_packs()}


def pack(code: str | None) -> dict[str, Any]:
    """Пакет по коду. ``None``/пусто — пакет по умолчанию, неизвестный код — ``KeyError``."""
    name = str(code or DEFAULT_PACK).strip() or DEFAULT_PACK
    try:
        return registry()[name]
    except KeyError:
        raise KeyError(name) from None


def packs() -> list[dict[str, Any]]:
    """Витрина пакетов для API: модели и клиенту нужно знать, что вообще бывает."""
    rows = []
    for code, item in sorted(registry().items()):
        rows.append({
            "code": code, "title": item.get("title"),
            "jurisdiction": item.get("jurisdiction"),
            "ad_allowed": bool(item.get("ad_allowed")),
            "labels": dict(item.get("labels") or {}),
            "requires": [name for name in ("erid", "advertiser", "inn")
                         if item.get(f"{name}_required")],
            "age_values": list(item.get("age_values") or ()),
            "note": item.get("note"),
        })
    return rows


def language(code: str | None, item: dict[str, Any]) -> str:
    """Язык плашки: запрошенный, если у пакета есть такая формулировка, иначе язык пакета."""
    labels = item.get("labels") or {}
    wanted = str(code or "").strip().lower()
    if wanted and wanted in labels:
        return wanted
    fallback = str(item.get("default_language") or "ru").lower()
    return fallback if fallback in labels else (next(iter(labels), "ru"))


def label(item: dict[str, Any], code: str | None = None) -> str:
    """Пометка словами: «Реклама» / «Ad» / «Anzeige» — из пакета, а не из рендерера."""
    return (item.get("labels") or {}).get(language(code, item), "Реклама")


def _issue(code: str, path: str, message: str) -> dict[str, Any]:
    return {"code": code, "path": path, "message": message}


def validate(compliance: dict | None) -> list[dict[str, Any]]:
    """Проверить маркировку по пакету. Пустой список — рендер разрешён.

    Коды ошибок стабильны и используются в API и тестах: ``erid_required``,
    ``advertiser_required``, ``inn_invalid``, ``min_age_invalid``, ``pack_unknown``,
    ``ad_forbidden_by_pack``, ``erid_without_ad``, ``erid_invalid``, ``disclaimer_too_long``.
    """
    data = compliance if isinstance(compliance, dict) else {}
    if compliance is not None and not isinstance(compliance, dict):
        return [_issue("compliance_invalid", "compliance", "compliance должен быть объектом")]

    issues: list[dict[str, Any]] = []
    try:
        item = pack(data.get("pack"))
    except KeyError:
        return [_issue("pack_unknown", "compliance.pack",
                       f"пакет {data.get('pack')!r} неизвестен: {sorted(registry())}")]

    min_age = data.get("min_age")
    if min_age is not None:
        allowed = tuple(item.get("age_values") or ALL_AGES)
        if min_age not in allowed:
            issues.append(_issue(
                "min_age_invalid", "compliance.min_age",
                f"возрастная метка {min_age} не из пакета {item.get('title')}: "
                f"допустимо {', '.join(str(value) for value in allowed)}"))

    disclaimer = str(data.get("disclaimer") or "").strip()
    if len(disclaimer) > MAX_DISCLAIMER_CHARS:
        issues.append(_issue(
            "disclaimer_too_long", "compliance.disclaimer",
            f"дисклеймер длиннее {MAX_DISCLAIMER_CHARS} знаков: на телефоне не читается"))

    ad = bool(data.get("ad"))
    erid = str(data.get("erid") or "").strip()
    advertiser = str(data.get("advertiser") or "").strip()
    inn = str(data.get("inn") or "").strip()

    if not ad:
        # erid без флага рекламы — не «перестраховка», а рассинхрон: плашка будет, признака нет
        if erid:
            issues.append(_issue("erid_without_ad", "compliance.erid",
                                 "указан erid, но ad=false: маркировка без признака рекламы"))
        return issues

    if not item.get("ad_allowed"):
        issues.append(_issue(
            "ad_forbidden_by_pack", "compliance.ad",
            f"пакет {item.get('title')} не допускает рекламу: этот контент — информирование"))
        return issues

    if item.get("erid_required"):
        if not erid:
            issues.append(_issue("erid_required", "compliance.erid",
                                 "реклама без erid не маркируется — рендер запрещён"))
        elif not ERID_RE.fullmatch(erid):
            issues.append(_issue("erid_invalid", "compliance.erid",
                                 "erid — токен из ОРД, а не прочерк: минимум 4 знака"))
    if item.get("advertiser_required") and not advertiser:
        issues.append(_issue("advertiser_required", "compliance.advertiser",
                             "нужен рекламодатель: юрлицо или ИП"))
    if item.get("inn_required") and not INN_RE.fullmatch(inn):
        issues.append(_issue("inn_invalid", "compliance.inn",
                             "ИНН рекламодателя — 10 или 12 цифр"))
    return issues


def plates(compliance: dict | None, *, language_code: str | None = None) -> list[dict[str, Any]]:
    """Плашки для кадра: что именно рисовать. Пустой список — маркировка не нужна.

    Порядок фиксирован: сначала плашка рекламы (она обязательна), затем возрастная метка,
    затем дисклеймер. Рендерер решает, где это стоит, но не решает, что это за текст.
    """
    data = compliance if isinstance(compliance, dict) else {}
    try:
        item = pack(data.get("pack"))
    except KeyError:
        return []
    rows: list[dict[str, Any]] = []

    if data.get("ad") and item.get("ad_allowed"):
        word = label(item, language_code or data.get("lang"))
        erid = str(data.get("erid") or "").strip()
        head = f"{word} · {item.get('erid_prefix') or ''}{erid}" if erid else word
        lines = [head]
        requisites = []
        advertiser = str(data.get("advertiser") or "").strip()
        if advertiser:
            requisites.append(advertiser)
        inn = str(data.get("inn") or "").strip()
        if inn:
            requisites.append(f"ИНН {inn}")
        if requisites:
            lines.append(" · ".join(requisites))
        rows.append({"kind": "ad", "lines": lines, "tone": "amber"})

    if data.get("min_age") is not None:
        age = data.get("min_age")
        rows.append({"kind": "age", "lines": [f"{age}+"], "tone": "muted"})

    disclaimer = str(data.get("disclaimer") or "").strip()
    if disclaimer:
        rows.append({"kind": "disclaimer", "lines": [disclaimer], "tone": "muted"})
    return rows


def description(compliance: dict | None, *, language_code: str | None = None) -> str | None:
    """Текст маркировки для описания канала — из тех же данных, что и плашка.

    Возвращает ``None``, если маркировать нечего: пустая строка в описании — это тоже
    дезинформация, поэтому «нет» отличается от «пусто».
    """
    data = compliance if isinstance(compliance, dict) else {}
    try:
        item = pack(data.get("pack"))
    except KeyError:
        return None
    if not data.get("ad") or not item.get("ad_allowed"):
        tail = str(data.get("disclaimer") or "").strip()
        return tail or None
    template = str(item.get("description") or "{label}")
    text = template.format(
        label=label(item, language_code or data.get("lang")),
        advertiser=str(data.get("advertiser") or "").strip(),
        inn=str(data.get("inn") or "").strip(),
        erid=str(data.get("erid") or "").strip(),
    )
    # реквизиты, которых нет в пакете, не должны оставлять «, ИНН » в описании
    text = re.sub(r"\s*·\s*(?=·)|,\s*(?=,)|[,.]\s*$", "", text)
    text = re.sub(r"[,;]\s*(?=(?:erid|ИНН)\s*[:.]?\s*(?:$|[,.·]))", "", text)
    text = re.sub(r"\s{2,}", " ", text).strip(" ,;·")
    disclaimer = str(data.get("disclaimer") or "").strip()
    if disclaimer:
        text = f"{text}\n{disclaimer}"
    return text


def resolve(compliance: dict | None, *, language_code: str | None = None) -> dict[str, Any]:
    """Всё про маркировку одним объектом: для задачи ролика, API и гейтов."""
    data = compliance if isinstance(compliance, dict) else {}
    try:
        item = pack(data.get("pack"))
        found = True
    except KeyError:
        item, found = {}, False
    issues = validate(data)
    rows = plates(data, language_code=language_code)
    return {
        "pack": str(data.get("pack") or DEFAULT_PACK) if found else str(data.get("pack")),
        "title": item.get("title"),
        "jurisdiction": item.get("jurisdiction"),
        "label": label(item, language_code or data.get("lang")) if found else None,
        "language": language(data.get("lang"), item) if found else None,
        "ad": bool(data.get("ad")),
        "min_age": data.get("min_age"),
        "plates": rows,
        "description": description(data, language_code=language_code),
        "issues": issues,
        "blocked": bool(issues),
    }


def reserve_height(rows: list[dict[str, Any]], *, line_height: int, gap: int = 14) -> int:
    """Сколько пикселей по высоте занимает маркировка — рендерер освобождает их заранее.

    Плашка не должна наезжать на смысловой текст: безопаснее ужать контент, чем поставить
    «Реклама» поверх титров.
    """
    height = 0
    for row in rows:
        if row.get("kind") == "age":
            continue                                  # возрастная метка идёт в угол, не в стопку
        height += len(row.get("lines") or []) * line_height + gap
    return height
