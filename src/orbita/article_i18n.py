"""Мультиязычная статья: перевод данными, «хром» кодом, `hreflang` как часть артефакта (I7).

Задача этапа: из одной спеки получается сайт на четырёх языках. Из этого следуют три решения.

**1. Движок не переводит.** Перевод приходит данными (`translations`): так его можно проверить
редактором и сохранить в истории. Здесь код делает то, что машина делает лучше человека:
подписи интерфейса («Читайте также», «источник», «мин чтения»), пересчёт времени чтения по
профилю языка, `lang` и `hreflang`, лимиты длины заголовка под язык.

**2. Непереведённое называется.** Блок без перевода попадает в `missing` с адресом
(`blocks[blk_x].text`), а спека получает `translation.complete = false`. В строгом режиме такая
версия не публикуется (`translation_missing`): полстраницы по-русски на английской версии —
худший вид локализации. Частичный перевод — законный черновик, но не публикация.

**3. Профиль языка — данные, и он не тот же, что у ролика.** `media_locale` хранит темп чтения
с экрана (2.6 слова/с у русского) — это про титры, а не про статью. Для статьи темп — слова в
минуту (180 ru / 220 en / 160 kk / 170 uz): иначе статья на 1 200 слов «читается 8 минут»
и обещает читателю не то. Лимиты заголовка тоже разные: 70 знаков у ru и 60 у en — потому что
английский заголовок в выдаче обрезается раньше.

Неизвестный язык — `locale_unknown`, а не тихий откат на русский: «перевели на en, а вышло
по-русски» хуже, чем честный отказ.
"""
from __future__ import annotations

import json
from typing import Any, Iterable

from . import media_locale

#: Параметры статьи по языкам: темп чтения (слова/мин), лимиты заголовка и описания.
#: Коды совпадают с `media_locale`, но числа — свои: темп титров и темп чтения статьи разные.
ARTICLE_LOCALES: dict[str, dict[str, Any]] = {
    "ru": {"words_per_minute": 180, "title_limit": 70, "description_limit": 160},
    "en": {"words_per_minute": 220, "title_limit": 60, "description_limit": 155},
    "kk": {"words_per_minute": 160, "title_limit": 70, "description_limit": 160},
    "uz": {"words_per_minute": 170, "title_limit": 70, "description_limit": 160},
}

#: Подписи интерфейса: их переводит код, а не переводчик — они одинаковы во всех статьях языка.
UI_STRINGS: dict[str, dict[str, str]] = {
    "ru": {"related": "Читайте также", "source": "источник", "reading_time": "мин чтения",
           "words": "слов", "visuals": "визуалов", "updated": "обновлено",
           "faq": "Вопросы и ответы", "translated": "перевод"},
    "en": {"related": "Read next", "source": "source", "reading_time": "min read",
           "words": "words", "visuals": "visuals", "updated": "updated",
           "faq": "Questions and answers", "translated": "translation"},
    "kk": {"related": "Осыны оқыңыз", "source": "дереккөз", "reading_time": "мин оқу",
           "words": "сөз", "visuals": "көрнекілік", "updated": "жаңартылды",
           "faq": "Сұрақ-жауап", "translated": "аударма"},
    "uz": {"related": "Buni ham o‘qing", "source": "manba", "reading_time": "daq. o‘qish",
           "words": "so‘z", "visuals": "ko‘rinish", "updated": "yangilandi",
           "faq": "Savol-javob", "translated": "tarjima"},
}

#: Поля блоков, которые вообще могут иметь перевод. `rows`/`items`/`steps` — списки строк.
TEXT_FIELDS = ("text", "alt", "caption", "title", "author", "label", "value", "unit")
LIST_FIELDS = ("items", "steps", "columns")
#: блоки, которые не переводятся: данные и разметка живут одинаково на всех языках
SYSTEM_BLOCKS = ("related",)


