"""Карточки и обложки из спеки (этап A медиа-конвейера, ADR 0023).

Что делает модуль: превращает **пост** (или готовую спеку `reel.spec.v1`) в карточки
1:1, 9:16 и 16:9 — детерминированно, без браузера и без внешних сервисов.

Три принципа, ради которых это написано именно так:

1. **Модель не пишет вёрстку.** Спека — данные, валидируемые здесь. Шаблон выбирается из
   закрытого набора, координаты не приходят извне: их считает рендерер по формату.
2. **Проверки — до рендера и в коде.** Обрезка текста, попадание в интерфейс канала
   (safe zone), контраст пары «текст/фон», обязательная маркировка рекламы. Всё это
   считается здесь арифметикой, а не «смотрит модель»: bbox замечает обрезанный
   заголовок всегда, LLM-судья — не всегда.
3. **Идемпотентность.** Одинаковая спека + одинаковая версия рендерера = тот же
   артефакт. Хэш считается по канонизированному JSON, поэтому порядок ключей не важен.

Формат SVG выбран на этап A осознанно: он текстовый (диффится в тестах), детерминированный
и открывается в браузере, VK и Telegram принимают превью. Растр появится на этапе B вместе
с кадровым движком (Chromium + FFmpeg) — там же, где видео.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from html import escape
from typing import Any, Iterable

from . import media_compliance
from . import media_raster as raster
from . import media_storage as storage
from .docs import Collection, DEMO_WORKSPACE

SPEC_VERSION = "reel.spec.v1"
#: версия рендерера входит в ключ идемпотентности: правка шаблона = новый артефакт
RENDERER = "inhouse-svg@1"

#: форматы и их размеры в пикселях
FORMATS: dict[str, tuple[int, int]] = {
    "1x1": (1080, 1080),
    "9x16": (1080, 1920),
    "16x9": (1920, 1080),
    "4x5": (1080, 1350),
}

#: безопасные зоны: сколько пикселей по краям занимает интерфейс канала.
#: Для 9:16 — по замерам клипов/Shorts (см. 20-DESIGN/video/README.md).
SAFE_ZONES: dict[str, dict[str, int]] = {
    "1x1": {"top": 96, "bottom": 96, "left": 96, "right": 96},
    "9x16": {"top": 220, "bottom": 420, "left": 80, "right": 140},
    "16x9": {"top": 90, "bottom": 90, "left": 120, "right": 120},
    # 4:5 — лента Instagram/Pinterest: снизу место под подпись и кнопки
    "4x5": {"top": 96, "bottom": 150, "left": 96, "right": 96},
}

#: лимиты текста: титры и хедлайны читаются на телефоне только до этих длин
MAX_HEADLINE_CHARS = 90
MAX_LINE_CHARS = 42
MAX_ITEMS = 5
MAX_ITEM_CHARS = 48
MIN_CONTRAST = 4.5

TEXT_COLOR = "#ededf0"
TEXT_MUTED = "#a1a1ab"

#: наборы шрифтов: клиент выбирает гарнитуру, а не получает одну на всех.
#: Только системные стеки — веб-шрифты в растеризаторе требуют загрузки файлов.
FONTS: dict[str, str] = {
    "sans": "-apple-system, Segoe UI, Roboto, Arial, sans-serif",
    "serif": "Georgia, Times New Roman, serif",
    "mono": "SFMono-Regular, Consolas, Liberation Mono, monospace",
}
#: темы: светлая нужна магазинам и клиникам чаще, чем «тёмный космос» по умолчанию
THEMES: dict[str, dict[str, str]] = {
    # scrim — цвет подложек и сетки (на тёмном это белый, на светлом — чёрный),
    # plate — цвет маркировки: на светлом фоне янтарный нечитаем, нужен тёмный вариант
    "dark": {"bg": "#09090b", "text": TEXT_COLOR, "muted": TEXT_MUTED,
             "scrim": "#ffffff", "plate": "#f5b544"},
    "light": {"bg": "#f6f6f7", "text": "#14141a", "muted": "#5b5b66",
              "scrim": "#000000", "plate": "#9a5b00"},
}
DEFAULT_THEME = "dark"
DEFAULT_FONT = "sans"
#: множитель типографики бренда: «крупнее/мельче» — поле,а не новый шаблон
MIN_BRAND_SCALE, MAX_BRAND_SCALE = 0.8, 1.3

#: закрытый набор шаблонов: модель выбирает из него, а не придумывает вёрстку
TEMPLATES: dict[str, dict[str, Any]] = {
    "card-hero": {
        "title": "Карточка-герой",
        "props": ["headline"],
        "optional": ["eyebrow", "note", "footer"],
        "best": ["1x1", "9x16"],
    },
    "card-list": {
        "title": "Список",
        "props": ["headline", "items"],
        "optional": ["eyebrow", "footer"],
        "best": ["1x1", "9x16"],
    },
    "card-quote": {
        "title": "Цитата",
        "props": ["headline"],
        "optional": ["author", "footer"],
        "best": ["1x1", "9x16"],
    },
    "cover-wide": {
        "title": "Обложка 16:9",
        "props": ["headline"],
        "optional": ["note", "eyebrow", "footer"],
        "best": ["16x9"],
    },
}

#: Ширины глифов в 1/1000 em для стека `-apple-system, Segoe UI, Roboto, Arial`
#: при насыщенности 400 и 800. Это **замер** (`canvas.measureText` в Chromium), а не
#: оценка: первая версия считала кириллицу за 0,52 em вместо 0,61 и пропускала
#: обрезанные заголовки в проверку — теперь ширина текста считается по реальным метрикам.
#: Пересобрать таблицу при смене шрифтового стека: `50-SITE/scripts/measure-glyphs.mjs`.
_GLYPH_METRICS = (
    "0:636:696;1:636:696;2:636:696;3:636:696;4:636:696;5:636:696;6:636:696;7:636:696;8:636:696;9:636:696;а:613:675;б:617:698;в:589:633;г:525:522;д:691:808;е:615:678;ё:615:678;ж:901:995;з:532:581;и:650:701;й:650:701;к:604:679;л:639:732;м:754:817;н:654:691;о:612:687;п:654:691;р:635:716;с:550:593;т:583:580;у:592:652;ф:855:992;х:592:645;ц:681:741;ч:591:687;ш:915:1062;щ:942:1105;ъ:707:751;ы:790:904;ь:589:632;э:549:593;ю:842:972;я:602:642;А:684:774;Б:686:762;В:686:762;Г:610:637;Д:781:891;Е:632:683;Ё:632:683;Ж:1077:1224;З:641:710;И:748:837;Й:748:837;К:710:817;Л:752:831;М:863:995;Н:752:837;О:787:850;П:752:837;Р:603:733;С:698:734;Т:611:682;У:609:771;Ф:861:992;Х:685:771;Ц:776:928;Ч:686:808;Ш:1069:1235;Щ:1094:1326;Ъ:833:939;Ы:882:1036;Ь:686:762;Э:698:734;Ю:1080:1174;Я:695:770;a:613:675;b:635:716;c:550:593;d:635:716;e:615:678;f:352:435;g:635:716;h:634:712;i:278:343;j:278:343;k:579:665;l:278:343;m:974:1042;n:634:712;o:612:687;p:635:716;q:635:716;r:411:493;s:521:595;t:392:478;u:634:712;v:592:652;w:818:924;x:592:645;y:592:652;z:525:582;A:684:774;B:686:762;C:698:734;D:770:830;E:632:683;F:575:683;G:775:821;H:752:837;I:295:372;J:295:372;K:656:775;L:557:637;M:863:995;N:748:837;O:787:850;P:603:733;Q:787:850;R:695:770;S:635:720;T:611:682;U:732:812;V:684:774;W:989:1103;X:685:771;Y:611:724;Z:685:725;"
    ":318:348;.:318:380;,:318:380;!:401:456;?:531:580;—:1000:1000;–:500:500;-:361:415;«:612:646;»:612:646;(:390:457;):390:457;[:390:457;]:390:457;{:636:712;}:636:712;%:950:1002;№:1040:1203;+:838:838;×:838:838;/:337:365;':275:306;@:1000:1000;#:838:838;&:780:872;*:500:523;=:838:838;_:500:500;·:318:380;…:1000:1000"
)

_GLYPHS: dict[str, tuple[float, float]] = {}
for _part in _GLYPH_METRICS.split(";"):
    _ch, _light, _bold = _part.split(":")
    _GLYPHS[_ch] = (int(_light) / 1000, int(_bold) / 1000)

DEFAULT_GLYPH = (0.56, 0.62)
_TOLERANCE = 12  # px: приближение измерения не должно давать ложных «впритык»

_specs = Collection("reel_specs")
_jobs = Collection("render_jobs")
_assets = Collection("media_assets")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uid(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


# ------------------------------------------------------------------ геометрия
def text_width(text: str, size: int, *, bold: bool = False) -> int:
    """Ширина строки в пикселях по замеренным метрикам глифов."""
    index = 1 if bold else 0
    total = 0.0
    for ch in text:
        total += _GLYPHS.get(ch, DEFAULT_GLYPH)[index]
    return int(round(total * size))


def _wrap(text: str, size: int, max_width: int, *, bold: bool = False) -> list[str]:
    """Перенос по словам. Слово длиннее строки остаётся как есть — его поймает проверка."""
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if current and text_width(candidate, size, bold=bold) > max_width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def validate_brand(brand: dict | None) -> list[dict[str, Any]]:
    """Brand kit: цвета, тема, гарнитура, масштаб типографики. Общий для карточек и роликов.

    Вынесено из валидатора карточки, потому что ролик — тот же бренд: если правило живёт в
    одном рендерере, второй обязательно разойдётся.
    """
    if brand is not None and not isinstance(brand, dict):
        return [_issue("brand_invalid", "brand", "brand должен быть объектом")]
    data = brand or {}
    issues: list[dict[str, Any]] = []
    for key in ("accent", "bg", "text", "muted"):
        if key in data:
            try:
                _norm_hex(data[key])
            except ValueError as exc:
                issues.append(_issue("color_invalid", f"brand.{key}", str(exc)))
    if data.get("theme") and str(data["theme"]).lower() not in THEMES:
        issues.append(_issue("theme_invalid", "brand.theme",
                             f"тема {data['theme']!r} не из {sorted(THEMES)}"))
    if data.get("font") and str(data["font"]).lower() not in FONTS:
        issues.append(_issue("font_invalid", "brand.font",
                             f"гарнитура {data['font']!r} не из {sorted(FONTS)}"))
    if data.get("scale") is not None:
        try:
            value = float(data["scale"])
        except (TypeError, ValueError):
            value = -1.0
        if not MIN_BRAND_SCALE <= value <= MAX_BRAND_SCALE:
            issues.append(_issue("brand_scale_invalid", "brand.scale",
                                 f"масштаб типографики {MIN_BRAND_SCALE}…{MAX_BRAND_SCALE}"))
    return issues


def content_box(fmt: str) -> dict[str, int]:
    """Зона, куда можно класть контент: формат минус безопасные зоны."""
    width, height = FORMATS[fmt]
    zone = SAFE_ZONES[fmt]
    return {
        "x": zone["left"],
        "y": zone["top"],
        "width": width - zone["left"] - zone["right"],
        "height": height - zone["top"] - zone["bottom"],
    }


def contrast_ratio(color_a: str, color_b: str) -> float:
    """WCAG-контраст двух цветов ``#rrggbb`` (та же метрика, что в аудите сайта)."""

    def channel(value: int) -> float:
        c = value / 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    def luminance(hex_color: str) -> float:
        raw = _norm_hex(hex_color)
        r, g, b = (int(raw[i:i + 2], 16) for i in (1, 3, 5))
        return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)

    la, lb = luminance(color_a), luminance(color_b)
    high, low = max(la, lb), min(la, lb)
    return round((high + 0.05) / (low + 0.05), 2)


