"""Редакционные статьи: спека, структура, визуальные блоки и проверки (этап I, ADR 0024).

Задача этапа: не «сгенерировать текст», а собрать **готовую публикацию** — с продуманной
структурой, визуальными блоками по смыслу и SEO, пригодную к публикации без ручной правки.
Из этого следует устройство модуля.

**1. Статья — это спека, а не HTML от модели.** `article.spec.v1` — данные: SEO-поля, блоки,
визуалы, источники. HTML собирает код (`render_html`), поэтому вёрстка не зависит от того, что
«придумала» модель, а правка дизайна — это правка одного рендерера, а не перегенерация статей.

**2. Блоки — закрытый реестр.** `BLOCKS` перечисляет типы: вводка, заголовки, абзацы, списки,
цитата, таблица, график, схема, изображение, галерея, сравнение, врезка, видео, FAQ, цифра.
Новый тип блока — запись в реестре + функция рендера, а не ветка «если это статья про X».

**3. Факты — из материалов, а не из модели.** Числа, цитаты и ссылки берутся из `materials`
(что дал клиент: статистика, отчёты, документы). Модель пишет связки и структуру; **каждая цифра
в графике или блоке «цифра» обязана ссылаться на материал** (`source`). Статья, которая сама
придумала охваты, — это не статья, а реклама.

**4. Визуал ставится по смыслу и объясняет себя.** У каждого визуального блока есть `reason`
(«здесь таблица, потому что сравниваются три тарифа») и `alt`. Проверка требует и то и другое:
картинка «для красоты» без объяснения — первый признак сгенерированного текста.

**5. Уникальность проверяема.** `uniqueness()` считает долю общих шинглов (5 слов) с корпусом
(архив блога, ранее опубликованные статьи). Порог — данные, а не ощущение.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from . import article_i18n, article_links, media, media_locale, media_reel
from .docs import Collection

SPEC_VERSION = "article.spec.v1"
DEFAULT_LOCALE = "ru"

#: минимальный и максимальный объём: короче — не статья, длиннее — не читают
MIN_WORDS, MAX_WORDS = 350, 4000
#: доля общих шинглов с корпусом, выше которой текст считается неуникальным
MAX_DUPLICATE_RATIO = 0.35
#: шингл: 5 слов — стандарт для проверки уникальности без ложных совпадений на формулах
SHINGLE = 5

_articles = Collection("articles")

#: Реестр блоков. `visual` — блок требует `alt` и `reason`; `needs` — какие поля обязательны;
#: `title` — человеческое имя для интерфейса и отчёта. Новый блок = запись + рендер.
BLOCKS: dict[str, dict[str, Any]] = {
    "lead": {"title": "Вводка", "visual": False, "needs": ("text",)},
    "heading": {"title": "Подзаголовок", "visual": False, "needs": ("text", "level")},
    "paragraph": {"title": "Абзац", "visual": False, "needs": ("text",)},
    "list": {"title": "Список", "visual": False, "needs": ("items",)},
    "quote": {"title": "Цитата", "visual": False, "needs": ("text", "author")},
    "table": {"title": "Таблица", "visual": True, "needs": ("columns", "rows")},
    "chart": {"title": "График", "visual": True, "needs": ("kind", "series")},
    "diagram": {"title": "Схема", "visual": True, "needs": ("steps",)},
    "image": {"title": "Иллюстрация", "visual": True, "needs": ("prompt",)},
    "gallery": {"title": "Галерея", "visual": True, "needs": ("items",)},
    "comparison": {"title": "Сравнение", "visual": True, "needs": ("columns",)},
    "callout": {"title": "Врезка", "visual": False, "needs": ("text",)},
    "stat": {"title": "Цифра", "visual": True, "needs": ("value", "label")},
    "video": {"title": "Видео", "visual": True, "needs": ("title",)},
    "faq": {"title": "Вопросы и ответы", "visual": False, "needs": ("items",)},
    "related": {"title": "Читайте также", "visual": False, "needs": ("items",)},
    "cta": {"title": "Призыв", "visual": False, "needs": ("text",)},
}
#: порядок блоков, которые допустимо ставить «по делу», а не по сюжету
INLINE_VISUALS = ("table", "chart", "diagram", "image", "gallery", "comparison", "stat", "video")

#: Правила выбора визуала: сигнал в тексте раздела → блок. Это данные, поэтому новую нишу
#: (например, «рецепт») можно описать записью, а не правкой генератора.
VISUAL_RULES: tuple[dict[str, Any], ...] = (
    {"code": "prices", "block": "table", "data": ("rows",), "strict_data": True,
     "signals": ("тариф", "цена", "стоимост", "прайс", "₽"),
     "reason": "здесь сравниваются варианты — таблица читается быстрее абзаца"},
    {"code": "trend", "block": "chart", "data": ("material",),
     "signals": ("рост", "динамик", "за год", "процент", "%"),
     #: причина уточняется видом графика: столбцы сравнивают, линия показывает движение
     "reasons": {"bar": "сравнение чисел по каналам — столбцы показывают разницу в одну секунду"},
     "reason": "есть числовой ряд — график показывает динамику, которую текст описывает словами"},
    {"code": "process", "block": "diagram", "data": ("steps",), "strict_data": True,
     "signals": ("шаг", "этап", "процесс", "как работает",
                                                       "инструкц"),
     "reason": "описан процесс — схема снимает необходимость держать шаги в голове"},
    {"code": "compare", "block": "comparison", "data": ("columns",),
     "signals": ("сравн", "против", "плюсы", "минусы",
                                                           "альтернатив"),
     "reason": "два и более вариантов — блок сравнения показывает разницу без воды"},
    {"code": "proof", "block": "stat", "signals": ("исследован", "опрос", "отчёт", "данные"),
     "reason": "в разделе есть цифра из источника — её нужно показать, а не спрятать в абзаце"},
    {"code": "collection", "block": "gallery", "data": ("gallery",), "strict_data": True,
     "signals": ("подборк", "галере", "несколько примеров", "варианты оформлен"),
     "reason": "несколько примеров рядом — галерея показывает их без абзаца перечислений"},
    {"code": "demo", "block": "video", "data": ("video",), "strict_data": True,
     "signals": ("видео", "ролик", "демонстрац", "покажем в движении"),
     "reason": "раздел про движение — короткий ролик из спеки показывает то, что текст "
               "описывает словами"},
    {"code": "show", "block": "image", "data": ("image_prompt",),
     "signals": ("пример", "как выглядит", "интерфейс", "фото"),
     "reason": "раздел про то, как это выглядит — иллюстрация объясняет быстрее текста"},
)


# ------------------------------------------------------------------ паки
#: Пакет правил: то, что отличает нишу, — данные. Пак расширяет/отключает правила выбора
#: визуала и задаёт политику публикации (объём, обязательные блоки, запрещённые обещания,
#: оговорку). Встроенный `default` — то, что работает без настройки.
RULE_PACKS: dict[str, dict[str, Any]] = {
    "default": {
        "title": "Обычные темы",
        "description": "Базовые правила: визуал по данным раздела, цифры — с источником",
        "rules": [],
        "policy": {},
    },
    "high-stakes": {
        "title": "Темы с высокой ценой ошибки",
        "description": ("Право, финансы, здоровье, безопасность: оговорка обязательна, цифры — "
                        "только с источником, обещания результата запрещены"),
        "rules": [
            {"code": "risk", "block": "callout", "data": ("callout",), "strict_data": True,
             "signals": ("риск", "ответственност", "последстви", "ошибк"),
             "reason": "в разделе есть риск ошибки — врезка отделяет факт от оговорки"},
        ],
        "policy": {
            "min_words": 500,
            "max_duplicate_ratio": 0.2,
            "required_blocks": ["callout"],
            "require_sources": ["chart", "table", "stat", "comparison"],
            "banned_phrases": ["гарантируем результат", "100% гарантия", "полностью безопасно"],
            "disclaimer": ("Материал носит информационный характер и не заменяет консультацию "
                           "специалиста по вашему случаю."),
        },
    },
}

#: Шаблон публикации: токены вёрстки, а не отдельный рендерер. Новый шаблон = запись.
TEMPLATES: dict[str, dict[str, Any]] = {
    "editorial": {
        "title": "Редакционный",
        "description": "Светлая сетка, акцент бренда, плотная типографика",
        "tokens": {"accent": "#7c5cff", "bg": "#ffffff", "fg": "#111318", "dim": "#5b6270",
                   "line": "#e6e8ee", "font": ('-apple-system, Segoe UI, Roboto, Arial, '
                                               'sans-serif'),
                   "max_width": "760px", "radius": "12px", "h1": "34px",
                   "callout_bg": "#f4f2ff", "callout_line": "#ded8ff",
                   "stat_bg": "#111318", "stat_fg": "#ffffff", "stat_note": "#b9c0cf",
                   "table_head": "#f7f8fb"},
    },
    "longread": {
        "title": "Лонгрид",
        "description": "Тёплый фон, антиква в тексте, широкий заголовок — для чтения 8–15 минут",
        "tokens": {"accent": "#0f766e", "bg": "#fbfaf7", "fg": "#1a1a17", "dim": "#5f6259",
                   "line": "#e7e3d8", "font": 'Georgia, "Times New Roman", serif',
                   "max_width": "720px", "radius": "8px", "h1": "38px",
                   "callout_bg": "#f1f0e6", "callout_line": "#ded9c6",
                   "stat_bg": "#12312c", "stat_fg": "#f4fffb", "stat_note": "#a9c9c1",
                   "table_head": "#f2f0e8"},
    },
}

DEFAULT_PACK = "default"
DEFAULT_TEMPLATE = "editorial"

_pack_cache: dict[str, dict[str, Any]] | None = None
_template_cache: dict[str, dict[str, Any]] | None = None


def _load_file(env_name: str) -> dict[str, dict[str, Any]]:
    """Пак из JSON-файла (``ARTICLE_RULE_PACKS`` / ``ARTICLE_TEMPLATES``): формат — словарь."""
    import os

    path = os.getenv(env_name, "").strip()
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(key): value for key, value in raw.items() if isinstance(value, dict)}


def _extra_packs() -> dict[str, dict[str, Any]]:
    global _pack_cache
    if _pack_cache is None:
        _pack_cache = _load_file("ARTICLE_RULE_PACKS")
    return _pack_cache


def _extra_templates() -> dict[str, dict[str, Any]]:
    global _template_cache
    if _template_cache is None:
        _template_cache = _load_file("ARTICLE_TEMPLATES")
    return _template_cache


def reload_packs() -> None:
    """Сбросить кеш паков и шаблонов: нужно тестам и подмене конфигурации на лету."""
    global _pack_cache, _template_cache
    _pack_cache = None
    _template_cache = None


def rule_registry() -> dict[str, dict[str, Any]]:
    """Все пакеты правил: встроенные + из файла (файл может переопределить встроенный)."""
    return {**RULE_PACKS, **_extra_packs()}


def template_registry() -> dict[str, dict[str, Any]]:
    return {**TEMPLATES, **_extra_templates()}


def _invalid_rules(pack: dict[str, Any]) -> list[str]:
    """Правила пака, которые нельзя применить: блок не из реестра визуалов или нет причины."""
    bad = []
    for rule in pack.get("rules") or []:
        if not isinstance(rule, dict) or not rule.get("code"):
            bad.append(str(rule)[:40])
        elif rule.get("block") not in BLOCKS or not rule.get("reason"):
            bad.append(str(rule.get("code")))
    return bad


def rule_pack(code: str | None) -> dict[str, Any]:
    """Пакет правил по коду. Неизвестный код — ``KeyError``: молча подменять политику нельзя."""
    name = str(code or DEFAULT_PACK).strip() or DEFAULT_PACK
    try:
        return rule_registry()[name]
    except KeyError:
        raise KeyError(name) from None


def rules_for(code: str | None = None) -> tuple[dict[str, Any], ...]:
    """Правила выбора визуала для пака: встроенные (минус отключённые) + правила пака."""
    pack = rule_pack(code)
    disabled = {str(item) for item in pack.get("disable") or ()}
    merged: dict[str, dict[str, Any]] = {rule["code"]: dict(rule) for rule in VISUAL_RULES
                                         if rule["code"] not in disabled}
    for rule in pack.get("rules") or []:
        if _invalid_rules({"rules": [rule]}):
            continue                        # правило, которое нельзя применить, не применяется
        merged.setdefault(str(rule["code"]), dict(rule))
    return tuple(merged.values())


def pack_policy(code: str | None = None) -> dict[str, Any]:
    """Политика публикации пака: объём, обязательные блоки, запрещённые фразы, оговорка."""
    return dict(rule_pack(code).get("policy") or {})


def rule_packs() -> list[dict[str, Any]]:
    """Витрина паков: клиенту и модели нужно знать, что бывает, до генерации."""
    rows = []
    for code, pack in sorted(rule_registry().items()):
        policy = pack.get("policy") or {}
        rows.append({"code": code, "title": pack.get("title"),
                     "description": pack.get("description"),
                     "rules": [str(rule.get("code")) for rule in pack.get("rules") or []
                               if isinstance(rule, dict)],
                     "disables": [str(item) for item in pack.get("disable") or ()],
                     "invalid_rules": _invalid_rules(pack),
                     "policy": dict(policy)})
    return rows


def templates() -> list[dict[str, Any]]:
    """Витрина шаблонов: код, назначение и токены вёрстки (без CSS — его собирает рендерер)."""
    return [{"code": code, "title": item.get("title"), "description": item.get("description"),
             "tokens": dict(item.get("tokens") or {})}
            for code, item in sorted(template_registry().items())]


def template(code: str | None) -> dict[str, Any]:
    """Шаблон по коду. Неизвестный код — ``KeyError``, а не «как-нибудь по умолчанию»."""
    name = str(code or DEFAULT_TEMPLATE).strip() or DEFAULT_TEMPLATE
    try:
        return template_registry()[name]
    except KeyError:
        raise KeyError(name) from None


def template_tokens(code: str | None = None) -> dict[str, str]:
    """Токены шаблона, дозаполненные значениями редакционного: шаблон может задать часть."""
    base = dict(TEMPLATES[DEFAULT_TEMPLATE]["tokens"])
    base.update({key: str(value) for key, value in (template(code).get("tokens") or {}).items()})
    return base


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _seed(*parts: Any) -> str:
    payload = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def slugify(text: str) -> str:
    """ЧПУ из заголовка: латиница сохраняется, кириллица транслитерируется."""
    table = {"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
             "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
             "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts",
             "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
             "я": "ya"}
    lowered = str(text or "").lower()
    translit = "".join(table.get(char, char) for char in lowered)
    cleaned = re.sub(r"[^a-z0-9]+", "-", translit).strip("-")
    return re.sub(r"-{2,}", "-", cleaned)[:80] or "article"


def count_words(spec_or_text: Any) -> int:
    """Объём в словах: по тексту блоков, а не по длине HTML."""
    if isinstance(spec_or_text, str):
        return len(spec_or_text.split())
    total = 0
    for block in (spec_or_text or {}).get("blocks") or []:
        props = block.get("props") or {}
        for key in ("text", "title"):
            total += len(str(props.get(key) or "").split())
        for key in ("items", "steps", "rows"):
            value = props.get(key) or []
            if key == "rows":
                total += sum(len(str(cell).split()) for row in value for cell in row)
            else:
                total += sum(len(str(item if not isinstance(item, dict)
                                     else item.get("text") or "").split()) for item in value)
        if block.get("type") == "quote":
            total += len(str(props.get("author") or "").split())
    return total


def reading_minutes(words: int, locale: str | None = None) -> int:
    """Время чтения по темпу языка — тот же профиль, что у ролика (media_locale)."""
    wps = media_locale.words_per_second(locale or DEFAULT_LOCALE)
    return max(1, round(words / (wps * 60)))


# ------------------------------------------------------------------ материалы
def material_numbers(materials: list[dict] | None) -> dict[str, dict]:
    """Числовые материалы по коду: каждый график/цифра должна ссылаться сюда."""
    rows: dict[str, dict] = {}
    for material in materials or []:
        if material.get("kind") != "numbers":
            continue
        code = str(material.get("code") or "").strip()
        if not code:
            continue
        rows[code] = {"code": code, "title": material.get("title") or code,
                      "unit": material.get("unit") or "", "values": material.get("values") or [],
                      "source": material.get("source") or "материалы клиента"}
    return rows


def _rank_rules(section: dict, rules: tuple[dict, ...] | None = None) -> list[dict]:
    """Правила-кандидаты для раздела: сначала те, под которые есть данные.

    Слова в тексте — догадка, данные раздела — факт. Если раздел принёс строки таблицы,
    таблица идёт раньше любого ключевого слова: иначе «пример отчёта» превращался бы в
    цифру только потому, что слово «отчёт» встретилось в правиле `proof`.
    """
    data = section.get("data") or {}
    haystack = " ".join([str(section.get("heading") or ""),
                         str(section.get("intent") or ""),
                         str(section.get("text") or "")]).lower()
    ranked: list[tuple[int, int, dict]] = []
    for order, rule in enumerate(rules or VISUAL_RULES):
        backed = any(key in data and data[key] not in (None, "", [], {})
                     for key in rule.get("data") or ())
        if backed:
            ranked.append((0, order, rule))
        elif rule.get("strict_data"):
            # таблицу и схему нельзя поставить «по слову»: строка «своя цена ошибки»
            # в тексте не даёт ни строк таблицы, ни шагов схемы
            continue
        elif any(signal in haystack for signal in rule["signals"]):
            ranked.append((1, order, rule))
    ranked.sort(key=lambda row: (row[0], row[1]))
    return [rule for _, _, rule in ranked]


def _pick_rule(section: dict, rules: tuple[dict, ...] | None = None) -> dict | None:
    ranked = _rank_rules(section, rules)
    return ranked[0] if ranked else None


# ------------------------------------------------------------------ план
def plan(brief: dict, *, sections: int | None = None, locale: str | None = None) -> dict:
    """Разложить тему на разделы и решить, где нужен визуал и какой.

    План — данные: заголовки, интент раздела, выбранный блок и **причина** выбора. Он
    показывается человеку до генерации текста: правка плана в десять раз дешевле правки статьи.
    """
    topic = str(brief.get("topic") or brief.get("title") or "").strip()
    if not topic:
        return {"ok": False, "code": "topic_required", "reason": "нужна тема статьи"}
    try:
        pack_code = str(brief.get("rule_pack") or DEFAULT_PACK).strip() or DEFAULT_PACK
        pack = rule_pack(pack_code)
    except KeyError:
        return {"ok": False, "code": "pack_unknown",
                "reason": f"пакет правил {brief.get('rule_pack')!r} не из реестра",
                "known": sorted(rule_registry())}
    try:
        template_code = str(brief.get("template") or DEFAULT_TEMPLATE).strip() or DEFAULT_TEMPLATE
        template(template_code)
    except KeyError:
        return {"ok": False, "code": "template_unknown",
                "reason": f"шаблон {brief.get('template')!r} не из реестра",
                "known": sorted(template_registry())}
    wanted_image = str(brief.get("image_provider") or "brand-card").strip() or "brand-card"
    if wanted_image not in IMAGES:
        return {"ok": False, "code": "image_provider_unknown",
                "reason": f"провайдер иллюстраций {wanted_image!r} не из реестра",
                "known": sorted(IMAGES)}
    rules = rules_for(pack_code)
    locale_code = media_locale.resolve(locale or brief.get("locale"))[0]
    wanted = int(sections or brief.get("sections") or 5)
    wanted = max(3, min(9, wanted))
    outline = brief.get("outline") or []
    planned: list[dict] = []
    for index in range(wanted):
        given = outline[index] if index < len(outline) else {}
        heading = str(given.get("heading") or "").strip()
        if not heading:
            heading = _default_heading(topic, index, wanted, brief)
        section = {"index": index + 1, "heading": heading,
                   "intent": given.get("intent") or _default_intent(index, wanted),
                   "text": given.get("text") or "",
                   # данные раздела (строки таблицы, шаги схемы, код материала) едут вместе с
                   # планом: иначе визуал выбирается, а материала для него уже нет
                   "data": {key: value for key, value in given.items()
                            if key not in {"heading", "intent", "text"}}}
        rule = _pick_rule(section, rules)
        section["visual"] = None
        if rule:
            section["visual"] = {"block": rule["block"], "code": rule["code"],
                                 "reason": rule["reason"], "reasons": rule.get("reasons")}
        planned.append(section)
    visuals = [section for section in planned if section["visual"]]
    return {"ok": True, "topic": topic, "locale": locale_code, "sections": planned,
            "rule_pack": pack_code, "image_provider": wanted_image,
            "template": template_code,
            "policy": dict(pack.get("policy") or {}),
            "rules": [rule["code"] for rule in rules],
            "stats": {"sections": len(planned), "visuals": len(visuals),
                      "share": round(len(visuals) / len(planned), 2)},
            "notes": _plan_notes(planned)}


def _default_heading(topic: str, index: int, total: int, brief: dict) -> str:
    """Заголовки по умолчанию: структура «что важно знать → как выбрать → цена → итог»."""
    shapes = ["Что важно знать о теме", "С чего начать", "Как выбрать подходящий вариант",
              "Сколько это стоит", "Частые ошибки", "Как проверить результат",
              "Что дальше", "Коротко"]
    if index == 0:
        return f"{topic}: главное"
    if index == total - 1:
        return "Коротко"
    return shapes[min(index, len(shapes) - 1)]


def _default_intent(index: int, total: int) -> str:
    if index == 0:
        return "ответить на главный вопрос читателя"
    if index == total - 1:
        return "сжать вывод до трёх пунктов"
    return "раскрыть раздел по существу"


def _plan_notes(sections: list[dict]) -> list[str]:
    """Честные замечания к плану: их видно человеку, а не только в логах."""
    notes: list[str] = []
    if sections and sections[0]["visual"]:
        notes.append("первый раздел без визуала читается лучше: читатель ещё решает, читать ли")
    visuals = sum(1 for section in sections if section["visual"])
    if visuals > max(2, len(sections) - 1):
        notes.append("визуалов почти в каждом разделе — страница станет тяжёлой")
    if not visuals:
        notes.append("в плане нет ни одного визуала: тема может не требовать графики")
    return notes


# ------------------------------------------------------------------ сборка
def _block(kind: str, props: dict, *, reason: str | None = None,
           alt: str | None = None, caption: str | None = None,
           source: str | None = None) -> dict:
    if kind not in BLOCKS:
        raise ValueError(f"блок {kind!r} не из реестра")
    block = {"id": f"blk_{_seed(kind, props, reason)[:10]}", "type": kind,
             "props": props, "reason": reason, "alt": alt, "caption": caption,
             "source": source}
    return block


def compose(brief: dict, *, materials: list[dict] | None = None, plan_data: dict | None = None,
            brand: dict | None = None, locale: str | None = None,
            author: dict | None = None, workspace_id: str = "ws_de",
            links: list[str] | None = None) -> dict:
    """Собрать статью по плану и материалам.

    Текст разделов приходит из `brief["outline"][i]["text"]` (его пишет модель), визуалы —
    из материалов и правил. Ни одна цифра не появляется без `source`: если в материале нет
    ряда, график не ставится, а раздел остаётся текстом — честно и предсказуемо.
    """
    planned = plan_data or plan(brief, locale=locale)
    if not planned.get("ok"):
        return planned
    locale_code = planned["locale"]
    numbers = material_numbers(materials)
    topic = planned["topic"]
    image_provider = str(brief.get("image_provider") or "brand-card").strip() or "brand-card"
    pack_code = str(planned.get("rule_pack") or DEFAULT_PACK)
    policy = dict(planned.get("policy") or {})
    active_rules = rules_for(pack_code)
    blocks: list[dict] = []
    sources: list[str] = []

    lead = str(brief.get("lead") or "").strip() or (
        f"{topic}: разбираем по шагам, что важно знать до первого шага, "
        f"сколько это стоит и как проверить результат.")
    blocks.append(_block("lead", {"text": lead}))

    for section in planned["sections"]:
        blocks.append(_block("heading", {"text": section["heading"], "level": 2},
                             reason=section["intent"]))
        text = str(section.get("text") or "").strip()
        if not text:
            text = (f"{section['heading']}. Здесь модель пишет связный текст по существу: "
                    f"факты — из материалов, выводы — из фактов.")
        blocks.append(_block("paragraph", {"text": text}))
        visual = section.get("visual")
        if not visual:
            continue
        # выбранный блок может не материализоваться (раздел обещал график, а ряда нет) —
        # тогда идём по следующим кандидатам, а не оставляем раздел без визуала
        block, tried = None, set()
        for candidate in ([visual]
                          + [rule for rule in _rank_rules(section, active_rules)
                             if rule != visual]):
            if candidate["block"] in tried:
                continue
            tried.add(candidate["block"])
            block = _visual_for(candidate, section, numbers, materials or [], brand,
                                image_provider)
            if block is not None:
                block["section"] = section["index"]
                blocks.append(block)
                if block.get("source"):
                    sources.append(block["source"])
                break

    disclaimer = str(policy.get("disclaimer") or "").strip()
    # оговорка — отдельный блок: врезка раздела про риски её не заменяет
    if disclaimer and disclaimer not in _plain_text({"blocks": blocks}):
        blocks.append(_block("callout", {"text": disclaimer, "tone": "warning"},
                             reason="пакет правил требует оговорку в материале такой темы",
                             alt="Врезка: оговорка"))
    blocks.extend(_closing_blocks(brief, topic, materials or []))
    keywords = [str(word).strip() for word in (brief.get("keywords") or []) if str(word).strip()]
    seo_title = str(brief.get("seo_title") or brief.get("title") or topic).strip()
    slug = slugify(brief.get("slug") or seo_title)
    spec = {
        "spec_version": SPEC_VERSION,
        "id": f"art_{_seed(planned['topic'], blocks, locale_code)}",
        "rule_pack": pack_code,
        "image_provider": image_provider,
        "template": str(planned.get("template") or DEFAULT_TEMPLATE),
        "policy": policy,
        "workspace_id": workspace_id,
        "slug": slug,
        "locale": locale_code,
        "seo": {
            "title": seo_title[:70],
            "description": _description(brief, topic),
            "keywords": keywords,
            "schema": str(brief.get("schema") or "Article"),
            "canonical": canonical_for(brief, slug),
        },
        "blocks": blocks,
        "internal_links": list(links or brief.get("links") or []),
        "cta": {"text": str(brief.get("cta") or "Попробовать в Posiz"),
                "url": brief.get("cta_url") or "/signup"},
        "author": dict(author or {"name": "Редакция Posiz"}),
        "materials": [{"code": row["code"], "title": row["title"], "source": row["source"]}
                      for row in numbers.values()],
        "created_at": _now(),
    }
    if brief.get("evergreen"):
        spec = article_links.evergreen(spec, review_days=brief.get("review_days"))
    if brief.get("related"):
        linked = article_links.apply_links(spec, list(brief["related"]))
        if linked.get("ok"):
            spec = linked["spec"]
    spec["stats"] = _stats(spec, sources)
    spec["checks"] = check(spec)
    return {"ok": True, "plan": planned, "spec": spec, "checks": spec["checks"]}


def canonical_for(brief: dict, slug: str) -> str | None:
    """Адрес статьи для `rel=canonical`: свой, если клиент задал, иначе — из домена платформы.

    Домен не выдумывается: без `ARTICLE_CANONICAL_BASE` canonical нет вовсе. Ссылка «на всякий
    случай» указывает в чужой домен и склеивает выдачу вместо того, чтобы её разделять.
    """
    given = str(brief.get("canonical") or "").strip()
    if given:
        return given
    base = str(os.environ.get("ARTICLE_CANONICAL_BASE") or "").strip().rstrip("/")
    return f"{base}/{slug}/" if base else None


def _description(brief: dict, topic: str) -> str:
    given = str(brief.get("description") or "").strip()
    if given:
        return given[:180]
    return (f"{topic}: структура, цифры с источниками, типичные ошибки и чек-лист. "
            f"Материал с таблицами и схемами — без воды.")[:180]


def _field(section: dict, key: str, default: Any = None) -> Any:
    """Поле раздела: сначала из данных плана, потом из самого раздела."""
    data = section.get("data") or {}
    if key in data:
        return data[key]
    return section.get(key, default)


def _scene_label(shot: dict) -> str:
    """Подпись сцены в сториборде: главное поле шаблона, а не имя шаблона."""
    props = shot.get("props") or {}
    for key in ("headline", "cta", "captions"):
        if props.get(key):
            return str(props[key])[:42]
    if props.get("items"):
        return " · ".join(str(item) for item in props["items"][:2])[:42]
    return str(shot.get("template") or "")


def _visual_for(visual: dict, section: dict, numbers: dict[str, dict],
                materials: list[dict], brand: dict | None,
                image_provider: str = "brand-card") -> dict | None:
    """Материализовать выбранный план-блок. Нет данных под график — блока не будет."""
    kind = visual["block"]
    reason = (visual.get("reasons") or {}).get(
        str(_field(section, "chart_kind") or "line")) if kind == "chart" else None
    reason = reason or visual["reason"]
    if kind == "chart":
        code = str(_field(section, "material") or "").strip()
        material = numbers.get(code)
        if not material or len(material["values"]) < 2:
            # раздел не назвал материал — ряд не подставляем «первый попавшийся»:
            # график про охваты посреди раздела о рисках хуже, чем его отсутствие
            return None
        return _block("chart", {"kind": _field(section, "chart_kind") or "line",
                                "series": material["values"], "unit": material["unit"],
                                "title": material["title"], "material": code},
                      reason=reason, alt=f"График: {material['title']}",
                      caption=f"{material['title']} ({material['unit']})".strip(),
                      source=material["source"])
    if kind == "stat":
        code = str(_field(section, "material") or "").strip()
        material = numbers.get(code)
        if not material or not material["values"]:
            return None
        last = material["values"][-1]
        value = last.get("value") if isinstance(last, dict) else last
        return _block("stat", {"value": value, "label": material["title"],
                               "unit": material["unit"], "material": code},
                      reason=reason, alt=f"Цифра: {material['title']}",
                      source=material["source"])
    if kind == "table":
        rows = _field(section, "rows") or []
        if not rows:
            return None
        columns = _field(section, "columns") or [f"Колонка {index + 1}"
                                                 for index in range(len(rows[0]))]
        return _block("table", {"columns": columns, "rows": rows},
                      reason=reason, alt=f"Таблица: {section['heading']}",
                      caption=section["heading"],
                      # источника нет — блок всё равно покажем, но проверка это назовёт:
                      # «данные клиента» вместо ссылки на прайс — это выдуманное происхождение
                      source=str(_field(section, "source") or "").strip() or None)
    if kind == "diagram":
        steps = _field(section, "steps") or []
        if len(steps) < 2:
            return None
        return _block("diagram", {"steps": steps, "title": section["heading"]},
                      reason=reason, alt=f"Схема: {section['heading']}", caption=section["heading"])
    if kind == "comparison":
        columns = [column for column in (_field(section, "columns") or [])
                   if isinstance(column, dict) and column.get("title")]
        if len(columns) < 2:
            return None           # строки таблицы — не сравнение: им нужен `rows`
        return _block("comparison", {"columns": columns},
                      reason=reason, alt=f"Сравнение: {section['heading']}",
                      caption=section["heading"],
                      # сравнение вариантов тоже опирается на данные: источник — если он есть
                      source=str(_field(section, "source") or "").strip() or None)
    if kind == "callout":
        text = str(_field(section, "callout") or "").strip()
        if not text:
            return None
        return _block("callout", {"text": text,
                                  "tone": str(_field(section, "tone") or "warning")},
                      reason=reason, alt=f"Врезка: {section['heading']}")
    if kind == "gallery":
        raw_items = _field(section, "gallery") or []
        items: list[dict] = []
        for raw in raw_items[:6]:
            if isinstance(raw, dict):
                title = str(raw.get("title") or raw.get("prompt") or "").strip()
                caption = str(raw.get("caption") or title).strip()
                alt = str(raw.get("alt") or caption).strip()
            else:
                title = str(raw).strip()
                caption, alt = title, title
            if not title:
                continue
            asset = media.build_spec({"title": title[:60],
                                      "eyebrow": f"Posiz · {section['heading'][:32]}"},
                                     "1x1", brand=brand or {})
            items.append({"title": title, "caption": caption, "alt": alt, "asset": asset,
                          "provider": "brand-card"})
        if len(items) < 2:
            return None            # одна картинка — это `image`, а не галерея
        return _block("gallery", {"items": items},
                      reason=reason, alt=f"Галерея: {section['heading']}",
                      caption=section["heading"])

    if kind == "video":
        raw = _field(section, "video")
        src = ""
        items: list[str] = []
        note = ""
        cta = ""
        if isinstance(raw, dict):
            src = str(raw.get("src") or raw.get("url") or "").strip()
            items = [str(item).strip() for item in raw.get("items") or [] if str(item).strip()]
            note = str(raw.get("note") or "").strip()
            cta = str(raw.get("cta") or "").strip()
        elif raw:
            src = str(raw).strip()
        reel = None
        if items or not src:
            # ролик собирается из спеки статьи: заголовок раздела + пункты + подпись
            post = {"title": section["heading"], "eyebrow": "Posiz · разбор",
                    "items": items or [section["heading"]], "note": note or section["heading"],
                    "cta": cta or "Читать целиком"}
            candidate = media_reel.build_reel_spec(post, fps=25, fmt="16x9")
            verdict = media_reel.check(candidate)
            if verdict["ok"]:
                rows = media_reel.timeline(candidate)
                reel = {"spec": candidate, "duration": candidate["duration"],
                        "scenes": len(candidate["scenes"]), "fps": candidate["fps"],
                        "format": candidate["format"],
                        # кадры целиком не вставляем: в статье нужен сториборд-полоса,
                        # а не четыре SVG по 1080×1920 в разметке
                        "storyboard": [{"t": row["start"], "duration": row["duration"],
                                        "template": scene.get("template"),
                                        "label": _scene_label(scene)}
                                       for row, scene in zip(rows, candidate["scenes"])]}
            elif not src:
                return None        # ролик не собрался и ссылки нет — блока не будет
        return _block("video", {"title": section["heading"], "src": src or None,
                                "reel": reel, "provider": "reel" if reel else "external"},
                      reason=reason, alt=f"Видео: {section['heading']}",
                      caption=section["heading"])

    if kind == "image":
        prompt = str(_field(section, "image_prompt") or section["heading"]).strip()
        made = generate_image(prompt, provider=image_provider, size="16x9", brand=brand)
        if not made.get("ok"):
            # провайдер недоступен — рисуем свою графику, но причину не скрываем
            fallback = generate_image(prompt, provider="brand-card", size="16x9", brand=brand)
            return _block("image", {"prompt": prompt, "asset": fallback["asset"],
                                    "provider": "brand-card",
                                    "requested_provider": image_provider,
                                    "fallback_reason": made.get("reason") or made.get("code")},
                          reason=reason, alt=prompt[:120], caption=section["heading"])
        if made.get("asset") is not None:
            return _block("image", {"prompt": prompt, "asset": made["asset"],
                                    "provider": "brand-card"},
                          reason=reason, alt=prompt[:120], caption=section["heading"])
        return _block("image", {"prompt": prompt, "provider": made["provider"],
                                "image": {"url": made["url"], "width": made.get("width"),
                                          "height": made.get("height"),
                                          "provider": made["provider"],
                                          "license": made.get("license") or {},
                                          "cached": bool(made.get("cached")),
                                          "key": made.get("key")}},
                      reason=reason, alt=prompt[:120], caption=section["heading"])
    return None


def _closing_blocks(brief: dict, topic: str, materials: list[dict]) -> list[dict]:
    """Финал: FAQ и призыв. FAQ — из брифа или из типовых вопросов по теме."""
    blocks: list[dict] = []
    faq = brief.get("faq") or []
    if faq:
        blocks.append(_block("heading", {"text": "Частые вопросы", "level": 2}))
        blocks.append(_block("faq", {"items": faq}))
    quote = brief.get("quote")
    if quote:
        blocks.append(_block("quote", {"text": quote.get("text"),
                                       "author": quote.get("author") or "Источник"}))
    if brief.get("summary"):
        blocks.append(_block("heading", {"text": "Коротко", "level": 2}))
        blocks.append(_block("list", {"items": brief["summary"], "ordered": False}))
    blocks.append(_block("cta", {"text": str(brief.get("cta") or "Попробовать в Posiz"),
                                 "url": brief.get("cta_url") or "/signup"}))
    return blocks


def _stats(spec: dict, sources: list[str]) -> dict:
    words = count_words(spec)
    visuals = [block for block in spec["blocks"] if BLOCKS.get(block["type"], {}).get("visual")]
    return {"words": words, "reading_minutes": reading_minutes(words, spec.get("locale")),
            "blocks": len(spec["blocks"]), "visuals": len(visuals),
            "h2": sum(1 for block in spec["blocks"]
                      if block["type"] == "heading" and block.get("props", {}).get("level") == 2),
            "sources": sorted(set(sources)),
            "kinds": sorted({block["type"] for block in spec["blocks"]})}


# ------------------------------------------------------------------ проверки
def check(spec: dict) -> dict:
    """Проверки качества: их видит и автор, и гейт релиза.

    Проверяется то, что чаще всего ломается у «сгенерированных» статей: визуал без alt и
    причины, цифры без источника, скачки заголовков, пустые блоки, вода и дубли.
    """
    issues: list[dict] = []
    if not isinstance(spec, dict):
        return {"ok": False, "issues": [{"code": "spec_version", "message": "нет спеки"}]}
    policy = spec.get("policy") or {}
    if spec.get("spec_version") != SPEC_VERSION:
        return {"ok": False, "issues": [{"code": "spec_version", "message": "нужна article.spec.v1"}]}
    blocks = spec.get("blocks") or []
    if not blocks:
        issues.append({"code": "blocks_empty", "message": "в статье нет блоков"})
    seo = spec.get("seo") or {}
    for field, limit in (("title", 70), ("description", 180)):
        value = str(seo.get(field) or "").strip()
        if not value:
            issues.append({"code": "seo_missing", "field": field,
                           "message": f"нет SEO-поля {field}"})
        elif len(value) > limit:
            issues.append({"code": "seo_too_long", "field": field,
                           "message": f"{field} длиннее {limit} знаков"})
    if not seo.get("keywords"):
        issues.append({"code": "keywords_missing", "message": "нет ключевых фраз"})

    if not any(block["type"] == "heading" for block in blocks):
        issues.append({"code": "headings_missing", "message": "нет ни одного подзаголовка"})
    level = 1
    for index, block in enumerate(blocks):
        kind = block.get("type")
        meta = BLOCKS.get(kind)
        if meta is None:
            issues.append({"code": "block_unknown", "field": f"blocks[{index}].type",
                           "message": f"блок {kind!r} не из реестра"})
            continue
        props = block.get("props") or {}
        for field in meta["needs"]:
            if props.get(field) in (None, "", [], {}):
                issues.append({"code": "prop_required", "field": f"blocks[{index}].{field}",
                               "message": f"{kind}: обязательно поле {field}"})
        if meta["visual"]:
            if not str(block.get("alt") or "").strip():
                issues.append({"code": "alt_missing", "field": f"blocks[{index}].alt",
                               "message": f"{kind}: у визуала нет alt"})
            if not str(block.get("reason") or "").strip():
                issues.append({"code": "reason_missing", "field": f"blocks[{index}].reason",
                               "message": f"{kind}: не объяснено, зачем он в этом месте"})
        if kind == "image":
            image = props.get("image") or {}
            if image:
                license_data = image.get("license") or {}
                if not (license_data.get("name") or license_data.get("type")):
                    issues.append({"code": "license_missing", "field": f"blocks[{index}].license",
                                   "message": "внешняя иллюстрация без лицензии"})
                if not str(image.get("url") or "").strip():
                    issues.append({"code": "image_url_missing", "field": f"blocks[{index}].url",
                                   "message": "у внешней иллюстрации нет адреса"})
                license_name = str(license_data.get("name") or license_data.get("type")
                                   or "").strip()
                if policy.get("require_attribution") and not license_name:
                    # видимая подпись — это имя лицензии, а не название провайдера
                    issues.append({"code": "attribution_missing",
                                   "field": f"blocks[{index}].attribution",
                                   "message": "политика темы требует подпись источника иллюстрации"})
            elif not (props.get("asset") or {}).get("props"):
                issues.append({"code": "image_asset_missing", "field": f"blocks[{index}].asset",
                               "message": "иллюстрация без спеки карточки и без файла"})
        if kind == "gallery":
            for position, item in enumerate(props.get("items") or []):
                if not str(item.get("alt") or "").strip():
                    issues.append({"code": "alt_missing",
                                   "field": f"blocks[{index}].items[{position}].alt",
                                   "message": "gallery: у элемента нет alt"})
        if kind == "video":
            reel = props.get("reel") or {}
            limits = media_reel.limits()
            duration = float(reel.get("duration") or 0)
            if duration and duration > float(limits["max_duration"]):
                issues.append({"code": "video_too_long", "field": f"blocks[{index}].duration",
                               "message": f"ролик {duration} с длиннее лимита "
                                          f"{limits['max_duration']} с"})
            if not reel and not str(props.get("src") or "").strip():
                issues.append({"code": "video_source", "field": f"blocks[{index}].src",
                               "message": "video: нет ни спеки ролика, ни ссылки"})
        if kind in {"chart", "stat", "table"} and not str(block.get("source") or "").strip():
            issues.append({"code": "source_missing", "field": f"blocks[{index}].source",
                           "message": f"{kind}: цифры без источника — это не факт"})
        if kind == "heading":
            new_level = int(props.get("level") or 2)
            if new_level > level + 1:
                issues.append({"code": "heading_jump", "field": f"blocks[{index}].level",
                               "message": "уровни заголовков идут с пропуском (h2 → h4)"})
            level = new_level
        if kind in {"paragraph", "lead"} and len(str(props.get("text") or [])) < 40:
            issues.append({"code": "too_short", "field": f"blocks[{index}].text",
                           "message": "блок короче 40 знаков — это не абзац"})

    text = _plain_text(spec)
    for marker in ("lorem", "TODO", "[вставить", "xxx"):
        if marker.lower() in text.lower():
            issues.append({"code": "placeholder", "message": f"в тексте остался маркер «{marker}»"})
    words = count_words(spec)
    # пороги объёма — политика пака: у темы с высокой ценой ошибки минимум выше
    min_words = int(policy.get("min_words") or MIN_WORDS)
    max_words = int(policy.get("max_words") or MAX_WORDS)
    if words < min_words:
        issues.append({"code": "too_few_words", "message":
                       f"{words} слов при минимуме {min_words}: это заметка, а не статья"})
    if words > max_words:
        issues.append({"code": "too_many_words", "message": f"{words} слов: статью не дочитают"})

    keyword_cov = keyword_coverage(spec)
    if seo.get("keywords") and keyword_cov["in_title"] == 0:
        issues.append({"code": "keyword_not_in_title",
                       "message": "ни одна ключевая фраза не попала в заголовок"})
    kinds = {block.get("type") for block in blocks}
    for required in policy.get("required_blocks") or ():
        if required not in kinds:
            issues.append({"code": "block_required", "field": str(required),
                           "message": f"пакет правил требует блок {required}"})
    text_lower = _plain_text(spec).lower()
    for phrase in policy.get("banned_phrases") or ():
        if str(phrase).strip() and str(phrase).lower() in text_lower:
            issues.append({"code": "banned_phrase", "field": str(phrase),
                           "message": f"фраза «{phrase}» запрещена политикой темы"})
    for kind in policy.get("require_sources") or ():
        for index, block in enumerate(blocks):
            if block.get("type") == kind and not str(block.get("source") or "").strip():
                issues.append({"code": "source_missing", "field": f"blocks[{index}].source",
                               "message": f"{kind}: политика темы требует источник"})
    visuals = [block for block in blocks if BLOCKS.get(block.get("type"), {}).get("visual")]
    if len(blocks) > 8 and not visuals:
        issues.append({"code": "no_visuals",
                       "message": "статья без единого визуала — читается как лонгрид 2010 года"})
    issues.extend(article_links.check_links(spec))
    if spec.get("translation") or spec.get("hreflang"):
        issues.extend(article_i18n.check_i18n(spec))
    for position, entry in enumerate(spec.get("history") or []):
        if not str((entry or {}).get("reason") or "").strip():
            issues.append({"code": "history_reason_missing", "field": f"history[{position}]",
                           "message": "правка без причины: через месяц её никто не объяснит"})
    return {"ok": not issues, "issues": issues,
            "stats": {"words": words, "visuals": len(visuals),
                      "keyword_coverage": keyword_cov}}


def _plain_text(spec: dict) -> str:
    parts: list[str] = []
    for block in spec.get("blocks") or []:
        props = block.get("props") or {}
        for key in ("text", "title", "author"):
            if props.get(key):
                parts.append(str(props[key]))
        for key in ("items", "steps"):
            for item in props.get(key) or []:
                parts.append(str(item if not isinstance(item, dict) else item.get("text") or ""))
        for row in props.get("rows") or []:
            parts.extend(str(cell) for cell in row)
    return " ".join(parts)


def keyword_coverage(spec: dict) -> dict:
    """Куда попали ключевые фразы: заголовок, первый абзац, подзаголовки. Это SEO-минимум."""
    seo = spec.get("seo") or {}
    keywords = [str(word).lower() for word in seo.get("keywords") or []]
    title = str(seo.get("title") or "").lower()
    first = ""
    for block in spec.get("blocks") or []:
        if block["type"] in {"lead", "paragraph"}:
            first = str((block.get("props") or {}).get("text") or "").lower()
            break
    headings = " ".join(str((block.get("props") or {}).get("text") or "").lower()
                        for block in spec.get("blocks") or []
                        if block["type"] == "heading")
    return {"keywords": len(keywords),
            "in_title": sum(1 for word in keywords if word in title),
            "in_first_paragraph": sum(1 for word in keywords if word in first),
            "in_headings": sum(1 for word in keywords if word in headings),
            "title": title}


def shuffles(text: str) -> set[str]:
    """Множество шинглов (5 слов) — основа проверки уникальности."""
    words = re.findall(r"[\w'-]+", str(text or "").lower())
    return {" ".join(words[index:index + SHINGLE])
            for index in range(max(0, len(words) - SHINGLE + 1))}


def uniqueness(spec: dict, corpus: list[str] | None = None) -> dict:
    """Доля общих шинглов с корпусом: 1.0 — текст целиком из архива, 0.0 — новый."""
    mine = shuffles(_plain_text(spec))
    other: set[str] = set()
    for text in corpus or []:
        other |= shuffles(text)
    shared = mine & other
    ratio = round(len(shared) / max(len(mine), 1), 3)
    policy = (spec or {}).get("policy") or {}
    limit = float(policy.get("max_duplicate_ratio") or MAX_DUPLICATE_RATIO)
    return {"shingles": len(mine), "shared": len(shared), "duplicate_ratio": ratio,
            "limit": limit, "ok": ratio <= limit,
            "examples": sorted(shared)[:5]}


# ------------------------------------------------------------------ изображения
#: Откуда берутся визуалы. `brand-card` — своя графика через медиа-движок (работает всегда),
#: остальные — провайдеры: без ключа они честно недоступны, а не «нарисуем чем-нибудь».
#: Каждая внешняя иллюстрация обязана прийти с лицензией: иначе блок не ставится.
IMAGES: dict[str, dict[str, Any]] = {
    "brand-card": {"title": "Брендовая карточка из спеки", "mode": "internal",
                   "needs": [], "key_env": None, "url_env": None},
    "ai-generate": {"title": "AI-иллюстрация (генеративный провайдер)", "mode": "cloud",
                    "needs": ["MEDIA_IMAGE_PROVIDER_URL"],
                    "key_env": "MEDIA_IMAGE_PROVIDER_KEY",
                    "url_env": "MEDIA_IMAGE_PROVIDER_URL"},
    "stock": {"title": "Фотобанк (лицензия обязательна)", "mode": "cloud",
              "needs": ["MEDIA_STOCK_URL"], "key_env": "MEDIA_STOCK_KEY",
              "url_env": "MEDIA_STOCK_URL"},
}

#: Кэш иллюстраций: одинаковый запрос не оплачивается дважды, а лицензия хранится рядом с файлом.
_images = Collection("article_images")


def _env(name: str | None) -> str:
    import os

    return os.getenv(name or "", "").strip()


def image_state(code: str) -> dict:
    """Доступность провайдера: чего именно не хватает — в ``needs``, а не «недоступно»."""
    row = IMAGES.get(code)
    if not row:
        return {"code": code, "available": False, "needs": [], "reason": "провайдер не из реестра"}
    missing = [name for name in (row.get("needs") or []) if not _env(name)]
    if row.get("key_env") and not _env(row["key_env"]):
        missing.insert(0, str(row["key_env"]))
    available = not missing
    result = {"code": code, "title": row.get("title"), "mode": row.get("mode"),
              "available": available, "needs": missing}
    if not available:
        result["reason"] = f"нужны переменные: {', '.join(missing)}"
    return result


def _provider_request(provider: str, prompt: str, size: str) -> dict:
    """Запрос к внешнему провайдеру: URL, размеры и **лицензия**. Отдельная функция, чтобы
    тесты подменяли сеть, не трогая остальной конвейер."""
    import httpx

    row = IMAGES[provider]
    url = _env(row.get("url_env"))
    if not url:
        return {"ok": False, "code": "provider_url_missing",
                "reason": f"нужна переменная {row.get('url_env')}"}
    payload = {"prompt": prompt, "size": size}
    headers = {"Authorization": f"Bearer {_env(row.get('key_env'))}"} if row.get("key_env") else {}
    try:
        response = httpx.post(url, json=payload, headers=headers, timeout=30.0)
        response.raise_for_status()
        body = response.json()
    except Exception as exc:                       # noqa: BLE001 — сеть не должна ронять статью
        return {"ok": False, "code": "provider_failed", "reason": f"{exc.__class__.__name__}: {exc}"}
    if not isinstance(body, dict) or not body.get("url"):
        return {"ok": False, "code": "provider_response_invalid",
                "reason": "провайдер не вернул url иллюстрации"}
    license_data = body.get("license") or {}
    if not license_data.get("name") and not license_data.get("type"):
        return {"ok": False, "code": "license_missing",
                "reason": "провайдер не вернул лицензию: такую иллюстрацию публиковать нельзя"}
    return {"ok": True, "url": str(body["url"]),
            "width": int(body.get("width") or 0), "height": int(body.get("height") or 0),
            "license": {"name": str(license_data.get("name") or ""),
                        "type": str(license_data.get("type") or ""),
                        "terms_url": str(license_data.get("terms_url") or ""),
                        "author": str(license_data.get("author") or "")},
            "cost_rub": float(body.get("cost_rub") or 0)}


def image_key(provider: str, prompt: str, size: str) -> str:
    payload = json.dumps([provider, prompt.strip(), size], ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def generate_image(prompt: str, *, provider: str = "brand-card", size: str = "16x9",
                   brand: dict | None = None, license_hint: str | None = None) -> dict:
    """Иллюстрация для статьи: своя графика, AI-провайдер или фотобанк — с лицензией и кэшем.

    Три отказа честные и названные: неизвестный провайдер, отсутствие ключа/адреса и ответ без
    лицензии. Четвёртый — кэш: тот же запрос не оплачивается и не генерируется второй раз.
    """
    code = str(provider or "brand-card").strip() or "brand-card"
    if code not in IMAGES:
        return {"ok": False, "code": "provider_unknown", "provider": code,
                "known": sorted(IMAGES)}
    prompt_text = str(prompt or "").strip()
    if not prompt_text:
        return {"ok": False, "code": "prompt_required", "provider": code}
    if code == "brand-card":
        asset = media.build_spec({"title": prompt_text[:60], "eyebrow": "Posiz · статья"},
                                 "16x9" if size == "16x9" else "1x1", brand=brand or {})
        return {"ok": True, "provider": code, "prompt": prompt_text, "size": size,
                "asset": asset, "cached": False,
                "license": {"type": "own", "name": "Графика платформы",
                            "note": "своя вёрстка: сторонних прав нет"}}

    state = image_state(code)
    if not state["available"]:
        return {"ok": False, "code": "provider_key_missing", "provider": code,
                "needs": state["needs"], "reason": state["reason"]}
    key = image_key(code, prompt_text, size)
    cached = _images.get(f"img_{key}")
    if cached:
        return {"ok": True, "provider": code, "prompt": prompt_text, "size": size,
                "url": cached["url"], "width": cached.get("width"),
                "height": cached.get("height"), "license": cached["license"],
                "cached": True, "key": key}
    answer = _provider_request(code, prompt_text, size)
    if not answer.get("ok"):
        return {**answer, "provider": code, "prompt": prompt_text}
    # лицензию проверяем здесь, а не «как договорились с провайдером»: ответ без прав
    # не становится иллюстрацией ни при какой реализации запроса
    answer_license = answer.get("license") or {}
    if not (answer_license.get("name") or answer_license.get("type")):
        return {"ok": False, "code": "license_missing", "provider": code,
                "prompt": prompt_text,
                "reason": "провайдер не вернул лицензию: такую иллюстрацию публиковать нельзя"}
    entry = {"id": f"img_{key}", "key": key, "provider": code, "prompt": prompt_text,
             "size": size, "url": answer["url"], "width": answer.get("width"),
             "height": answer.get("height"), "license": answer["license"],
             "cost_rub": answer.get("cost_rub", 0.0),
             "license_hint": str(license_hint or "").strip() or None,
             "retrieved_at": _now()}
    _images.put(entry)
    return {"ok": True, "provider": code, "prompt": prompt_text, "size": size,
            "url": entry["url"], "width": entry["width"], "height": entry["height"],
            "license": entry["license"], "cached": False, "key": key}


def image_cache() -> list[dict]:
    """Что лежит в кэше: провайдер, лицензия и когда получено — без самих файлов."""
    rows = sorted(_images.all(), key=lambda item: str(item.get("retrieved_at") or ""), reverse=True)
    return [{"id": row["id"], "provider": row.get("provider"), "prompt": row.get("prompt"),
             "license": row.get("license"), "retrieved_at": row.get("retrieved_at"),
             "cost_rub": row.get("cost_rub")} for row in rows]


def image_cache_stats() -> dict:
    rows = _images.all()
    by_provider: dict[str, int] = {}
    cost = 0.0
    for row in rows:
        provider = str(row.get("provider") or "")
        by_provider[provider] = by_provider.get(provider, 0) + 1
        cost += float(row.get("cost_rub") or 0)
    return {"entries": len(rows), "providers": by_provider, "cost_rub": round(cost, 2)}


def attribution(block: dict) -> str:
    """Видимая подпись под иллюстрацией: провайдер и лицензия. Не то же, что `source` цифры."""
    image = (block.get("props") or {}).get("image") or {}
    license_data = image.get("license") or {}
    name = str(license_data.get("name") or license_data.get("type") or "").strip()
    provider = str(image.get("provider") or block.get("props", {}).get("provider") or "")
    parts = [part for part in (name, provider and f"провайдер: {provider}") if part]
    return " · ".join(parts)


def reset_images() -> None:
    _images.clear_memory()


def image_catalog() -> list[dict]:
    """Витрина провайдеров иллюстраций: доступность считается из окружения, а не объявлена."""
    return [image_state(code) for code in sorted(IMAGES)]


#: Откуда берётся видео в статье. `reel` — ролик из спеки статьи (медиа-движок, работает всегда),
#: `external` — ссылка клиента на готовый файл: своего файла мы не выдумываем.
VIDEOS: dict[str, dict[str, Any]] = {
    "reel": {"title": "Ролик из спеки статьи", "mode": "internal", "needs": [],
             "available": True},
    "external": {"title": "Ссылка на готовый ролик клиента", "mode": "client",
                 "needs": ["video.src"], "available": True},
}


def video_catalog() -> list[dict]:
    return [{"code": code, **row} for code, row in sorted(VIDEOS.items())]


# ------------------------------------------------------------------ HTML
#: CSS собирается из токенов шаблона: шаблон — это данные (цвета, гарнитура, ширина, радиусы),
#: а не второй рендерер. Поэтому новый шаблон не требует правки кода.
_STYLE_TEMPLATE = """
:root{--fg:%(fg)s;--dim:%(dim)s;--line:%(line)s;--bg:%(bg)s;--accent:%(accent)s}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:17px/1.65 %(font)s}
article{max-width:%(max_width)s;margin:0 auto;padding:32px 20px 72px}
h1{font-size:%(h1)s;line-height:1.2;margin:0 0 12px}
h2{font-size:24px;line-height:1.3;margin:40px 0 12px}
p{margin:0 0 16px}
.lead{font-size:20px;color:var(--dim);margin-bottom:24px}
figure{margin:24px 0;padding:0}
figure img,figure svg{width:100%%;height:auto;border-radius:%(radius)s;border:1px solid var(--line)}
figcaption{color:var(--dim);font-size:14px;margin-top:8px}
table{width:100%%;border-collapse:collapse;margin:8px 0;font-size:15px}
th,td{border-bottom:1px solid var(--line);padding:10px 8px;text-align:left}
th{background:%(table_head)s}
blockquote{margin:24px 0;padding:16px 20px;border-left:3px solid var(--accent);background:%(callout_bg)s}
blockquote p{margin:0 0 6px;font-size:19px}
cite{color:var(--dim);font-size:14px;font-style:normal}
.callout{margin:24px 0;padding:16px 18px;border-radius:%(radius)s;background:%(callout_bg)s;border:1px solid %(callout_line)s}
.callout.warning{background:#fff7ed;border-color:#ffd9a8}
.stat{margin:24px 0;padding:20px;border-radius:%(radius)s;background:%(stat_bg)s;color:%(stat_fg)s}
.stat b{display:block;font-size:40px;line-height:1.1}
.stat span{color:%(stat_note)s;font-size:14px}
.gallery{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:14px}
.gallery figure{margin:0}
.gallery figcaption{margin-top:6px}
.video{position:relative}
.video-strip{margin:0}
.videobar{display:flex;justify-content:space-between;color:var(--dim);font-size:13px;margin-top:8px}
.video video{width:100%%;border-radius:%(radius)s;border:1px solid var(--line);background:#000}
.cmp{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin:16px 0}
.cmp div{border:1px solid var(--line);border-radius:%(radius)s;padding:14px}
.cmp h3{margin:0 0 8px;font-size:16px}
.cmp ul{margin:0;padding-left:18px;font-size:15px}
.faq{margin:16px 0}
.faq details{border-bottom:1px solid var(--line);padding:10px 0}
.faq summary{font-weight:600;cursor:pointer}
.cta{margin:32px 0 0;padding:20px;border-radius:%(radius)s;background:var(--accent);color:#fff}
.cta a{color:#fff;font-weight:600}
.meta{color:var(--dim);font-size:14px;margin-bottom:20px}
ul,ol{margin:0 0 16px;padding-left:22px}
li{margin:6px 0}
"""


#: акцент по умолчанию: у графиков и схем он берётся из токенов шаблона статьи
_ACCENT = TEMPLATES[DEFAULT_TEMPLATE]["tokens"]["accent"]


def style_for(code: str | None = None) -> str:
    """CSS для шаблона: токены подставляются в один каркас, поэтому шаблоны не расходятся."""
    return _STYLE_TEMPLATE % template_tokens(code)


def _esc(text: Any) -> str:
    from xml.sax.saxutils import escape
    return escape(str("" if text is None else text))


def _chart_svg(block: dict, accent: str = _ACCENT) -> str:
    """График из ряда материала: столбцы или линия, подписи по осям, без библиотек."""
    props = block.get("props") or {}
    values = [row for row in props.get("series") or []]
    points: list[tuple[str, float]] = []
    for row in values:
        if isinstance(row, dict):
            points.append((str(row.get("label") or ""), float(row.get("value") or 0)))
        else:
            points.append(("", float(row or 0)))
    if not points:
        return ""
    width, height, pad = 640, 260, 36
    top = max(value for _, value in points) or 1.0
    unit = str(props.get("unit") or "")
    kind = str(props.get("kind") or "line")
    inner_w, inner_h = width - pad * 2, height - pad * 2
    step = inner_w / max(len(points) - 1, 1)
    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" '
             f'aria-label="{_esc(block.get("alt"))}">',
             f'<rect width="{width}" height="{height}" fill="#0f1117" rx="12"/>']
    # сетка
    for line in range(4):
        y = pad + inner_h * line / 3
        parts.append(f'<line x1="{pad}" y1="{y:.1f}" x2="{width - pad}" y2="{y:.1f}" '
                     f'stroke="#232734" stroke-width="1"/>')
        value = top * (3 - line) / 3
        parts.append(f'<text x="8" y="{y + 4:.1f}" fill="#6b7280" font-size="11" '
                     f'font-family="DejaVu Sans">{value:.0f}{_esc(unit)}</text>')
    if kind == "bar":
        bar_w = inner_w / max(len(points) * 1.6, 1)
        for index, (label, value) in enumerate(points):
            x = pad + index * (inner_w / len(points)) + bar_w * 0.3
            bar_h = inner_h * value / top
            parts.append(f'<rect x="{x:.1f}" y="{pad + inner_h - bar_h:.1f}" width="{bar_w:.1f}" '
                         f'height="{bar_h:.1f}" fill="{accent}" rx="4"/>')
            parts.append(f'<text x="{x + bar_w / 2:.1f}" y="{height - 10}" fill="#9ca3af" '
                         f'font-size="11" text-anchor="middle" font-family="DejaVu Sans">'
                         f'{_esc(label)}</text>')
    else:
        coords = []
        for index, (label, value) in enumerate(points):
            coords.append((pad + index * step, pad + inner_h - inner_h * value / top))
        path = " ".join(f'{"M" if index == 0 else "L"}{x:.1f},{y:.1f}'
                        for index, (x, y) in enumerate(coords))
        parts.append(f'<path d="{path}" fill="none" stroke="{accent}" stroke-width="3"/>')
        for index, (x, y) in enumerate(coords):
            parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="{accent}"/>')
            parts.append(f'<text x="{x:.1f}" y="{height - 10}" fill="#9ca3af" font-size="11" '
                         f'text-anchor="middle" font-family="DejaVu Sans">'
                         f'{_esc(points[index][0])}</text>')
    parts.append(f'<text x="{pad}" y="22" fill="#e5e7eb" font-size="14" '
                 f'font-family="DejaVu Sans">{_esc(props.get("title") or "")}</text>')
    parts.append("</svg>")
    return "".join(parts)


def _diagram_svg(block: dict, accent: str = _ACCENT) -> str:
    """Схема процесса: шаги в ряд со стрелками. Длинные подписи переносятся по словам."""
    steps = [str(step) for step in (block.get("props") or {}).get("steps") or []]
    if not steps:
        return ""
    width = 700
    step_w = min(200, width // max(len(steps), 1) - 16)
    height = 180
    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" '
             f'aria-label="{_esc(block.get("alt"))}">',
             f'<rect width="{width}" height="{height}" fill="#0f1117" rx="12"/>']
    for index, step in enumerate(steps):
        x = 16 + index * (step_w + 16)
        parts.append(f'<rect x="{x}" y="52" width="{step_w}" height="72" rx="10" '
                     f'fill="#181b24" stroke="#2a2f3d"/>')
        words, line, lines = step.split(), "", []
        for word in words:
            if len(line) + len(word) > 22 and line:
                lines.append(line)
                line = word
            else:
                line = f"{line} {word}".strip()
        if line:
            lines.append(line)
        for row, text in enumerate(lines[:3]):
            parts.append(f'<text x="{x + step_w / 2}" y="{86 + row * 16}" fill="#e5e7eb" '
                         f'font-size="12" text-anchor="middle" font-family="DejaVu Sans">'
                         f'{_esc(text)}</text>')
        parts.append(f'<text x="{x + 10}" y="42" fill="{accent}" font-size="12" '
                     f'font-family="DejaVu Sans">Шаг {index + 1}</text>')
        if index < len(steps) - 1:
            parts.append(f'<path d="M{x + step_w + 2} 88 L{x + step_w + 14} 88" stroke="{accent}" '
                         f'stroke-width="2"/>')
    parts.append("</svg>")
    return "".join(parts)


def _video_strip(reel: dict, accent: str = _ACCENT) -> str:
    """Сториборд-полоса ролика: сцены, время и подписи. Кадры 1080×1920 не вставляем."""
    shots = reel.get("storyboard") or []
    width, height, pad = 700, 150, 12
    total = float(reel.get("duration") or 1) or 1
    inner = width - pad * 2
    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" '
             f'aria-label="Сториборд ролика: {len(shots)} сцен" class="video-strip">',
             f'<rect width="{width}" height="{height}" fill="#0f1117" rx="12"/>']
    for index, shot in enumerate(shots):
        share = float(shot.get("duration") or 0) / total
        x = pad + inner * (float(shot.get("t") or 0) / total)
        box_w = max(inner * share - 6, 40)
        parts.append(f'<rect x="{x:.1f}" y="{pad + 26}" width="{box_w:.1f}" height="62" '
                     f'rx="8" fill="#181b24" stroke="#2a2f3d"/>')
        parts.append(f'<text x="{x + 8:.1f}" y="{pad + 18}" fill="{accent}" font-size="11" '
                     f'font-family="DejaVu Sans">'
                     f'{index + 1} · {float(shot.get("t") or 0):.1f} с</text>')
        label = str(shot.get("label") or shot.get("template") or "")
        limit = max(int(box_w // 7), 6)
        parts.append(f'<text x="{x + 8:.1f}" y="{pad + 52}" fill="#e5e7eb" font-size="11" '
                     f'font-family="DejaVu Sans">{_esc(label[:limit])}</text>')
        parts.append(f'<text x="{x + 8:.1f}" y="{pad + 74}" fill="#6b7280" font-size="10" '
                     f'font-family="DejaVu Sans">'
                     f'{_esc(str(shot.get("template") or ""))}</text>')
    parts.append(f'<text x="{pad}" y="{height - 12}" fill="#6b7280" font-size="11" '
                 f'font-family="DejaVu Sans">'
                 f'{_esc(reel.get("format"))} · {float(total):.1f} с · '
                 f'{_esc(len(shots))} сцен</text>')
    parts.append("</svg>")
    return "".join(parts)


def labels_for(spec: dict) -> dict:
    """Подписи интерфейса статьи: переводятся кодом, а не переводчиком."""
    return dict(spec.get("labels") or article_i18n.UI_STRINGS["ru"])


def _source_note(block: dict, labels: dict) -> str:
    """« · источник: прайс клиента» — с подписью на языке статьи."""
    source = block.get("source")
    if not source:
        return ""
    return f" · {_esc(labels['source'])}: {_esc(source)}"


def _render_block(block: dict, accent: str = _ACCENT,
                  labels: dict | None = None) -> str:
    labels = labels or article_i18n.UI_STRINGS["ru"]
    kind, props = block["type"], block.get("props") or {}
    if kind == "lead":
        return f'<p class="lead">{_esc(props["text"])}</p>'
    if kind == "heading":
        level = int(props.get("level") or 2)
        return f'<h{level}>{_esc(props["text"])}</h{level}>'
    if kind == "paragraph":
        return f"<p>{_esc(props['text'])}</p>"
    if kind == "list":
        tag = "ol" if props.get("ordered") else "ul"
        items = "".join(f"<li>{_esc(item)}</li>" for item in props.get("items") or [])
        return f"<{tag}>{items}</{tag}>"
    if kind == "related":
        rows = "".join(
            f'<li><a href="{_esc(item.get("url"))}">{_esc(item.get("anchor") or item.get("title"))}'
            f'</a></li>' for item in props.get("items") or [])
        return (f'<nav class="related"><h2>{_esc(labels["related"])}</h2><ul>{rows}</ul>'
                f'<p>{_esc(block.get("reason"))}</p></nav>')
    if kind == "quote":
        return (f'<blockquote><p>{_esc(props["text"])}</p>'
                f'<cite>{_esc(props.get("author"))}</cite></blockquote>')
    if kind == "table":
        head = "".join(f"<th>{_esc(cell)}</th>" for cell in props.get("columns") or [])
        body = "".join("<tr>" + "".join(f"<td>{_esc(cell)}</td>" for cell in row) + "</tr>"
                       for row in props.get("rows") or [])
        return (f"<figure><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
                f'<figcaption>{_esc(block.get("caption"))}'
                f"{_source_note(block, labels)}</figcaption></figure>")
    if kind == "chart":
        return (f'<figure>{_chart_svg(block, accent)}<figcaption>{_esc(block.get("caption"))}'
                f"{_source_note(block, labels)}</figcaption></figure>")
    if kind == "diagram":
        return (f'<figure>{_diagram_svg(block, accent)}'
                f'<figcaption>{_esc(block.get("caption"))}</figcaption></figure>')
    if kind == "stat":
        return (f'<div class="stat"><b>{_esc(props.get("value"))}{_esc(props.get("unit"))}</b>'
                f'<span>{_esc(props.get("label"))} · {_esc(labels["source"])}: '
                f'{_esc(block.get("source"))}</span>'
                f"</div>")
    if kind == "comparison":
        columns = "".join(
            f"<div><h3>{_esc(column.get('title'))}</h3><ul>"
            + "".join(f"<li>{_esc(item)}</li>" for item in column.get("items") or [])
            + "</ul></div>" for column in props.get("columns") or [])
        return (f'<figure><div class="cmp">{columns}</div>'
                f'<figcaption>{_esc(block.get("caption"))}</figcaption></figure>')
    if kind == "image":
        image = props.get("image") or {}
        if image.get("url"):
            size = ""
            if image.get("width") and image.get("height"):
                size = f' width="{int(image["width"])}" height="{int(image["height"])}"'
            note = attribution(block)
            caption = _esc(block.get("caption") or block.get("alt"))
            return (f'<figure><img src="{_esc(image["url"])}" alt="{_esc(block.get("alt"))}"'
                    f'{size} loading="lazy">'
                    f'<figcaption>{caption}'
                    f'{" · " + _esc(note) if note else ""}</figcaption></figure>')
        asset = props.get("asset") or {}
        asset_spec = asset.get("spec") or asset     # спека карточки: обёртка или она сама
        # иллюстрация — SVG медиа-движка прямо в разметке: страница не тянет внешние файлы
        svg = ""
        if asset_spec.get("props"):
            try:
                svg = media.render_svg(asset_spec)
            except Exception:                     # битая карточка не должна ломать статью
                svg = ""
        caption = _esc(block.get("caption") or block.get("alt"))
        if svg:
            return f"<figure>{svg}<figcaption>{caption}</figcaption></figure>"
        title = _esc(props.get("prompt"))
        return (f'<figure><div style="padding:28px;border-radius:12px;background:#0f1117;'
                f'color:#fff;font-size:22px">{title}</div>'
                f"<figcaption>{caption}</figcaption></figure>")
    if kind == "gallery":
        tiles = []
        for item in props.get("items") or []:
            asset_spec = (item.get("asset") or {}).get("spec") or item.get("asset") or {}
            svg = ""
            if asset_spec.get("props"):
                try:
                    svg = media.render_svg(asset_spec)
                except Exception:                 # битая карточка не ломает статью
                    svg = ""
            if not svg:
                svg = (f'<div style="padding:24px;border-radius:{12}px;background:#0f1117;'
                       f'color:#fff">{_esc(item.get("title"))}</div>')
            tiles.append(f'<figure>{svg}<figcaption>{_esc(item.get("caption"))}</figcaption>'
                         f"</figure>")
        return (f'<figure><div class="gallery">{"".join(tiles)}</div>'
                f'<figcaption>{_esc(block.get("caption") or block.get("alt"))}</figcaption>'
                f"</figure>")
    if kind == "video":
        reel = props.get("reel") or {}
        parts = []
        if reel.get("storyboard"):
            parts.append(_video_strip(reel, accent))
        if props.get("src"):
            parts.append(f'<video controls preload="metadata" src="{_esc(props["src"])}">'
                         f'<track kind="captions" src="{_esc(props["src"])}.vtt" '
                         f'label="субтитры"></video>')
        if not parts:
            parts.append(f'<div class="callout">Видео: {_esc(props.get("title"))}</div>')
        meta = ""
        if reel:
            meta = (f'<div class="videobar"><span>{_esc(reel.get("scenes"))} сцен · '
                    f'{_esc(reel.get("duration"))} с · {_esc(reel.get("format"))} · '
                    f'{_esc(reel.get("fps"))} fps</span>'
                    f'<span>ролик из спеки статьи</span></div>')
        return (f'<figure><div class="video">{"".join(parts)}{meta}</div>'
                f'<figcaption>{_esc(block.get("caption") or block.get("alt"))}</figcaption>'
                f"</figure>")
    if kind == "callout":
        tone = " warning" if props.get("tone") == "warning" else ""
        return f'<div class="callout{tone}">{_esc(props["text"])}</div>'
    if kind == "faq":
        items = "".join(
            f"<details><summary>{_esc(item.get('q'))}</summary>"
            f"<p>{_esc(item.get('a'))}</p></details>" for item in props.get("items") or [])
        return f'<div class="faq">{items}</div>'
    if kind == "video":
        return (f'<figure><div style="padding:48px;border-radius:12px;background:#0f1117;'
                f'color:#b9c0cf;text-align:center">Видео: {_esc(props.get("source"))}</div>'
                f'<figcaption>{_esc(block.get("caption"))}</figcaption></figure>')
    if kind == "cta":
        return (f'<div class="cta">{_esc(props.get("text"))} — '
                f'<a href="{_esc(props.get("url"))}">перейти</a></div>')
    return ""


def _jsonld(spec: dict) -> str:
    seo = spec.get("seo") or {}
    graph: list[dict] = [{
        "@type": seo.get("schema") or "Article",
        "headline": seo.get("title"),
        "description": seo.get("description"),
        "inLanguage": spec.get("locale"),
        "datePublished": spec.get("created_at"),
        "author": {"@type": "Organization", "name": (spec.get("author") or {}).get("name")},
        "image": [],
        "keywords": ", ".join(seo.get("keywords") or []),
    }]
    faq = next((block for block in spec.get("blocks") or [] if block["type"] == "faq"), None)
    if faq:
        graph.append({"@type": "FAQPage", "mainEntity": [
            {"@type": "Question", "name": item.get("q"),
             "acceptedAnswer": {"@type": "Answer", "text": item.get("a")}}
            for item in (faq.get("props") or {}).get("items") or []]})
    payload = {"@context": "https://schema.org", "@graph": graph}
    return ('<script type="application/ld+json">'
            + json.dumps(payload, ensure_ascii=False) + "</script>")


def render_html(spec: dict, *, standalone: bool = True) -> str:
    """Спека → HTML публикации. Вёрстка здесь, а не в модели: правка дизайна не требует
    перегенерации статей, а каждая статья получает одинаково корректную разметку."""
    seo, stats = spec.get("seo") or {}, spec.get("stats") or {}
    accent = template_tokens(spec.get("template"))["accent"]
    labels = labels_for(spec)
    body = "\n".join(_render_block(block, accent, labels) for block in spec.get("blocks") or [])
    reading = spec.get("reading") or {}
    minutes = reading.get("minutes", stats.get("reading_minutes"))
    meta = (f'<div class="meta">{_esc(minutes)} {_esc(labels["reading_time"])} · '
            f'{_esc(reading.get("words", stats.get("words")))} {_esc(labels["words"])} · '
            f'{_esc(stats.get("visuals"))} {_esc(labels["visuals"])}</div>')
    links = ""
    if spec.get("internal_links"):
        links = ("<h2>Читать дальше</h2><ul>"
                 + "".join(f'<li><a href="/blog/{_esc(slugify(str(link)))}/">'
                           f'{_esc(str(link).replace("-", " "))}</a></li>'
                           for link in spec["internal_links"]) + "</ul>")
    article = (f'<article><h1>{_esc(seo.get("title"))}</h1>{meta}{body}{links}</article>')
    if not standalone:
        return article
    return ("<!doctype html><html lang=\"" + _esc(spec.get("locale") or "ru") + "\"><head>"
            "<meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f'<title>{_esc(seo.get("title"))}</title>'
            f'<meta name="description" content="{_esc(seo.get("description"))}">'
            f'<meta name="keywords" content="{_esc(", ".join(seo.get("keywords") or []))}">'
            + (f'<link rel="canonical" href="{_esc(seo["canonical"])}">'
               if seo.get("canonical") else "")
            + "".join(f'<link rel="alternate" hreflang="{_esc(row.get("hreflang"))}" '
                      f'href="{_esc(row.get("href"))}">'
                      for row in spec.get("hreflang") or [])
            + f"<style>{style_for(spec.get('template'))}</style>{_jsonld(spec)}"
              f"</head><body>{article}</body></html>")


# ------------------------------------------------------------------ хранилище
def spec_hash(spec: dict) -> str:
    payload = json.dumps({"renderer": "articles@1", "spec": spec}, ensure_ascii=False,
                         sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def publish(spec: dict, *, workspace_id: str | None = None) -> dict:
    """Положить статью в хранилище. Одинаковая спека — одна статья, не второй черновик."""
    key = spec_hash(spec)
    existing = next((row for row in _articles.all() if row.get("spec_hash") == key), None)
    if existing:
        return {"ok": True, "reused": True, "article": existing}
    doc = {"id": spec.get("id") or f"art_{key[:12]}", "spec_hash": key,
           "workspace_id": workspace_id or spec.get("workspace_id") or "ws_de",
           "slug": spec.get("slug"), "title": (spec.get("seo") or {}).get("title"),
           "created_at": spec.get("created_at") or _now(), "spec": spec,
           "status": "draft" if not spec["checks"]["ok"] else "ready",
           "issues": spec["checks"]["issues"]}
    _articles.put(doc)
    article_links.index_entry(doc)
    return {"ok": True, "reused": False, "article": doc}


def replace(article_id: str, spec: dict, *, reason: str | None = None) -> dict | None:
    """Новая версия статьи на том же месте: перелинковка и обновление не плодят дубли."""
    existing = _articles.get(article_id)
    if not existing:
        return None
    doc = {**existing, "spec": spec, "spec_hash": spec_hash(spec), "updated_at": _now(),
           "version": int(existing.get("version") or 1) + 1,
           "last_reason": str(reason or "").strip() or None,
           "status": "draft" if not spec["checks"]["ok"] else "ready",
           "issues": spec["checks"]["issues"]}
    _articles.put(doc)
    article_links.index_entry(doc)
    return doc


def article(article_id: str) -> dict | None:
    return _articles.get(article_id)


def articles(limit: int = 50) -> list[dict]:
    rows = sorted(_articles.all(), key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return rows[:limit]


def summary() -> dict:
    rows = _articles.all()
    return {"articles": len(rows), "ready": sum(1 for row in rows if row.get("status") == "ready"),
            "blocks": sorted(BLOCKS), "images": image_catalog(), "videos": video_catalog(),
            "limits": {"min_words": MIN_WORDS, "max_words": MAX_WORDS,
                       "max_duplicate_ratio": MAX_DUPLICATE_RATIO},
            "visual_rules": [rule["code"] for rule in VISUAL_RULES],
            "rule_packs": [{key: row[key] for key in ("code", "title", "rules")}
                           for row in rule_packs()],
            "default_pack": DEFAULT_PACK,
            "templates": [{"code": row["code"], "title": row["title"]} for row in templates()],
            "default_template": DEFAULT_TEMPLATE,
            "links": {"min_score": article_links._threshold(None, "ARTICLE_LINK_MIN_SCORE",
                                                            article_links.DEFAULT_MIN_SCORE),
                      "max_links": article_links._limit(None, "ARTICLE_MAX_LINKS",
                                                        article_links.DEFAULT_MAX_LINKS),
                      "indexed": len(article_links.index_rows())},
            "review_days": article_links._limit(None, "ARTICLE_REVIEW_DAYS",
                                                article_links.DEFAULT_REVIEW_DAYS)}


def reset() -> None:
    _articles.clear_memory()