def profile(code: str | None) -> dict[str, Any] | None:
    """Профиль языка статьи: коды берём из `media_locale`, числа — из `ARTICLE_LOCALES`."""
    if not code:
        return None
    key = str(code).strip().lower()
    if key not in media_locale.LOCALES:
        return None
    base = dict(media_locale.LOCALES[key])
    base.update(ARTICLE_LOCALES.get(key) or {})
    base["code"] = key
    return base


def catalog() -> list[dict[str, Any]]:
    """Витрина языков статьи: для интерфейса, а не «список констант в коде»."""
    rows: list[dict[str, Any]] = []
    for code in media_locale.codes():
        row = profile(code) or {}
        rows.append({"code": code, "title": row.get("title"),
                     "words_per_minute": row.get("words_per_minute"),
                     "title_limit": row.get("title_limit"),
                     "description_limit": row.get("description_limit"),
                     #: темп титров ролика оставлен рядом намеренно: это разные величины
                     "reel_words_per_second": row.get("words_per_second"),
                     "ui": sorted(UI_STRINGS.get(code, {}))})
    return rows


def ui_strings(code: str | None) -> dict[str, str]:
    return dict(UI_STRINGS.get(str(code or "").strip().lower()) or UI_STRINGS["ru"])


def _plain_words(text: str) -> int:
    return len([word for word in str(text or "").replace("\n", " ").split(" ") if word.strip()])


def reading(spec: dict, code: str | None = None) -> dict:
    """Время чтения по языку: слова в минуту из профиля статьи, а не темп титров."""
    locale = str(code or spec.get("locale") or media_locale.DEFAULT_LOCALE).lower()
    row = profile(locale) or profile("ru") or {}
    words = int((spec.get("stats") or {}).get("words") or 0) or _plain_words(
        _collect_text(spec))
    wpm = int(row.get("words_per_minute") or 180)
    minutes = max(1, round(words / wpm))
    return {"locale": locale, "words": words, "words_per_minute": wpm, "minutes": minutes,
            "label": f"{minutes} {ui_strings(locale)['reading_time']}"}


def _collect_text(spec: dict) -> str:
    from .articles import _plain_text

    return _plain_text(spec)


def _block_text_keys(block: dict) -> list[str]:
    props = block.get("props") or {}
    keys = [field for field in TEXT_FIELDS if str(props.get(field) or "").strip()]
    keys.extend(field for field in LIST_FIELDS if props.get(field))
    return keys