def _norm_hex(value: str) -> str:
    value = (value or "").strip()
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        raise ValueError(f"цвет должен быть в формате #rrggbb: {value!r}")
    return value.lower()


# ------------------------------------------------------------------ валидация
def _issue(code: str, field: str, message: str) -> dict[str, str]:
    return {"code": code, "field": field, "message": message}


def validate_spec(spec: dict) -> list[dict]:
    """Схема и смысловые правила спеки. Пустой список — спека годна."""
    issues: list[dict] = []
    if not isinstance(spec, dict):
        return [_issue("spec_not_object", "spec", "спека должна быть объектом")]

    if spec.get("spec_version") != SPEC_VERSION:
        issues.append(_issue("spec_version", "spec_version",
                             f"ожидается {SPEC_VERSION}, получено {spec.get('spec_version')!r}"))

    fmt = spec.get("format")
    if fmt not in FORMATS:
        issues.append(_issue("format_unknown", "format",
                             f"формат {fmt!r} не поддерживается: {sorted(FORMATS)}"))

    template = spec.get("template")
    if template not in TEMPLATES:
        issues.append(_issue("template_unknown", "template",
                             f"шаблон {template!r} не входит в библиотеку: {sorted(TEMPLATES)}"))

    props = spec.get("props")
    if not isinstance(props, dict):
        issues.append(_issue("props_missing", "props", "props должен быть объектом"))
        props = {}

    if template in TEMPLATES:
        for required in TEMPLATES[template]["props"]:
            if not str(props.get(required) or "").strip():
                issues.append(_issue("prop_required", f"props.{required}",
                                     f"шаблон {template} требует поле {required}"))

    headline = str(props.get("headline") or "").strip()
    if headline:
        if len(headline) > MAX_HEADLINE_CHARS:
            issues.append(_issue("headline_too_long", "props.headline",
                                 f"заголовок длиннее {MAX_HEADLINE_CHARS} знаков: {len(headline)}"))
        for word in headline.split():
            if len(word) > MAX_LINE_CHARS:
                issues.append(_issue("word_too_long", "props.headline",
                                     f"слово «{word[:20]}…» не переносится: длиннее {MAX_LINE_CHARS} знаков"))

    items = props.get("items")
    if items is not None:
        if not isinstance(items, list) or not items:
            issues.append(_issue("items_invalid", "props.items", "items — непустой список строк"))
        else:
            if len(items) > MAX_ITEMS:
                issues.append(_issue("items_too_many", "props.items",
                                     f"не больше {MAX_ITEMS} пунктов: {len(items)}"))
            for index, item in enumerate(items):
                text = str(item).strip()
                if not text:
                    issues.append(_issue("item_empty", f"props.items[{index}]", "пустой пункт"))
                elif len(text) > MAX_ITEM_CHARS:
                    issues.append(_issue("item_too_long", f"props.items[{index}]",
                                         f"пункт длиннее {MAX_ITEM_CHARS} знаков: {len(text)}"))

    issues.extend(validate_brand(spec.get("brand")))

    compliance = spec.get("compliance") or {}
    if not isinstance(compliance, dict):
        issues.append(_issue("compliance_invalid", "compliance", "compliance должен быть объектом"))
        return issues

    # правила маркировки — из пакета рынка (`media_compliance`), а не из ветки в рендерере:
    # другой рынок = другой пакет, а не другой код
    issues.extend(media_compliance.validate(compliance))

    return issues