def translate(spec: dict, locale: str, *, translations: dict | None = None,
              strict: bool = True, article_id: str | None = None) -> dict:
    """Перевод спеки по данным: `translations` — то, что дал переводчик, остальное — `missing`.

    Структура `translations`:

    ```python
    {"seo": {"title": "...", "description": "...", "keywords": [...]},
     "blocks": {"blk_1234": {"text": "..."}, "blk_5678": {"items": [...], "columns": [...]}},
     "cta": {"text": "...", "url": "/pricing"}}
    ```

    Ключ блока — `id` из спеки: перевод не привязан к порядку блоков и не съезжает, если
    редактор вставил абзац в русскую версию.
    """
    code = str(locale or "").strip().lower()
    row = profile(code)
    if row is None:
        return {"ok": False, "code": "locale_unknown", "spec": spec,
                "reason": f"язык «{code or '—'}» не поддержан: доступны "
                          f"{', '.join(media_locale.codes())}"}
    source_locale = str(spec.get("locale") or media_locale.DEFAULT_LOCALE).lower()
    if code == source_locale:
        return {"ok": False, "code": "already_localized", "spec": spec,
                "reason": f"статья уже на языке {code}"}
    data = translations or {}
    local = json.loads(json.dumps(spec, ensure_ascii=False))
    missing: list[dict] = []
    issues: list[dict] = []
    applied: list[str] = []

    seo_in = data.get("seo") or {}
    for field, limit in (("title", row["title_limit"]), ("description", row["description_limit"])):
        value = str(seo_in.get(field) or "").strip()
        if not value:
            missing.append({"field": f"seo.{field}", "code": "translation_missing",
                            "message": f"нет перевода поля seo.{field}"})
            continue
        if len(value) > int(limit):
            issues.append({"code": "title_too_long_locale", "field": f"seo.{field}",
                           "message": f"перевод длиннее лимита языка {code}: "
                                      f"{len(value)} > {limit}"})
        local.setdefault("seo", {})[field] = value
        applied.append(f"seo.{field}")
    keywords = seo_in.get("keywords")
    if keywords:
        local.setdefault("seo", {})["keywords"] = [str(word).strip() for word in keywords
                                                  if str(word).strip()]
        applied.append("seo.keywords")

    blocks_in = data.get("blocks") or {}
    for block in local.get("blocks") or []:
        kind = block.get("type")
        if kind in SYSTEM_BLOCKS:
            continue
        keys = _block_text_keys(block)
        given = blocks_in.get(block.get("id")) or {}
        props = block.setdefault("props", {})
        for field in keys:
            if field not in given:
                missing.append({"field": f"blocks[{block.get('id')}].{field}",
                                "code": "translation_missing",
                                "message": f"блок «{kind}» без перевода поля {field}"})
                continue
            props[field] = given[field]
            applied.append(f"blocks[{block.get('id')}].{field}")
        unknown = set(given) - set(keys) - set(TEXT_FIELDS) - set(LIST_FIELDS)
        for field in sorted(unknown):
            issues.append({"code": "unknown_field",
                           "field": f"blocks[{block.get('id')}].{field}",
                           "message": f"в блоке «{kind}» нет поля {field}: "
                                      f"перевод некуда положить"})

    cta_in = data.get("cta") or {}
    if cta_in:
        cta = local.setdefault("cta", {})
        for field in ("text", "url"):
            if str(cta_in.get(field) or "").strip():
                cta[field] = cta_in[field]
                applied.append(f"cta.{field}")

    # объём и время чтения считаются по переведённому тексту: переносить русские «390 слов»
    # в английскую версию — значит обещать читателю не то время
    words = _plain_words(_collect_text(local))
    local["stats"] = {**(spec.get("stats") or {}), "words": words,
                      "reading_minutes": max(1, round(words / int(row["words_per_minute"])))}
    labels = ui_strings(code)
    #: перевод — отдельная статья: свой id и свой адрес. Иначе `publish()` перезаписал бы
    #: русскую версию английской, и группа `hreflang` осталась бы из одной страницы
    source_id = str(spec.get("id") or "art")
    local["id"] = f"{source_id}_{code}"
    local["slug"] = f"{(spec.get('slug') or 'article').strip('-')}-{code}"
    local["labels"] = labels
    local["locale"] = code
    local["translation_of"] = article_id or spec.get("translation_of") or spec.get("id")
    local["translation"] = {"source_locale": source_locale, "missing": missing,
                            "complete": not missing}
    local["reading"] = reading(local, code)
    if missing and strict:
        return {"ok": False, "code": "translation_missing", "spec": spec,
                "missing": missing, "issues": issues, "applied": applied,
                "reason": f"переведено не всё: {len(missing)} полей без перевода — "
                          f"частичная версия не публикуется"}
    return {"ok": True, "code": None, "spec": local, "missing": missing, "issues": issues,
            "applied": applied, "partial": bool(missing),
            "reason": (f"перевод на {code}: {len(applied)} полей, "
                       + ("все поля переведены" if not missing
                          else f"без перевода осталось {len(missing)}"))}