# ------------------------------------------------------------------ раскладка
def _layout(spec: dict) -> dict[str, Any]:
    """Считает текстовые блоки: строки, координаты, кегли.

    Раскладка — часть рендерера, а не спеки: спека не приносит координат, поэтому
    «подвинуть заголовок влево» нельзя сломать извне. Текст центрируется по вертикали
    в свободной части зоны контента, а маркировка и подпись прижаты к низу — плашка
    рекламы не должна «уезжать» вверх при коротком тексте.
    """
    fmt = spec["format"]
    template = spec["template"]
    props = spec.get("props") or {}
    compliance = spec.get("compliance") or {}
    brand = spec.get("brand") or {}
    box = content_box(fmt)
    width = box["width"]

    scale = 1.0 if fmt != "16x9" else 0.62
    headline_size = int((74 if template == "card-quote" else 64) * scale)
    body_size = int(34 * scale)
    meta_size = max(int(26 * scale), 18)
    plate_size = max(int(22 * scale), 16)

    # --- сколько места внизу занято маркировкой и подписью
    reserved = 0
    if compliance.get("ad"):
        reserved = int(plate_size * 1.15) * 2 + 44
    elif compliance.get("min_age") is not None:
        reserved = int(meta_size * 2.4)
    text_bottom = box["y"] + box["height"] - reserved

    blocks: list[dict[str, Any]] = []
    y = box["y"]

    brand_name = str(brand.get("name") or "").strip()
    if brand_name:
        blocks.append({"kind": "brand", "lines": [brand_name], "size": meta_size,
                       "x": box["x"], "y": y, "color": TEXT_MUTED})
        y += int(meta_size * 2.0)

    eyebrow = str(props.get("eyebrow") or "").strip()
    if eyebrow:
        blocks.append({"kind": "eyebrow", "lines": [eyebrow], "size": meta_size,
                       "x": box["x"], "y": y, "color": TEXT_MUTED})
        y += int(meta_size * 1.8) + 18

    headline = str(props.get("headline") or "").strip()
    for line in _wrap(headline, headline_size, width, bold=True):
        blocks.append({"kind": "headline", "lines": [line], "size": headline_size,
                       "x": box["x"], "y": y, "color": TEXT_COLOR})
        y += int(headline_size * 1.1)

    items = props.get("items")
    if items:
        y += int(34 * scale)
        for item in items:
            for index, line in enumerate(_wrap(str(item), body_size, width - 44)):
                blocks.append({"kind": "item" if index == 0 else "item-line", "lines": [line],
                               "size": body_size, "x": box["x"] + 44,
                               "y": y, "color": TEXT_COLOR})
                y += int(body_size * 1.32)
            y += 14

    note = str(props.get("note") or "").strip()
    if note:
        y += 26
        for line in _wrap(note, body_size, width):
            blocks.append({"kind": "note", "lines": [line], "size": body_size,
                           "x": box["x"], "y": y, "color": TEXT_MUTED})
            y += int(body_size * 1.32)

    author = str(props.get("author") or "").strip()
    if author:
        y += 26
        blocks.append({"kind": "author", "lines": [author], "size": body_size,
                       "x": box["x"], "y": y, "color": TEXT_MUTED})
        y += int(body_size * 1.32)

    # --- вертикальное центрирование текста в свободной части зоны
    if blocks:
        used = max(b["y"] + int(b["size"] * 1.1) for b in blocks) - box["y"]
        free = text_bottom - box["y"]
        if used < free * 0.94:
            shift = int((free - used) / 2)
            for block in blocks:
                block["y"] += shift

    # --- низ: подпись, маркировка, возрастная метка (прижаты к нижней границе)
    bottom = box["y"] + box["height"]
    footer = str(props.get("footer") or "").strip()
    if footer:
        bottom -= int(meta_size * 1.4)
        blocks.append({"kind": "footer", "lines": [footer], "size": meta_size,
                       "x": box["x"], "y": bottom, "color": TEXT_MUTED})

    # маркировка: текст плашек приходит из пакета правил, здесь — только место на кадре
    plate_color = brand_kit(spec.get("brand"))["plate"]
    for plate in reversed(media_compliance.plates(compliance)):
        kind = plate["kind"]
        lines = list(plate["lines"])
        if kind == "ad":
            if len(lines) > 1:
                bottom -= int(plate_size * 1.3) + 16
                blocks.append({"kind": "erid-meta", "lines": lines[1:], "size": plate_size,
                               "x": box["x"] + 20, "y": bottom, "color": plate_color})
            bottom -= int(plate_size * 1.5)
            blocks.append({"kind": "erid", "lines": lines[:1], "size": plate_size,
                           "x": box["x"] + 20, "y": bottom, "color": plate_color})
        elif kind == "age":
            bottom -= int(meta_size * 1.6)
            blocks.append({"kind": "age", "lines": lines, "size": meta_size,
                           "x": box["x"], "y": bottom, "color": TEXT_MUTED})
        else:
            bottom -= int(meta_size * 1.25)
            blocks.append({"kind": "disclaimer", "lines": lines, "size": meta_size,
                           "x": box["x"], "y": bottom, "color": TEXT_MUTED})

    return {"format": fmt, "blocks": blocks, "box": box, "scale": scale}


def measure(spec: dict) -> list[dict]:
    """bbox каждого текстового блока — основа проверок обрезки и safe zone."""
    layout = _layout(spec)
    boxes: list[dict] = []
    for block in layout["blocks"]:
        bold = block["kind"] == "headline"
        width = (max(text_width(line, block["size"], bold=bold) for line in block["lines"])
                 if block["lines"] else 0)
        height = int(block["size"] * 1.1) * max(len(block["lines"]), 1)
        boxes.append({"kind": block["kind"], "x": block["x"], "y": block["y"],
                      "width": width, "height": height})
    return boxes


def check_layout(spec: dict) -> list[dict]:
    """Обрезка и попадание под интерфейс канала."""
    issues: list[dict] = []
    fmt = spec["format"]
    full_w, full_h = FORMATS[fmt]
    box = content_box(fmt)
    for index, bbox in enumerate(measure(spec)):
        right = bbox["x"] + bbox["width"]
        bottom = bbox["y"] + bbox["height"]
        if bbox["x"] < 0 or bbox["y"] < 0 or right > full_w or bottom > full_h:
            issues.append(_issue("out_of_frame", f"blocks[{index}]",
                                 f"блок {bbox['kind']} выходит за кадр {full_w}×{full_h}"))
            continue
        inside = (bbox["x"] >= box["x"] - _TOLERANCE
                  and bbox["y"] >= box["y"] - _TOLERANCE
                  and right <= box["x"] + box["width"] + _TOLERANCE
                  and bottom <= box["y"] + box["height"] + _TOLERANCE)
        if not inside:
            issues.append(_issue("safe_zone", f"blocks[{index}]",
                                 f"блок {bbox['kind']} попадает под интерфейс канала: "
                                 f"зона контента {box['width']}×{box['height']} при "
                                 f"{box['x']},{box['y']}"))
    return issues