def hreflang(spec: dict, urls: dict[str, str], *, default: str | None = None) -> dict:
    """Набор `hreflang` для группы переводов: свой адрес, соседи и `x-default`.

    Правило без исключений: у каждой версии есть ссылка на себя. Без неё поисковик не связывает
    группу, и четыре версии конкурируют друг с другом.
    """
    own = str(spec.get("locale") or media_locale.DEFAULT_LOCALE).lower()
    rows: list[dict] = []
    for code, url in sorted(urls.items()):
        key = str(code).strip().lower()
        if profile(key) is None:
            return {"ok": False, "code": "locale_unknown", "rows": [],
                    "reason": f"язык «{key}» не поддержан"}
        href = str(url or "").strip()
        if not href:
            return {"ok": False, "code": "alternate_url_missing", "rows": [],
                    "reason": f"у версии {key} нет адреса"}
        rows.append({"hreflang": key, "href": href, "self": key == own})
    if not any(row["self"] for row in rows):
        return {"ok": False, "code": "hreflang_self_missing", "rows": rows,
                "reason": "в наборе нет ссылки на собственную версию"}
    default_code = str(default or own).strip().lower()
    if default_code not in urls:
        return {"ok": False, "code": "hreflang_default_missing", "rows": rows,
                "reason": f"нет версии по умолчанию ({default_code})"}
    rows.append({"hreflang": "x-default", "href": str(urls[default_code]), "self": False})
    return {"ok": True, "code": None, "rows": rows, "own": own, "default": default_code,
            "reason": f"{len(rows) - 1} языков + x-default: у каждой версии есть ссылка на себя"}


def check_i18n(spec: dict) -> list[dict]:
    """Проверки перевода: язык, полнота, hreflang, лимит заголовка под язык."""
    issues: list[dict] = []
    code = str(spec.get("locale") or "").strip().lower()
    row = profile(code)
    if code and row is None:
        issues.append({"code": "lang_unknown", "field": "locale",
                       "message": f"язык «{code}» не из реестра"})
        return issues
    mark = spec.get("translation") or {}
    if mark and not mark.get("complete", True):
        missing = mark.get("missing") or []
        issues.append({"code": "translation_missing", "field": "translation.missing",
                       "message": f"без перевода {len(missing)} полей — версия неполная"})
    title = str((spec.get("seo") or {}).get("title") or "")
    limit = int((row or {}).get("title_limit") or 70)
    if title and len(title) > limit:
        issues.append({"code": "title_too_long_locale", "field": "seo.title",
                       "message": f"заголовок {len(title)} знаков при лимите языка {code}: {limit}"})
    rows = spec.get("hreflang") or []
    if rows:
        codes = {str(row.get("hreflang") or "").lower() for row in rows}
        for value in codes - {"x-default"}:
            if profile(value) is None:
                issues.append({"code": "hreflang_unknown_locale", "field": "hreflang",
                               "message": f"hreflang «{value}» не из реестра"})
        if code and code not in codes:
            issues.append({"code": "hreflang_self_missing", "field": "hreflang",
                           "message": "в наборе нет ссылки на собственный язык"})
        if "x-default" not in codes:
            issues.append({"code": "hreflang_default_missing", "field": "hreflang",
                           "message": "нет x-default: неясно, какую версию показывать остальным"})
    return issues


def matrix(spec: dict, locales: Iterable[str], translations: dict[str, dict] | None = None,
           *, urls: dict[str, str] | None = None, strict: bool = True,
           article_id: str | None = None) -> dict:
    """Перевод во все языки сразу: что получилось, что не влезло, что не переведено."""
    rows: list[dict] = []
    made: dict[str, dict] = {}
    for code in locales:
        result = translate(spec, code, translations=(translations or {}).get(code),
                           strict=strict, article_id=article_id)
        rows.append({"locale": str(code).lower(), "ok": result.get("ok"),
                     "code": result.get("code"),
                     "title": ((result.get("spec") or {}).get("seo") or {}).get("title"),
                     "minutes": (result.get("spec") or {}).get("reading", {}).get("minutes"),
                     "missing": len(result.get("missing") or []),
                     "issues": len(result.get("issues") or []),
                     "applied": len(result.get("applied") or [])})
        if result.get("ok"):
            made[str(code).lower()] = result["spec"]
    group = None
    if urls:
        group = hreflang(spec, urls)
        if group.get("ok"):
            for code, localized in made.items():
                localized["hreflang"] = group["rows"]
    complete = [code for code, row in made.items()
                if (row.get("translation") or {}).get("complete")]
    return {"ok": bool(made), "rows": rows, "specs": made, "hreflang": group,
            "complete": complete, "total": len(rows),
            "reason": f"переведено полностью: {len(complete)} из {len(rows)} языков"}