def check_contrast(spec: dict) -> list[dict]:
    """Контраст каждой текстовой пары: тот же порог 4.5:1, что на сайте."""
    issues: list[dict] = []
    background = (spec.get("brand") or {}).get("bg") or "#09090b"
    layout = _layout(spec)
    seen: set[tuple[str, str]] = set()
    for block in layout["blocks"]:
        pair = (block["color"].lower(), _norm_hex(background))
        if pair in seen:
            continue
        seen.add(pair)
        ratio = contrast_ratio(pair[0], pair[1])
        if ratio < MIN_CONTRAST:
            issues.append(_issue("contrast", f"blocks.{block['kind']}",
                                 f"контраст {ratio}:1 меньше {MIN_CONTRAST}:1 "
                                 f"({pair[0]} на {pair[1]})"))
    return issues


def check(spec: dict) -> dict:
    """Полный прогон проверок этапа A. ``ok`` = можно рендерить."""
    issues = validate_spec(spec)
    if not issues:
        issues += check_layout(spec)
        issues += check_contrast(spec)
    return {
        "ok": not issues,
        "issues": issues,
        "metrics": {
            "format": spec.get("format"),
            "template": spec.get("template"),
            "boxes": len(measure(spec)) if not validate_spec(spec) else 0,
            "renderer": RENDERER,
        },
    }


# ------------------------------------------------------------------ рендер
def brand_kit(brand: dict | None) -> dict:
    """Brand kit: тема, гарнитура, цвета, масштаб типографики — из спеки, а не из кода.

    Неизвестные значения не подменяются молча: валидатор спеки вернёт ошибку, а рендерер
    всё равно возьмёт безопасное значение — ролик не должен собраться «наполовину брендовым».
    """
    data = brand or {}
    theme_code = str(data.get("theme") or DEFAULT_THEME).strip().lower()
    theme = THEMES.get(theme_code) or THEMES[DEFAULT_THEME]
    font_code = str(data.get("font") or DEFAULT_FONT).strip().lower()
    font = FONTS.get(font_code) or FONTS[DEFAULT_FONT]
    try:
        scale = float(data.get("scale") or 1.0)
    except (TypeError, ValueError):
        scale = 1.0
    scale = min(MAX_BRAND_SCALE, max(MIN_BRAND_SCALE, scale))
    return {
        "bg": _norm_hex(data.get("bg") or theme["bg"]),
        "accent": _norm_hex(data.get("accent") or "#7c5cff"),
        "text": _norm_hex(data.get("text") or theme["text"]),
        "muted": _norm_hex(data.get("muted") or theme["muted"]),
        "scrim": theme["scrim"],
        "plate": theme["plate"],
        "font": font,
        "font_code": font_code if font_code in FONTS else DEFAULT_FONT,
        "theme": theme_code if theme_code in THEMES else DEFAULT_THEME,
        "scale": scale,
    }


def _palette(spec: dict) -> dict[str, str]:
    return brand_kit(spec.get("brand"))


def render_svg(spec: dict) -> str:
    """Детерминированный SVG: один и тот же вход — побайтово тот же выход."""
    fmt = spec["format"]
    width, height = FORMATS[fmt]
    layout = _layout(spec)
    palette = _palette(spec)
    props = spec.get("props") or {}
    headline_size = next((b["size"] for b in layout["blocks"] if b["kind"] == "headline"), 64)

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="{escape(str(props.get("headline") or "карточка"))[:120]}">',
        "<defs>",
        f'<linearGradient id="glow" x1="0" y1="0" x2="1" y2="1">'
        f'<stop offset="0" stop-color="{palette["accent"]}" stop-opacity="0.20"/>'
        f'<stop offset="1" stop-color="{palette["accent"]}" stop-opacity="0"/>'
        f"</linearGradient>",
        f'<pattern id="grid" width="120" height="120" patternUnits="userSpaceOnUse">'
        f'<path d="M 120 0 L 0 0 0 120" fill="none" stroke="#ffffff" stroke-opacity="0.035"/>'
        f"</pattern>",
        "</defs>",
        f'<rect width="{width}" height="{height}" fill="{palette["bg"]}"/>',
        f'<rect width="{width}" height="{height}" fill="url(#grid)"/>',
        f'<ellipse cx="{int(width * 0.76)}" cy="{int(-height * 0.06)}" '
        f'rx="{int(width * 0.5)}" ry="{int(height * 0.26)}" fill="url(#glow)"/>',
    ]

    box = layout["box"]
    # акцентная линия слева: визуальный «позвоночник» карточки, привязан к заголовку
    first = next((b for b in layout["blocks"] if b["kind"] == "headline"), None)
    if first:
        parts.append(
            f'<rect x="{box["x"] - 26}" y="{first["y"]}" width="6" '
            f'height="{int(headline_size * 2.4)}" rx="3" fill="{palette["accent"]}" opacity="0.85"/>'
        )

    for block in layout["blocks"]:
        color = block["color"]
        if block["kind"] in {"headline", "author", "note", "eyebrow", "brand"}:
            color = palette["text"] if block["kind"] == "headline" else palette["muted"]
        weight = "800" if block["kind"] == "headline" else "500"
        anchor_y = block["y"] + block["size"] - int(block["size"] * 0.18)
        for line in block["lines"]:
            text = escape(line)
            if block["kind"] == "headline":
                text = f'<tspan font-size="{block["size"]}" font-weight="800">{text}</tspan>'
            parts.append(
                f'<text x="{block["x"]}" y="{anchor_y}" fill="{color}" '
                f'font-size="{block["size"]}" font-weight="{weight}" '
                f'font-family="{palette['font']}">{text}</text>'
            )
        if block["kind"] == "item":
            parts.append(
                f'<circle cx="{block["x"] - 26}" cy="{block["y"] + block["size"] * 0.55}" '
                f'r="7" fill="{palette["accent"]}"/>'
            )

    compliance = spec.get("compliance") or {}
    if media_compliance.plates(compliance):
        plate = next((b for b in layout["blocks"] if b["kind"] == "erid"), None)
        if plate:
            parts.append(
                f'<rect x="{plate["x"] - 18}" y="{plate["y"] - 14}" '
                f'width="{box["width"] + 36}" height="{int(plate["size"] * 3.4)}" rx="14" '
                f'fill="#f5b544" fill-opacity="0.10" stroke="#f5b544" stroke-opacity="0.45"/>'
            )

    parts.append("</svg>")
    return "".join(parts)


# ------------------------------------------------------------------ задачи
def _canonical(spec: dict) -> str:
    return json.dumps(spec, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def spec_hash(spec: dict) -> str:
    """Ключ идемпотентности: спека + версия рендерера."""
    return hashlib.sha256(f"{RENDERER}:{_canonical(spec)}".encode("utf-8")).hexdigest()[:32]


def build_spec(post: dict, fmt: str, template: str = "card-hero", *, brand: dict | None = None,
               compliance: dict | None = None) -> dict:
    """Спека одного формата из поста. Единственное место, где текст поста становится карточкой."""
    props: dict[str, Any] = {"headline": str(post.get("title") or post.get("text") or "").strip()}
    if post.get("eyebrow"):
        props["eyebrow"] = str(post["eyebrow"])
    if post.get("note"):
        props["note"] = str(post["note"])
    if post.get("items"):
        props["items"] = [str(item) for item in post["items"]][:MAX_ITEMS]
    if post.get("author"):
        props["author"] = str(post["author"])
    footer = str(post.get("footer") or post.get("url") or "").strip()
    if footer:
        props["footer"] = footer

    spec = {
        "spec_version": SPEC_VERSION,
        "format": fmt,
        "template": template,
        "props": props,
        "brand": dict(brand or {}),
        "compliance": dict(compliance or {"ad": False}),
    }
    if template == "card-list" and not props.get("items"):
        spec["template"] = "card-hero"     # список без пунктов — это герой
    return spec


def submit(spec: dict, *, post: dict | None = None) -> dict:
    """Проверить спеку, поставить задачу и отрендерить. Возвращает задачу и артефакт."""
    report = check(spec)
    spec_id = _uid("sp_")
    digest = spec_hash(spec)
    stored = _specs.put({
        "id": spec_id, "spec_version": spec.get("spec_version"), "format": spec.get("format"),
        "template": spec.get("template"), "body": spec, "spec_hash": digest,
        "post": post or {}, "created_at": _now(),
    })
    job_id = _uid("rj_")
    job = {
        "id": job_id, "spec_id": spec_id, "kind": "card", "format": spec.get("format"),
        "status": "queued", "attempts": 0, "renderer": RENDERER, "asset_id": None,
        "error": None, "created_at": _now(), "started_at": None, "finished_at": None,
        "checks": report,
    }

    if not report["ok"]:
        job.update(status="failed", error="spec_rejected",
                   finished_at=_now(), attempts=1)
        return {"job": _jobs.put(job), "asset": None, "checks": report}

    # идемпотентность: та же спека и та же версия рендерера — отдаём готовый артефакт
    for existing in _jobs.all():
        if (existing.get("status") == "done" and existing.get("spec_hash") == digest
                and existing.get("asset_id")):
            asset = _assets.get(existing["asset_id"])
            if asset:
                job.update(status="done", asset_id=asset["id"], spec_hash=digest,
                           started_at=_now(), finished_at=_now(), attempts=0, checks=report)
                return {"job": _jobs.put(job), "asset": asset, "checks": report,
                        "reused": True}

    job.update(status="rendering", started_at=_now(), attempts=1, spec_hash=digest)
    asset = _render_asset(spec, spec_id, digest, report)
    job.update(status="done", asset_id=asset["id"], finished_at=_now())
    return {"job": _jobs.put(job), "asset": asset, "checks": report}


def _asset_key(asset_id: str, ext: str) -> str:
    """Ключ в хранилище: ``media/<workspace>/<id><ext>``. Строится только кодом."""
    workspace = str(_specs._scope() or DEMO_WORKSPACE).lower()
    workspace = re.sub(r"[^a-z0-9_-]", "-", workspace)
    return f"media/{workspace}/{asset_id}{ext}"


def _render_asset(spec: dict, spec_id: str, digest: str, report: dict) -> dict:
    """Отрисовать SVG, положить его в хранилище и, если есть растеризатор, добавить PNG."""
    asset_id = _uid("as_")
    width, height = FORMATS[spec["format"]]
    svg = render_svg(spec)
    svg_bytes = svg.encode("utf-8")

    svg_key = _asset_key(asset_id, ".svg")
    stored = storage.put(svg_key, svg_bytes, mime="image/svg+xml")

    png_bytes, raster_backend = raster.rasterize(svg, width, height)
    png_key = None
    if png_bytes:
        png_key = _asset_key(asset_id, ".png")
        storage.put(png_key, png_bytes, mime="image/png")

    return _assets.put({
        "id": asset_id, "kind": "generated_card", "format": spec["format"],
        "template": spec["template"],
        "mime": "image/png" if png_bytes else "image/svg+xml",
        "width": width, "height": height,
        "bytes": len(png_bytes or svg_bytes),
        "spec_id": spec_id, "renderer": RENDERER, "created_at": _now(),
        "storage": stored["backend"], "svg_key": svg_key, "png_key": png_key,
        "svg_bytes": len(svg_bytes), "png_bytes": len(png_bytes or b""),
        "raster_backend": raster_backend,
        "meta": {"checks": report["metrics"], "spec_hash": digest},
    })


def asset_content(asset_id: str, fmt: str = "svg") -> tuple[bytes, str] | None:
    """Байты артефакта и mime.

    Если файл в хранилище пропал (почистили спул, TTL), SVG **восстанавливается
    из спеки** — он детерминированный. Это дешевле, чем хранить копию в документе,
    и честнее, чем отдавать 404 на артефакт, который можно пересобрать за миллисекунды.
    """
    if str(asset_id).startswith("reel_") or fmt in {"mp4", "mp3", "srt"}:
        from . import media_reel                            # локальный импорт: модуль зависит от media

        return media_reel.content(asset_id, fmt)

    item = _assets.get(asset_id)
    if not item:
        return None

    if fmt == "png":
        if not item.get("png_key"):
            svg_pair = asset_content(asset_id, "svg")
            if not svg_pair:
                return None
            png, backend = raster.rasterize(svg_pair[0].decode("utf-8"),
                                            item["width"], item["height"])
            if not png:
                return None
            item["png_key"] = _asset_key(asset_id, ".png")
            item["raster_backend"] = backend
            item["mime"] = "image/png"
            _assets.put(item)
            storage.put(item["png_key"], png, mime="image/png")
        data = storage.get(item["png_key"])
        return (data, "image/png") if data else None

    data = storage.get(item["svg_key"]) if item.get("svg_key") else None
    if data is None:
        found = _specs.get(item["spec_id"])
        if not found:
            return None
        data = render_svg(found["body"]).encode("utf-8")
        if item.get("svg_key"):
            storage.put(item["svg_key"], data, mime="image/svg+xml")
    return data, "image/svg+xml"


# ------------------------------------------------------------------ публичные ссылки
def _secret() -> bytes | None:
    value = os.getenv("MEDIA_URL_SECRET") or os.getenv("JWT_SECRET") or ""
    return value.encode("utf-8") if len(value) >= 8 else None


def _sign(payload: str) -> str:
    secret = _secret()
    if secret is None:
        raise ValueError("MEDIA_URL_SECRET не задан: подписанные ссылки недоступны")
    return hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def signed_token(asset_id: str, fmt: str = "png", *, ttl: int = 3600,
                 now: float | None = None) -> str:
    """Токен публичной ссылки: id, формат и срок в одном неподделываемом значении."""
    if fmt not in {"svg", "png", "mp4", "mp3", "srt"}:
        raise ValueError("формат ссылки — svg, png, mp4, mp3 или srt")
    expires = int((now if now is not None else time.time()) + ttl)
    payload = f"{asset_id}:{fmt}:{expires}"
    raw = f"{payload}:{_sign(payload)}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def resolve_token(token: str) -> tuple[str, str] | None:
    """Проверить токен ссылки. ``None`` — подделка, истёк срок или мусор."""
    try:
        padded = token + "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        asset_id, fmt, expires, signature = raw.rsplit(":", 3)
    except Exception:                                   # noqa: BLE001 — любой мусор = невалидный токен
        return None
    expected = _sign(f"{asset_id}:{fmt}:{expires}")
    if not hmac.compare_digest(signature, expected):
        return None
    if int(expires) < int(time.time()):
        return None
    if fmt not in {"svg", "png", "mp4", "mp3", "srt"}:
        return None
    return asset_id, fmt


def best_format(asset_id: str) -> str | None:
    """Какой формат реально есть у артефакта: ``png`` если растр получился, иначе ``svg``.

    Каналы хотят PNG (превью в ленте), но если растеризатора на узле нет, отдавать
    битую ссылку хуже, чем отправить SVG: Teletype/VK его не покажут, зато не будет 404.
    """
    if str(asset_id).startswith("reel_"):
        from . import media_reel

        reel = media_reel.reel(asset_id)
        if reel is None:
            return None
        return "mp4" if reel.get("video_key") else ("png" if reel.get("poster_key") else None)
    item = _assets.get(asset_id)
    if item is None:
        return None
    return "png" if item.get("png_key") else "svg"


def public_url(asset_id: str, fmt: str = "png", *, ttl: int = 3600,
               base: str | None = None) -> str:
    """Ссылка, которую адаптер канала скачает без наших заголовков авторизации."""
    root = (base or os.getenv("MEDIA_PUBLIC_BASE") or os.getenv("SITE_URL")
            or "http://127.0.0.1:8000").rstrip("/")
    return f"{root}/api/v1/media/public/{signed_token(asset_id, fmt, ttl=ttl)}"


# ------------------------------------------------------------------ TTL
def purge(ttl_days: int = 14) -> dict:
    """Удалить старые артефакты и их документы. Вызывается воркером по cron."""
    if ttl_days < 1:
        raise ValueError("TTL в днях должен быть >= 1")
    ttl_seconds = ttl_days * 86400
    stored = storage.purge_expired(ttl_seconds)
    cutoff = time.time() - ttl_seconds
    removed = 0
    for item in list(_assets.all()):
        created = item.get("created_at")
        stamp = created.timestamp() if hasattr(created, "timestamp") else None
        if stamp is not None and stamp < cutoff:
            _assets.delete(item["id"])
            removed += 1
    from . import media_reel

    reels = media_reel.purge(ttl_days)
    return {**stored, "assets_removed": removed, "reels_removed": reels.get("removed", 0),
            "ttl_days": ttl_days}


def cards_from_post(post: dict, formats: Iterable[str] = ("1x1", "9x16"), *,
                    template: str = "card-hero", brand: dict | None = None,
                    compliance: dict | None = None) -> dict:
    """Карточки из поста сразу в нескольких форматах: то, что вызывает интерфейс."""
    items: list[dict] = []
    skipped: list[dict] = []
    for fmt in formats:
        if fmt not in FORMATS:
            skipped.append({"format": fmt, "reason": "format_unknown"})
            continue
        spec = build_spec(post, fmt, template, brand=brand, compliance=compliance)
        result = submit(spec, post=post)
        items.append({"format": fmt, "spec_id": result["job"]["spec_id"],
                      "job_id": result["job"]["id"], "status": result["job"]["status"],
                      "asset_id": (result["asset"] or {}).get("id"),
                      "checks": result["checks"],
                      "reused": result.get("reused", False)})
    return {"items": items, "skipped": skipped,
            "ok": any(item["status"] == "done" for item in items)}


# ------------------------------------------------------------------ чтение
def jobs(limit: int = 50) -> list[dict]:
    return sorted(_jobs.all(), key=lambda j: str(j.get("created_at")), reverse=True)[:limit]


def job(job_id: str) -> dict | None:
    return _jobs.get(job_id)


def spec(spec_id: str) -> dict | None:
    return _specs.get(spec_id)


def asset(asset_id: str) -> dict | None:
    return _assets.get(asset_id)


def cancel(job_id: str) -> dict | None:
    current = _jobs.get(job_id)
    if not current:
        return None
    if current["status"] in {"done", "failed", "cancelled"}:
        return current
    current.update(status="cancelled", finished_at=_now(), error="cancelled_by_user")
    return _jobs.put(current)


def templates() -> list[dict]:
    """Каталог шаблонов — то, из чего модель может выбирать."""
    return [{"id": key, **value, "formats": [f for f in FORMATS if f in value["best"]]}
            for key, value in TEMPLATES.items()]


def limits() -> dict:
    return {"formats": {k: {"width": v[0], "height": v[1], "safe_zone": SAFE_ZONES[k]}
                        for k, v in FORMATS.items()},
            "max_headline_chars": MAX_HEADLINE_CHARS, "max_line_chars": MAX_LINE_CHARS,
            "max_items": MAX_ITEMS, "max_item_chars": MAX_ITEM_CHARS,
            "min_contrast": MIN_CONTRAST, "spec_version": SPEC_VERSION}


def summary() -> dict:
    all_jobs = _jobs.all()
    by_status: dict[str, int] = {}
    for item in all_jobs:
        by_status[item["status"]] = by_status.get(item["status"], 0) + 1
    return {"jobs": len(all_jobs), "by_status": by_status,
            "assets": len(_assets.all()), "specs": len(_specs.all()),
            "renderer": RENDERER, "raster": raster.available(),
            "storage": storage.usage(), "workspace": DEMO_WORKSPACE}
