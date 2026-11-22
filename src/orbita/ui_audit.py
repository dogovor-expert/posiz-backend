"""Аудит визуального слоя: дизайн-система, доступность, движение, компоненты, размещение (UI-QA).

Зачем отдельный движок, а не «посмотреть глазами»:

**1. Находка — это проверка, а не мнение.** Каждое замечание в отчёте имеет идентификатор правила,
файл и строку, поэтому его можно воспроизвести, исправить и закрыть. Ровно так же устроен
`release_gate`: «красиво/некрасиво» — не аргумент, «контраст 3.9:1 при объявленных 4.5:1» —
аргумент.

**2. Объявленное сравнивается с реализованным.** `20-DESIGN/design-system.md` объявляет токены,
шкалу кеглей, тайминги движения, правила фокуса и зоны нажатия. Аудит читает этот документ и
файлы интерфейса и ищет расхождения: токен, разошедшийся с системой; кегль вне шкалы; анимация
без `prefers-reduced-motion`; кнопка без доступного имени.

**3. Аудит не рендерит браузер.** Геометрия (переполнения, адаптив) уже измерена реальным
Chromium в `20-DESIGN/layout-audit` (`50-SITE/scripts/audit-layout.mjs`, 13 ширин). Здесь —
статический слой: контраст считается из фактических цветов, зоны нажатия — из объявленных
`height`/`padding`/`line-height`, доступность — из разметки и обработчиков. В отчёте это сказано
явно, чтобы цифры не смешивались с замерами.

Запуск:
    PYTHONPATH=../../40-BACKEND/src python audit_ui.py            # текст
    PYTHONPATH=... python audit_ui.py --json report.json --html report.html
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable

#: корень репозитория: 40-BACKEND/src/orbita/ui_audit.py → четыре уровня вверх
REPO = Path(__file__).resolve().parents[3]

SEVERITIES = ("critical", "high", "medium", "low", "info")

#: какие файлы считаются интерфейсом. Пути — от корня репозитория.
TARGETS = {
    "app": ["30-APP/index.html"],
    "site": ["50-SITE/src/app/globals.css", "50-SITE/src/app/layout.tsx"],
    "site_pages": [],          # дозаполняется обходом 50-SITE/src/app/**/*.tsx
    "site_components": [],     # 50-SITE/src/components/**/*.tsx
    "design": ["20-DESIGN/design-system.md"],
    "artifacts": [],           # 20-DESIGN/articles/backend/*.html
}

#: объявленная шкала кеглей (design-system.md §2) и допустимые производные
FONT_SCALE = {11, 12, 13, 14, 16, 20, 24, 32}
#: шкала отступов: базовые ступени §1 (--sp-1…--sp-7) + плотная сетка 2 px для
#: инструментальных панелей (шаг 2 px — не «на глаз», а проверяемое правило: все
#: отступы обязаны быть чётными)
SPACING_SCALE = {0, 2, 4, 6, 8, 10, 12, 16, 24, 32, 48}
#: радиусы из токенов §1 + служебные (пилюли, круги)
RADIUS_OK = {0, 2, 4, 6, 8, 10, 12, 14, 16, 20, 22, 999, 50}
#: тайминги движения (§5: 120–180 мс; допускаем 60–400 как «мягкое» нарушение)
MOTION_MIN, MOTION_MAX = 60, 400
#: отклик на действие (цвет, тень, обводка) — 120–180 мс
MOTION_TARGET = (120, 180)
#: переезд поверхности (шторка, панель, колонка) — до 240 мс: 180 мс читается как «рывок»,
#: а всё, что дольше 240, уже тормозит. Правило §5 дизайн-системы.
MOTION_TARGET_SURFACE = (120, 240)


# ------------------------------------------------------------------ цвет и контраст
def _rgb(value: str) -> tuple[float, float, float] | None:
    text = str(value or "").strip().lower()
    if text.startswith("#"):
        raw = text[1:]
        if len(raw) == 3:
            raw = "".join(ch * 2 for ch in raw)
        if len(raw) != 6:
            return None
        try:
            return tuple(int(raw[index:index + 2], 16) / 255.0 for index in (0, 2, 4))  # type: ignore[return-value]
        except ValueError:
            return None
    shade = re.fullmatch(r"rgba?\(([^)]+)\)", text)
    if shade:
        parts = [part.strip() for part in shade.group(1).split(",")]
        if len(parts) >= 3:
            return tuple(_channel(part) for part in parts[:3])  # type: ignore[return-value]
    return None


def _channel(part: str) -> float:
    if part.endswith("%"):
        return float(part[:-1]) / 100.0
    return float(part) / 255.0


def _luminance(rgb: tuple[float, float, float]) -> float:
    def linear(channel: float) -> float:
        return channel / 12.92 if channel <= 0.03928 else ((channel + 0.055) / 1.055) ** 2.4

    red, green, blue = (linear(channel) for channel in rgb)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast(foreground: str, background: str) -> float | None:
    """Коэффициент контраста WCAG 2.x для пары цветов."""
    front, back = _rgb(foreground), _rgb(background)
    if front is None or back is None:
        return None
    light, dark = sorted((_luminance(front), _luminance(back)), reverse=True)
    return round((light + 0.05) / (dark + 0.05), 2)


# ------------------------------------------------------------------ разбор файлов
class File:
    """Файл интерфейса: текст, строки, выделенный CSS и позиция начала CSS."""

    def __init__(self, path: Path, text: str) -> None:
        self.path = path
        self.text = text
        self.lines = text.splitlines()
        self.css, self.css_start = self._extract_css()

    def _extract_css(self) -> tuple[str, int]:
        if self.path.suffix == ".css":
            return self.text, 1
        blocks = list(re.finditer(r"<style[^>]*>(.*?)</style>", self.text, re.S))
        if not blocks:
            return "", 1
        css = "\n\n".join(block.group(1) for block in blocks)
        start = self.text[:blocks[0].start(1)].count("\n") + 1
        return css, start

    def line_of(self, offset: int, base: str | None = None) -> int:
        source = self.text if base is None else base
        return source[:offset].count("\n") + 1

    def css_line(self, offset: int) -> int:
        return self.css[:offset].count("\n") + self.css_start


def load(root: Path) -> dict[str, File]:
    """Прочитать цели аудита. Отсутствующий файл — не ошибка: набор целей может меняться."""
    targets = {key: list(value) for key, value in TARGETS.items()}
    targets["site_pages"] = sorted(str(path.relative_to(root))
                                   for path in (root / "50-SITE/src/app").rglob("*.tsx"))
    targets["site_components"] = sorted(str(path.relative_to(root))
                                        for path in (root / "50-SITE/src/components").rglob("*.tsx"))
    targets["artifacts"] = sorted(str(path.relative_to(root)) for path in
                                  (root / "20-DESIGN/articles/backend").glob("*.html"))
    found: dict[str, File] = {}
    for paths in targets.values():
        for name in paths:
            path = root / name
            if path.is_file():
                found[name] = File(path, path.read_text(encoding="utf-8"))
    return found


def findings() -> list[dict[str, Any]]:
    return []


def add(store: list[dict], *, rule: str, area: str, severity: str, file: str, line: int,
        message: str, evidence: str = "", selector: str = "", hint: str = "") -> None:
    store.append({"rule": rule, "area": area, "severity": severity, "file": file,
                  "line": int(line or 0), "message": message, "evidence": evidence[:240],
                  "selector": selector[:120], "hint": hint[:240]})


# ------------------------------------------------------------------ CSS-помощники
def strip_comments(css: str) -> str:
    """Убрать /* … */, сохранив переводы строк: номера строк в отчёте остаются верными."""
    return re.sub(r"/\*.*?\*/", lambda m: re.sub(r"[^\n]", " ", m.group(0)), css or "", flags=re.S)


def css_rules(css: str) -> Iterable[tuple[str, str]]:
    """Правила CSS как (селектор, тело). Вложенность @media не разворачивается: селектор — тот,
    что стоит непосредственно перед блоком, этого достаточно для проверок."""
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", strip_comments(css)):
        selector = " ".join(match.group(1).split())
        yield selector, match.group(2)


def declarations(body: str) -> dict[str, str]:
    rows: dict[str, str] = {}
    for chunk in body.split(";"):
        if ":" not in chunk:
            continue
        name, _, value = chunk.partition(":")
        rows[name.strip().lower()] = " ".join(value.split())
    return rows


def px_values(value: str) -> list[float]:
    return [float(number) for number in re.findall(r"(-?\d+(?:\.\d+)?)px", value or "")]


def durations(value: str) -> list[float]:
    """Все длительности из значения `transition`/`animation` — в миллисекундах."""
    rows: list[float] = []
    for number, unit in re.findall(r"(?<![\d.])(\d*\.?\d+)(ms|s)\b", value or ""):
        rows.append(float(number) * (1000 if unit == "s" else 1))
    return rows


def root_tokens(css: str) -> dict[str, str]:
    """Базовые токены: только первый блок `:root`.

    Переопределения внутри `@media (prefers-contrast: more)` и подобных — не дрейф,
    а штатное поведение темы, поэтому в базовый набор они не попадают.
    """
    for selector, body in css_rules(css):
        if ":root" in selector:
            return {name: value for name, value in declarations(body).items()
                    if name.startswith("--")}
    return {}


def parse_design_tokens(markdown: str) -> dict[str, str]:
    """Токены из объявленного блока `:root` в design-system.md."""
    block = re.search(r"```css(.*?)```", markdown or "", re.S)
    return root_tokens(block.group(1)) if block else {}


def parse_documented_drift(markdown: str) -> dict[str, str]:
    """Отступления от токенов, зафиксированные в дизайн-системе.

    Формат строки в §1.1:  `--token: значение` — причина
    Так «дрейф» становится либо ошибкой, либо осознанным решением с записанной причиной.
    """
    rows: dict[str, str] = {}
    for line in (markdown or "").splitlines():
        match = re.search(r"`(--[a-z0-9-]+):\s*([^`]+)`\s*—\s*(\S.*)", line)
        if match:
            rows[match.group(1)] = match.group(2).strip()
    return rows


def parse_declared_contrast(markdown: str) -> list[dict[str, Any]]:
    """Таблица замеренных пар из §6 дизайн-системы: текст, фон, ожидаемое отношение, вердикт."""
    rows: list[dict[str, Any]] = []
    for line in (markdown or "").splitlines():
        match = re.search(r"\|\s*([^|]*?)#([0-9a-fA-F]{6})\s*\|\s*([^|]*?)#([0-9a-fA-F]{6})\s*\|"
                          r"\s*([\d.]+):1\s*\|\s*([^|]+)\|", line)
        if not match:
            continue
        rows.append({"fg_label": match.group(1).strip(" `"),
                     "fg": f"#{match.group(2)}",
                     "bg_label": match.group(3).strip(" `"),
                     "bg": f"#{match.group(4)}",
                     "expected": float(match.group(5)),
                     "verdict": match.group(6).strip()})
    return rows


# ------------------------------------------------------------------ сбор контекста
class Context:
    def __init__(self, files: dict[str, File]) -> None:
        self.files = files
        self.app = files.get("30-APP/index.html")
        self.site_css = files.get("50-SITE/src/app/globals.css")
        self.design = files.get("20-DESIGN/design-system.md")
        self.app_css = self.app.css if self.app else ""
        self.app_tokens = root_tokens(self.app_css) if self.app else {}
        self.site_tokens = root_tokens(self.site_css.text) if self.site_css else {}
        self.declared_tokens = parse_design_tokens(self.design.text) if self.design else {}

    def app_css_rules(self) -> list[tuple[str, str, int]]:
        rows: list[tuple[str, str, int]] = []
        if not self.app:
            return rows
        for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", strip_comments(self.app.css)):
            selector = " ".join(match.group(1).split())
            rows.append((selector, match.group(2), self.app.css_line(match.start())))
        return rows

    def site_css_rules(self) -> list[tuple[str, str, int]]:
        rows: list[tuple[str, str, int]] = []
        if not self.site_css:
            return rows
        for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", strip_comments(self.site_css.text)):
            selector = " ".join(match.group(1).split())
            rows.append((selector, match.group(2),
                         self.site_css.text[:match.start()].count("\n") + 1))
        return rows


# ------------------------------------------------------------------ правила: дизайн-система
def rule_token_drift(ctx: Context, store: list[dict]) -> None:
    """Токен разошёлся с объявленным в дизайн-системе — интерфейс «дрейфует» тихо."""
    declared = ctx.declared_tokens
    if not declared:
        return
    documented = parse_documented_drift(ctx.design.text) if ctx.design else {}
    for label, tokens, file in (("приложение", ctx.app_tokens, "30-APP/index.html"),
                                ("сайт", ctx.site_tokens, "50-SITE/src/app/globals.css")):
        for name, value in declared.items():
            actual = tokens.get(name)
            if actual is None:
                continue
            if _norm(actual) == _norm(documented.get(name, "")):
                continue                      # отступление зафиксировано в §1.1
            if _norm(actual) != _norm(value):
                severity = "high" if name in {"--fg", "--fg-muted", "--fg-dim", "--bg", "--accent",
                                              "--accent-strong"} else "medium"
                add(store, rule="ds.token.drift", area="design", severity=severity, file=file,
                    line=0, selector=name,
                    message=f"{label}: токен {name} = {actual}, в системе {value}",
                    evidence=f"{name}: {actual} ≠ {value}",
                    hint="вернуть значение из дизайн-системы или обновить её осознанно, с замером")


def rule_tokens_undefined(ctx: Context, store: list[dict]) -> None:
    """`var(--x)` без объявления и без запасного значения — токен молча «съедается».

    Именно так «пропал» фокус в приложении: `box-shadow: var(--focus)` ссылался на токен,
    которого в файле не было, — обводка не рисовалась ни при каких условиях.

    Что НЕ считается ошибкой: `var(--x, запасное)` (осознанный fallback) и переменные, которые
    объявлены в любом правиле файла (не только в `:root`) или выставляются из кода через
    `style.setProperty('--x', …)` — так делают плавающие панели с вычисляемой высотой шапки.
    """
    if not ctx.declared_tokens:
        return
    runtime = set()
    for item in ctx.files.values():
        if item.path.suffix in {".tsx", ".ts", ".html"}:
            runtime |= set(re.findall(r"setProperty\(\s*['\"](--[a-z0-9-]+)", item.text))
    for label, file, text in (
            ("приложение", "30-APP/index.html", ctx.app.text if ctx.app else ""),
            ("сайт", "50-SITE/src/app/globals.css",
             ctx.site_css.text if ctx.site_css else "")):
        if not text:
            continue
        # все объявления переменных в файле — в любом селекторе, включая @media и :root
        local = set(re.findall(r"(--[a-z0-9-]+)\s*:", strip_comments(text)))
        local |= set(ctx.declared_tokens) | runtime
        missing: dict[str, int] = {}
        for match in re.finditer(r"var\((--[a-z0-9-]+)\s*([,)])", text):
            name, tail = match.group(1), match.group(2)
            if name in local or tail == ",":
                continue
            missing.setdefault(name, text[:match.start()].count("\n") + 1)
        for name, line in missing.items():
            add(store, rule="ds.token.undefined", area="design", severity="high",
                file=file, line=line, selector=name,
                message=f"{label}: {name} используется, но нигде не объявлен",
                evidence="подставляется пустое значение — свойство не работает",
                hint="объявить токен или добавить запасное значение в var()")


def _norm(value: str) -> str:
    return str(value or "").strip().lower().replace(" ", "")


def rule_contrast(ctx: Context, store: list[dict]) -> None:
    """Контраст фактических пар против объявленной таблицы §6 и минимума WCAG AA."""
    declared = parse_declared_contrast(ctx.design.text) if ctx.design else []
    for row in declared:
        actual = contrast(row["fg"], row["bg"])
        if actual is None:
            continue
        ok = "нет" in row["verdict"].lower()
        if abs(actual - row["expected"]) > 0.15:
            add(store, rule="ds.contrast.table-stale", area="design",
                severity="medium", file="20-DESIGN/design-system.md", line=0,
                selector=f"{row['fg_label']} на {row['bg_label']}",
                message=(f"объявлено {row['expected']}:1, фактически {actual}:1 — "
                         f"таблица замеров устарела"),
                evidence=f"{row['fg']} на {row['bg']}",
                hint="пересчитать таблицу и обновить вердикт")
        if actual < 4.5 and not ok:
            add(store, rule="a11y.contrast", area="accessibility", severity="critical",
                file="20-DESIGN/design-system.md", line=0,
                selector=f"{row['fg_label']} на {row['bg_label']}",
                message=f"пара объявлена годной, но даёт {actual}:1 < 4.5:1",
                evidence=f"{row['fg']} на {row['bg']}", hint="затемнить фон или осветлить текст")

    # фактические токены интерфейса: подписи и вторичный текст на своих поверхностях
    for label, tokens, file in (("приложение", ctx.app_tokens, "30-APP/index.html"),
                                ("сайт", ctx.site_tokens, "50-SITE/src/app/globals.css")):
        if not tokens:
            continue
        pairs = [("--fg", "--bg", 4.5, "основной текст"),
                 ("--fg-muted", "--bg-2", 4.5, "вторичный текст"),
                 ("--fg-dim", "--bg-2", 4.5, "подписи на карточке"),
                 ("--fg-dim", "--bg-3", 4.5, "подписи на hover-поверхности"),
                 ("--fg-muted", "--bg", 4.5, "вторичный текст на фоне")]
        for fg, bg, need, note in pairs:
            value = contrast(tokens.get(fg, ""), tokens.get(bg, ""))
            if value is None:
                continue
            if value < need:
                add(store, rule="a11y.contrast.token", area="accessibility",
                    severity="critical" if fg in {"--fg-dim", "--fg-muted"} else "high",
                    file=file, line=0, selector=f"{fg} на {bg}",
                    message=f"{label}: {note} даёт {value}:1 < {need}:1",
                    evidence=f"{tokens[fg]} на {tokens[bg]}",
                    hint="поднять светлоту токена до 4.5:1 (в системе --fg-dim #82828e)")


def rule_accent_fill(ctx: Context, store: list[dict]) -> None:
    """Сплошная заливка акцентом под белой подписью — только --accent-strong."""
    for label, rules, file in (("приложение", ctx.app_css_rules(), "30-APP/index.html"),
                               ("сайт", ctx.site_css_rules(), "50-SITE/src/app/globals.css")):
        for selector, body, line in rules:
            decl = declarations(body)
            background = decl.get("background") or decl.get("background-color") or ""
            if re.fullmatch(r"var\(--accent\)", background):
                color = decl.get("color", "")
                if "fff" in color or "white" in color or "var(--fg)" in color:
                    ratio = contrast(color or "#ffffff", ctx.app_tokens.get("--accent", "#7c5cff")
                                     if label == "приложение"
                                     else ctx.site_tokens.get("--accent", "#7c5cff"))
                    add(store, rule="ds.accent.fill", area="design",
                        severity="high" if (ratio or 0) < 4.5 else "medium",
                        file=file, line=line, selector=selector,
                        message=(f"{label}: заливка var(--accent) под светлой подписью"
                                 + (f" даёт {(ratio or 0)}:1" if ratio else "")),
                        evidence=f"{selector} {{ background: {background}; color: {color} }}",
                        hint="для заливки под текстом — var(--accent-strong)")


def rule_font_scale(ctx: Context, store: list[dict]) -> None:
    """Кегли вне объявленной шкалы 11/12/13/14/16/20/24/32 и текст мельче 11 px."""
    for label, file, rules in (("приложение", "30-APP/index.html", ctx.app_css_rules()),
                               ("сайт", "50-SITE/src/app/globals.css", ctx.site_css_rules())):
        for selector, body, line in rules:
            decl = declarations(body)
            size = decl.get("font-size", "")
            for value in px_values(size):
                if value < 11:
                    add(store, rule="ds.font.too-small", area="design", severity="medium",
                        file=file, line=line, selector=selector,
                        message=f"{label}: кегль {value:g}px меньше минимума 11px",
                        evidence=f"{selector} {{ font-size: {size} }}",
                        hint="поднять до 11px: мельче не читается на телефоне")
                elif int(round(value)) not in FONT_SCALE:
                    add(store, rule="ds.font.scale", area="design", severity="low",
                        file=file, line=line, selector=selector,
                        message=f"{label}: кегль {value:g}px вне шкалы {sorted(FONT_SCALE)}",
                        evidence=f"{selector} {{ font-size: {size} }}",
                        hint="округлить до ближайшей ступени шкалы")
    # инлайн-кегли в разметке приложения (шаблоны в JS тоже)
    if ctx.app:
        for match in re.finditer(r"font-size:\s*([\d.]+)px", ctx.app.text):
            value = float(match.group(1))
            if value < 11 or int(round(value)) not in FONT_SCALE:
                add(store, rule="ds.font.inline", area="design",
                    severity="medium" if value < 11 else "low",
                    file="30-APP/index.html", line=ctx.app.line_of(match.start()),
                    selector="inline style",
                    message=f"инлайн-кегль {value:g}px вне шкалы",
                    evidence=match.group(0), hint="кегль — из токенов, а не в разметке")


def rule_spacing_scale(ctx: Context, store: list[dict]) -> None:
    """Отступы вне шкалы 4/8/12/16/24/32/48 — визуальный шум и «пляшущий» ритм."""
    counter: dict[float, list[tuple[str, int, str]]] = {}
    for rules, file in ((ctx.app_css_rules(), "30-APP/index.html"),
                        (ctx.site_css_rules(), "50-SITE/src/app/globals.css")):
        for selector, body, line in rules:
            decl = declarations(body)
            for name, value in decl.items():
                if not name.startswith(("padding", "margin", "gap", "row-gap", "column-gap")):
                    continue
                for number in px_values(value):
                    if number in SPACING_SCALE or number <= 2:
                        continue
                    if number % 2 == 0 and number <= 12:
                        continue          # чётный шаг плотной сетки
                    counter.setdefault(number, []).append((selector, line, name))
    for number, rows in sorted(counter.items()):
        if len(rows) < 6:
            continue
        selector, line, name = rows[0]
        add(store, rule="ds.spacing.scale", area="design",
            severity="low" if len(rows) < 20 else "medium", file="30-APP/index.html",
            line=line, selector=selector,
            message=f"отступ {number:g}px вне шкалы {sorted(SPACING_SCALE)} — {len(rows)} раз",
            evidence=f"{name}: {number:g}px, например {selector}",
            hint="отступы — из токенов --sp-1…--sp-7")


def rule_motion(ctx: Context, store: list[dict]) -> None:
    """Движение: тайминги, `transition: all`, отсутствие reduced-motion, изменение размера на hover.

    Переходы (transition) — отклик на действие: 120–180 мс по §5, дольше 400 мс — «вязко».
    Анимации (animation) — циклы (спиннер, пульс): их длительность не отклик, поэтому меряется
    только их наличие и отключаемость.
    """
    for label, file, rules in (("приложение", "30-APP/index.html", ctx.app_css_rules()),
                               ("сайт", "50-SITE/src/app/globals.css", ctx.site_css_rules())):
        for selector, body, line in rules:
            decl = declarations(body)
            transition = decl.get("transition") or decl.get("transition-duration") or ""
            if transition:
                if re.search(r"\ball\b", transition) and "," not in transition:
                    add(store, rule="ds.motion.transition-all", area="motion", severity="low",
                        file=file, line=line, selector=selector,
                        message=f"{label}: transition: all — анимируется всё, включая раскладку",
                        evidence=f"{selector} {{ transition: {transition} }}",
                        hint="перечислять свойства явно")
                surface = bool(re.search(r"\b(transform|translate|left|right|top|bottom)\b",
                                         transition))
                window = MOTION_TARGET_SURFACE if surface else MOTION_TARGET
                for duration in durations(transition):
                    if duration < 1:
                        continue                      # «мгновенно» — намеренное отключение
                    if duration > MOTION_MAX:
                        add(store, rule="ds.motion.too-long", area="motion", severity="medium",
                            file=file, line=line, selector=selector,
                            message=f"{label}: переход {duration:g} мс — дольше {MOTION_MAX} мс, "
                                    f"интерфейс кажется вязким",
                            evidence=f"{selector} {{ transition: {transition} }}",
                            hint="120–180 мс по §5 дизайн-системы")
                    elif not (window[0] <= duration <= window[1]):
                        add(store, rule="ds.motion.timing", area="motion", severity="low",
                            file=file, line=line, selector=selector,
                            message=f"{label}: переход {duration:g} мс вне окна "
                                    f"{window[0]}–{window[1]} мс "
                                    f"({'поверхность' if surface else 'отклик'})",
                            evidence=f"{selector} {{ transition: {transition} }}",
                            hint="единый тайминг движения")
            animation = decl.get("animation") or ""
            for duration in durations(animation):
                if duration > 4000:
                    add(store, rule="ds.motion.too-long", area="motion", severity="low",
                        file=file, line=line, selector=selector,
                        message=f"{label}: цикл анимации {duration:g} мс — индикатор «замер»",
                        evidence=f"{selector} {{ animation: {animation} }}",
                        hint="индикатор прогресса — до 2 с на цикл")
            if ":hover" in selector and not re.search(r":{1,2}(after|before)\b", selector):
                for name in ("width", "height", "margin", "padding", "font-size"):
                    if decl.get(name):
                        add(store, rule="ds.motion.hover-resize", area="motion", severity="medium",
                            file=file, line=line, selector=selector,
                            message=f"{label}: hover меняет {name} — элемент «прыгает», "
                                    f"соседние сдвигаются",
                            evidence=f"{selector} {{ {name}: {decl[name]} }}",
                            hint="hover — только цвет, тень и 1px обводка")
    # reduced-motion: движение обязано отключаться системной настройкой
    for label, file, text in (("приложение", "30-APP/index.html", ctx.app.text),
                              ("сайт", "50-SITE/src/app/globals.css",
                               ctx.site_css.text if ctx.site_css else "")):
        animated = len(re.findall(r"(?:transition|animation)\s*:", text))
        has_reduce = "prefers-reduced-motion" in text
        if animated and not has_reduce:
            add(store, rule="ds.motion.reduced-missing", area="motion", severity="critical",
                file=file, line=0, selector="@media (prefers-reduced-motion: reduce)",
                message=(f"{label}: {animated} объявлений transition/animation и нет ни одного "
                         f"блока prefers-reduced-motion"),
                evidence="анимации не отключаются при системной настройке «меньше движения»",
                hint="добавить @media (prefers-reduced-motion: reduce) со сбросом движения")


def rule_inline_and_important(ctx: Context, store: list[dict]) -> None:
    """Инлайн-стили и !important: чем больше, тем меньше дизайн-система управляет видом."""
    if ctx.app:
        inline = len(re.findall(r'\sstyle="', ctx.app.text))
        if inline > 20:
            add(store, rule="ds.inline-styles", area="design", severity="medium",
                file="30-APP/index.html", line=0, selector="[style]",
                message=f"приложение: {inline} инлайн-стилей в разметке",
                evidence="вид задаётся в разметке и в шаблонах JS, а не классами",
                hint="вынести повторяющиеся стили в классы дизайн-системы")
    for file, text in (("30-APP/index.html", ctx.app.text if ctx.app else ""),
                       ("50-SITE/src/app/globals.css", ctx.site_css.text if ctx.site_css else "")):
        # `!important` в «мгновенном» режиме движения, печати и скрытии — норма;
        # считаем только перебивание каскада ради внешнего вида
        important = 0
        for line in text.splitlines():
            if "!important" not in line:
                continue
            if re.search(r"(animation|transition)-duration|animation:|transition:", line):
                continue
            if re.search(r"display:\s*none", line):
                continue
            important += 1
        if important > 3:
            add(store, rule="ds.important", area="design", severity="low", file=file, line=0,
                selector="!important", message=f"!important встречается {important} раз",
                evidence="перебивание каскада вместо порядка правил",
                hint="переписать специфичность")


# ------------------------------------------------------------------ правила: доступность
def _tags(text: str, name: str) -> list[tuple[re.Match, str, str]]:
    """Открывающие теги с атрибутами и телом (для парных тегов)."""
    rows = []
    pattern = re.compile(rf"<{name}([^>]*)>(.*?)</{name}>", re.S | re.I)
    for match in pattern.finditer(text):
        rows.append((match, match.group(1), match.group(2)))
    return rows


def _attr(attrs: str, name: str) -> str | None:
    match = re.search(rf'{name}\s*=\s*"([^"]*)"', attrs or "", re.I)
    if match:
        return match.group(1)
    match = re.search(rf"{name}\s*=\s*'([^']*)'", attrs or "", re.I)
    return match.group(1) if match else None


def _plain(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html or "")
    text = re.sub(r"\$\{[^}]*\}", "x", text)       # шаблонные вставки считаем текстом
    return " ".join(text.split())


def rule_button_names(ctx: Context, store: list[dict]) -> None:
    """У кнопки должно быть доступное имя: текст, aria-label или title."""
    for file, text in (("30-APP/index.html", ctx.app.text if ctx.app else ""),
                       *[(name, item.text) for name, item in ctx.files.items()
                         if name.startswith(("50-SITE/src/app", "50-SITE/src/components"))]):
        for match, attrs, body in _tags(text, "button"):
            label = _plain(body)
            aria = _attr(attrs, "aria-label") or _attr(attrs, "title")
            if not label and not aria:
                add(store, rule="a11y.button-name", area="accessibility", severity="critical",
                    file=file, line=text[:match.start()].count("\n") + 1,
                    selector=attrs.strip()[:80],
                    message="кнопка без доступного имени (иконка без подписи)",
                    evidence=" ".join(_plain(attrs).split())[:120],
                    hint="aria-label с глаголом: «Закрыть», «Свернуть меню»")


def rule_svg_hidden(ctx: Context, store: list[dict]) -> None:
    """Декоративные inline-SVG прячутся от скринридера; иначе он читает пустые фигуры."""
    for file, text in (("30-APP/index.html", ctx.app.text if ctx.app else ""),
                       ("20-DESIGN", "")):
        if not text:
            continue
        total = len(re.findall(r"<svg\b", text))
        hidden = len(re.findall(r"<svg[^>]*aria-hidden", text))
        if total and hidden == 0:
            add(store, rule="a11y.svg-hidden", area="accessibility", severity="high",
                file=file, line=0, selector="svg",
                message=f"{total} inline-SVG и ни одного aria-hidden",
                evidence="иконки читаются скринридером как пустые группы",
                hint='добавить aria-hidden="true" всем декоративным иконкам')


def rule_labels(ctx: Context, store: list[dict]) -> None:
    """<label> без for и без вложенного поля — подпись не связана с контролом."""
    text = ctx.app.text if ctx.app else ""
    for match, attrs, body in _tags(text, "label"):
        has_for = bool(_attr(attrs, "for"))
        inner = re.search(r"<(input|textarea|select)\b", body, re.I) or \
            re.search(r"<(input|textarea|select)\b", text[match.end():match.end() + 200], re.I)
        if not has_for and not inner:
            add(store, rule="a11y.label-for", area="accessibility", severity="high",
                file="30-APP/index.html", line=text[:match.start()].count("\n") + 1,
                selector="<label>", message="подпись не связана с полем (нет for)",
                evidence=_plain(body)[:80], hint="связать через for/id или вложить поле в label")
    for match in re.finditer(r"<input([^>]*)>", text):
        attrs = match.group(1)
        if (_attr(attrs, "type") or "text") in {"hidden", "submit", "button"}:
            continue
        if not (_attr(attrs, "aria-label") or _attr(attrs, "aria-labelledby")
                or _attr(attrs, "placeholder") or _attr(attrs, "id")):
            add(store, rule="a11y.input-label", area="accessibility", severity="high",
                file="30-APP/index.html", line=text[:match.start()].count("\n") + 1,
                selector=attrs.strip()[:80], message="поле без подписи и без placeholder",
                evidence=attrs.strip()[:120], hint="добавить label for=… или aria-label")


def rule_div_click(ctx: Context, store: list[dict]) -> None:
    """Кликабельный <div> без роли и клавиатуры — действие недоступно с Tab."""
    text = ctx.app.text if ctx.app else ""
    for match in re.finditer(r'<(div|span)([^>]*data-action="([^"]+)"[^>]*)>', text):
        attrs, action = match.group(2), match.group(3)
        if 'role="button"' in attrs or "tabindex" in attrs.lower():
            continue
        add(store, rule="a11y.div-click", area="accessibility", severity="high",
            file="30-APP/index.html", line=text[:match.start()].count("\n") + 1,
            selector=f"{match.group(1)}[data-action={action}]",
            message=f"действие «{action}» висит на {match.group(1)} без role/tabindex",
            evidence=attrs.strip()[:120],
            hint="<button> вместо div или role=button + tabindex=0 + Enter/Space")


def rule_aria_state(ctx: Context, store: list[dict]) -> None:
    """Панели и меню: aria-expanded/aria-controls и aria-modal для модальных окон."""
    text = ctx.app.text if ctx.app else ""
    popups = len(re.findall(r'aria-haspopup="true"', text))
    expanded = len(re.findall(r"aria-expanded", text))
    if popups and not expanded:
        add(store, rule="a11y.aria-expanded", area="accessibility", severity="high",
            file="30-APP/index.html", line=0, selector='[aria-haspopup="true"]',
            message=f"{popups} кнопок с aria-haspopup и ни одного aria-expanded",
            evidence="состояние «открыто/закрыто» не передаётся скринридеру",
            hint="переключать aria-expanded при открытии панели, aria-controls → id панели")
    modals = len(re.findall(r'class="overlay"', text))
    dialog_roles = len(re.findall(r'role="dialog"', text))
    aria_modal = len(re.findall(r"aria-modal", text))
    if modals and not (dialog_roles and aria_modal):
        add(store, rule="a11y.dialog", area="accessibility", severity="high",
            file="30-APP/index.html", line=0, selector=".overlay",
            message=f"{modals} модальных окон без role=dialog/aria-modal",
            evidence=f"role=dialog: {dialog_roles}, aria-modal: {aria_modal}",
            hint="role=dialog + aria-modal=true + aria-labelledby на заголовок")
    if "aria-live" not in text and "role=\"status\"" not in text:
        add(store, rule="a11y.live-region", area="accessibility", severity="high",
            file="30-APP/index.html", line=0, selector="#toasts",
            message="тосты объявляются визуально, но не скринридером (нет aria-live)",
            evidence='контейнер .toasts без role="status"/aria-live="polite"',
            hint='role="status" aria-live="polite" на контейнер тостов')


def rule_focus_and_skip(ctx: Context, store: list[dict]) -> None:
    """Фокус виден, ловушка в модальных окнах, ссылка «к содержимому»."""
    if not ctx.app:
        return
    text, css = ctx.app.text, ctx.app_css
    if "focus-visible" not in css and ":focus" not in css:
        add(store, rule="a11y.focus-missing", area="accessibility", severity="critical",
            file="30-APP/index.html", line=0, selector=":focus-visible",
            message="в приложении нет ни одного правила фокуса",
            evidence=f"outline:none встречается {len(re.findall('outline:none', css))} раз",
            hint=":focus-visible { outline: 2px solid var(--accent-soft); outline-offset: 2px }")
    if "outline:none" in css.replace(" ", "") and "focus-visible" not in css:
        add(store, rule="a11y.outline-none", area="accessibility", severity="critical",
            file="30-APP/index.html", line=0, selector="outline: none",
            message="фокус отключён без замены — клавиатура «слепая»",
            evidence="outline:none без :focus-visible", hint="заменить на видимый :focus-visible")
    layers = len(re.findall(r'class="(?:overlay|pop|drawer|cmdk)', text))
    if layers and "Escape" not in text:
        add(store, rule="a11y.escape", area="accessibility", severity="high",
            file="30-APP/index.html", line=0, selector="keydown",
            message=f"Escape не закрывает слои: {layers} панелей и окон",
            evidence="в обработчиках клавиатуры нет 'Escape'",
            hint="keydown Escape → закрыть верхний слой и вернуть фокус")
    if "skip-link" not in text and "К содержимому" not in text:
        add(store, rule="a11y.skip-link", area="accessibility", severity="medium",
            file="30-APP/index.html", line=0, selector=".skip-link",
            message="нет ссылки «Перейти к содержимому»: 16 пунктов меню до контента",
            evidence="первый Tab попадает в рельсу навигации",
            hint="skip-link первым элементом body, видимый по фокусу")


def _resolve(value: str, tokens: dict[str, str]) -> str:
    """Подставить значение токена: `var(--tap)` → `32px`."""
    def swap(match: re.Match) -> str:
        return tokens.get(match.group(1), "")
    return re.sub(r"var\((--[a-z0-9-]+)\)", swap, value or "")


def rule_tap_targets(ctx: Context, store: list[dict]) -> None:
    """Зона нажатия: 32×32 в приложении и 44×44 на телефоне (§6).

    Высота, объявленная в ЛЮБОМ правиле для того же класса (например, общий `.chip`
    задаёт min-height, а `.prev-tabs .chip` — только padding), считается достаточной:
    каскад суммирует их, и статическая проверка не должна ругаться на уточняющее правило.
    """
    declared: dict[str, float] = {}
    for selector, body, _line in ctx.app_css_rules():
        decl = declarations(body)
        raw = decl.get("min-height", "") or decl.get("height", "")
        height = px_values(raw) or px_values(_resolve(raw, ctx.app_tokens))
        if not height:
            continue
        for cls in re.findall(r"\.([a-z0-9-]+)", selector):
            declared[cls] = max(declared.get(cls, 0), height[0])
    targets = {"btn": 32, "icon-btn": 32, "pi": 32, "chip": 32, "side-item": 32,
               "btn.sm": 32, "add": 24, "ck": 24}
    for selector, body, line in ctx.app_css_rules():
        for name, need in targets.items():
            base = selector.split(":")[0].split(".")[-1].strip()
            if name.split(".")[-1] != base:
                continue
            decl = declarations(body)
            if declared.get(base, 0) >= need:
                continue                      # высота обеспечена общим правилом класса
            height = px_values(_resolve(decl.get("height", ""), ctx.app_tokens))
            min_height = px_values(_resolve(decl.get("min-height", ""), ctx.app_tokens))
            padding = px_values(decl.get("padding", "") or decl.get("padding-y", ""))
            font = px_values(decl.get("font-size", "")) or [13]
            measured = (height or min_height or [0])[0]
            if not measured and padding:
                measured = (padding[0] * 2 if len(padding) == 1 else padding[0] + padding[-1]) \
                    + font[0] * 1.45
            if measured and measured + 0.6 < need:
                add(store, rule="a11y.tap-target", area="accessibility", severity="high",
                    file="30-APP/index.html", line=line, selector=selector,
                    message=f"зона нажатия {measured:.0f}px < {need}px ({selector})",
                    evidence=f"{selector} {{ {', '.join(f'{k}: {v}' for k, v in decl.items())} }}",
                    hint="min-height 32px, на телефоне 44px")


def rule_headings_landmarks(ctx: Context, store: list[dict]) -> None:
    """Заголовки и ориентиры: h1 на экран, без скачков уровней, main/nav подписаны."""
    text = ctx.app.text if ctx.app else ""
    h1 = len(re.findall(r"<h1\b", text))
    if h1 == 0:
        add(store, rule="a11y.h1", area="accessibility", severity="high",
            file="30-APP/index.html", line=0, selector="h1",
            message="в приложении нет ни одного h1: экран без заголовка для скринридера",
            evidence="заголовки только в модальных окнах (h2/h3)",
            hint="заголовок текущего экрана — h1, секции — h2")
    for match, attrs, body in _tags(text, "h3"):
        if not re.search(r"<h2\b", text[:match.start()][-3000:]):
            add(store, rule="a11y.heading-order", area="accessibility", severity="medium",
                file="30-APP/index.html", line=text[:match.start()].count("\n") + 1,
                selector="h3", message="уровень h3 без предшествующего h2",
                evidence=_plain(body)[:60], hint="заголовки по порядку уровней")
            break
    if "<nav" not in text and 'role="navigation"' not in text:
        add(store, rule="a11y.landmark-nav", area="accessibility", severity="medium",
            file="30-APP/index.html", line=0, selector="nav",
            message="навигация без <nav>: скринридер не может перейти к ней по ориентиру",
            evidence="rail и sidebar — <aside> без aria-label",
            hint="<nav aria-label=\"Разделы\"> вокруг rail/sidebar")
    for match in re.finditer(r"<aside([^>]*)>", text):
        attrs = match.group(1)
        if not _attr(attrs, "aria-label") and not _attr(attrs, "aria-labelledby"):
            add(store, rule="a11y.landmark-label", area="accessibility", severity="low",
                file="30-APP/index.html", line=text[:match.start()].count("\n") + 1,
                selector="<aside>", message="aside без подписи: непонятно, какой ориентир",
                evidence=attrs.strip()[:80], hint='aria-label="Рельса модулей", "Боковая панель"')


def rule_zoom_lang(ctx: Context, store: list[dict]) -> None:
    for file, item in ctx.files.items():
        if not file.endswith((".html", ".tsx")):
            continue
        text = item.text
        if "user-scalable=no" in text or "maximum-scale=1" in text:
            add(store, rule="a11y.zoom-blocked", area="accessibility", severity="critical",
                file=file, line=0, selector="meta viewport",
                message="масштабирование запрещено: пользователь не может увеличить текст",
                evidence="user-scalable=no / maximum-scale=1", hint="убрать ограничение")
        if file.endswith(".html") and "<html" in text:
            tag = re.search(r"<html[^>]*>", text, re.I)
            if not tag or "lang=" in tag.group(0):
                continue
            add(store, rule="a11y.lang", area="accessibility", severity="high", file=file,
                line=1, selector="<html>", message="у <html> нет lang",
                evidence="язык документа не объявлен", hint='lang="ru"')


# ------------------------------------------------------------------ правила: функции и структура
def rule_actions(ctx: Context, store: list[dict]) -> None:
    """Кнопка без обработчика — «мёртвая» кнопка; обработчик без кнопки — мёртвый код."""
    text = ctx.app.text if ctx.app else ""
    actions = set(re.findall(r'data-action="([a-z0-9-]+)"', text))
    actions |= set(re.findall(r"data-action=\\?['\"]([a-z0-9-]+)", text))
    cases = set(re.findall(r"case '([a-z0-9-]+)'", text))
    changes = set(re.findall(r'\$\$?\([^)]*data-action[^)]*\)', text))
    handled = cases | changes
    for action in sorted(actions - cases):
        if action in {"filter-status", "member-role", "nav-view", "select-brand"}:
            continue                      # обрабатываются слушателем change
        add(store, rule="fn.action.dead", area="functions", severity="high",
            file="30-APP/index.html", line=_line_of_action(text, action),
            selector=f'[data-action="{action}"]',
            message=f"у действия «{action}» нет обработчика: кнопка ничего не делает",
            evidence="нет ни case, ни слушателя", hint="добавить обработчик или убрать кнопку")
    unused = sorted(cases - actions - handled)
    if unused:
        add(store, rule="fn.action.orphan", area="functions", severity="low",
            file="30-APP/index.html", line=0, selector="switch(actions)",
            message=f"{len(unused)} обработчиков без кнопок: {', '.join(unused[:6])}",
            evidence="обработчик есть, а элемента, который его вызывает, нет",
            hint="удалить мёртвый код или вернуть кнопку")


def _line_of_action(text: str, action: str) -> int:
    match = re.search(rf'data-action="?{re.escape(action)}', text)
    return text[:match.start()].count("\n") + 1 if match else 0


def rule_module_matrix(ctx: Context, store: list[dict]) -> None:
    """Каждый модуль навигации обязан иметь экран; экран без модуля недостижим."""
    text = ctx.app.text if ctx.app else ""
    nav = re.search(r"const NAV = \[(.*?)\];", text, re.S)
    views = re.search(r"const VIEWS = \{(.*?)\};", text, re.S)
    if not (nav and views):
        return
    modules = set(re.findall(r"id:'([a-z-]+)'", nav.group(1)))
    screens = set(re.findall(r"([a-z-]+):\s*render", views.group(1)))
    for module in sorted(modules - screens):
        add(store, rule="fn.view.missing", area="functions", severity="critical",
            file="30-APP/index.html", line=text[:nav.start()].count("\n") + 1,
            selector=f"NAV[{module}]",
            message=f"модуль «{module}» есть в навигации, но экрана нет — пустой клик",
            evidence="нет записи в VIEWS", hint="добавить renderX или убрать пункт")
    for screen in sorted(screens - modules):
        add(store, rule="fn.view.orphan", area="functions", severity="medium",
            file="30-APP/index.html", line=0, selector=f"VIEWS[{screen}]",
            message=f"экран «{screen}» есть, но недостижим из навигации",
            evidence="нет пункта в NAV", hint="добавить пункт или удалить экран")


def rule_stubs(ctx: Context, store: list[dict]) -> None:
    """Заглушки и технические маркеры в интерфейсе: видимая честность вместо пустоты."""
    if not ctx.app:
        return
    for pattern, hint in ((r"TODO|FIXME|XXX", "технический маркер в коде интерфейса"),
                          (r"alert\(", "alert() вместо тоста"),
                          (r"lorem ipsum", "рыбный текст в интерфейсе"),
                          (r"dummy|placeholder-data", "фиктивные данные в интерфейсе")):
        for match in re.finditer(pattern, ctx.app.text, re.I):
            add(store, rule="fn.stub", area="functions", severity="low",
                file="30-APP/index.html", line=ctx.app.line_of(match.start()),
                selector=pattern, message=hint, evidence=match.group(0),
                hint="заменить на состояние «данных нет» с причиной")
    for match in re.finditer(r"function\s+(\w+)\s*\([^)]*\)\s*\{\s*\}", ctx.app.text):
        add(store, rule="fn.empty-handler", area="functions", severity="medium",
            file="30-APP/index.html", line=ctx.app.line_of(match.start()),
            selector=match.group(1), message=f"пустая функция {match.group(1)}()",
            evidence="ничего не делает", hint="реализовать или удалить")


def rule_destructive(ctx: Context, store: list[dict]) -> None:
    """Необратимые действия должны спрашивать подтверждение."""
    text = ctx.app.text if ctx.app else ""
    if "confirm(" in text:
        return
    for match in re.finditer(r">\s*(Удалить|Отключить|Отозвать|Сбросить|Удалить всё)[^<]*<",
                             text):
        add(store, rule="fn.destructive-guard", area="functions", severity="medium",
            file="30-APP/index.html", line=text[:match.start()].count("\n") + 1,
            selector="button", message=f"необратимое действие «{match.group(1)}» без подтверждения",
            evidence="нет confirm() и нет модального шага",
            hint="подтверждение с последствием: «Пост исчезнет из канала»")


def rule_copy_tone(ctx: Context, store: list[dict]) -> None:
    """Микрокопирайт: ошибка объясняет причину и путь, а не извиняется."""
    if not ctx.app:
        return
    for pattern, message in ((r"Упс", "«Упс» вместо объяснения"),
                             (r"Что-то пошло не так", "сообщение без причины"),
                             (r"Ошибка!", "ошибка без причины и пути решения"),
                             (r"Произошла ошибка", "ошибка без причины и пути решения")):
        for match in re.finditer(pattern, ctx.app.text, re.I):
            add(store, rule="fn.copy.tone", area="functions", severity="medium",
                file="30-APP/index.html", line=ctx.app.line_of(match.start()),
                selector="copy", message=message, evidence=match.group(0),
                hint="«Публикация не ушла: канал отклонил формат. Предлагаем альбом из 3 фото»")


def rule_state_persistence(ctx: Context, store: list[dict]) -> None:
    """Что переживёт перезагрузку: настройки, фильтры, свёрнутость панелей."""
    if not ctx.app:
        return
    if "localStorage" not in ctx.app.text and "sessionStorage" not in ctx.app.text:
        add(store, rule="fn.state.persistence", area="functions", severity="medium",
            file="30-APP/index.html", line=0, selector="state",
            message="состояние интерфейса не сохраняется: после F5 фильтры, свёрнутые панели "
                    "и выбранный бренд теряются",
            evidence="нет localStorage/sessionStorage",
            hint="хранить выбор бренда, фильтры и панели (без ПДн)")


# ------------------------------------------------------------------ правила: размещение
def rule_layout_placement(ctx: Context, store: list[dict]) -> None:
    """Размещение элементов: главное действие, скролл-области, таблицы, панели."""
    text = ctx.app.text if ctx.app else ""
    # таблицы без обёртки с прокруткой
    tables = len(re.findall(r"<table", text))
    wrapped = len(re.findall(r'class="tbl-wrap"', text))
    if tables and wrapped < tables:
        add(store, rule="lay.table-wrap", area="layout", severity="high",
            file="30-APP/index.html", line=0, selector="table",
            message=f"{tables} таблиц и только {wrapped} обёрток .tbl-wrap",
            evidence="таблица шире колонки уезжает под обрез",
            hint="обёртка с overflow-x:auto и tabindex=0")
    # цели нажатия в шаблонах с инлайновым padding меньше нормы
    for match in re.finditer(r'class="btn sm ghost"[^>]*style="[^"]*padding:\s*2px', text):
        add(store, rule="lay.tap-inline", area="layout", severity="medium",
            file="30-APP/index.html", line=text[:match.start()].count("\n") + 1,
            selector=".btn.sm.ghost",
            message="инлайновый padding 2px делает кнопку ниже нормы нажатия",
            evidence=match.group(0)[:120], hint="не сжимать высоту кнопки инлайном")
    # главное действие экрана: primary должен быть последним в строке действий
    rows = re.findall(r'<div class="actions">(.*?)</div>', text, re.S)
    for index, row in enumerate(rows):
        if "btn primary" in row and row.find("btn primary") < row.find("btn"):
            add(store, rule="lay.primary-first", area="layout", severity="low",
                file="30-APP/index.html", line=0, selector=f".actions[{index}]",
                message="главное действие стоит первым в строке — оно читается как второстепенное",
                evidence=_plain(row)[:100],
                hint="primary — последним справа: взгляд идёт слева направо")
    # рельса/сайдбар шириной не по системе
    declared_rail = ctx.declared_tokens.get("--rail")
    actual_rail = ctx.app_tokens.get("--rail")
    if declared_rail and actual_rail and _norm(declared_rail) != _norm(actual_rail):
        add(store, rule="lay.rail-width", area="layout", severity="low",
            file="30-APP/index.html", line=0, selector="--rail",
            message=f"рельса {actual_rail}, в системе {declared_rail}",
            evidence=f"--rail: {actual_rail} ≠ {declared_rail}",
            hint="выровнять по дизайн-системе")


# ------------------------------------------------------------------ правила: артефакты
def rule_artifacts(ctx: Context, store: list[dict]) -> None:
    """Собранные статьи и медиа-страницы: их вид тоже часть системы."""
    for name, item in ctx.files.items():
        if not name.startswith("20-DESIGN/articles/"):
            continue
        text = item.text
        if '<img' in text and 'alt="' not in text:
            add(store, rule="art.img-alt", area="artifacts", severity="high", file=name,
                line=0, selector="img", message="иллюстрация без alt",
                evidence="<img> без alt", hint="alt описывает смысл, а не файл")
        external = re.findall(r'<(?:link|script)[^>]+(?:href|src)="(https?://[^"]+)"', text)
        external = [url for url in external if not re.search(
            r'<(?:link)[^>]*rel="(?:canonical|alternate|dns-prefetch|preconnect)"', text)]
        external = [url for url in external if "posiz.ru" not in url]
        if external:
            add(store, rule="art.external-assets", area="artifacts", severity="medium",
                file=name, line=0, selector="link/src",
                message=f"{len(external)} внешних ресурсов: превью без сети останется без стилей",
                evidence=", ".join(sorted(set(external))[:3]),
                hint="внешние ресурсы — только по явному требованию")
        tokens = root_tokens(text)
        if tokens:
            for fg, bg, need in (("--fg", "--bg", 4.5), ("--fg-muted", "--bg-2", 4.5),
                                 ("--fg-dim", "--bg-2", 4.5)):
                value = contrast(tokens.get(fg, ""), tokens.get(bg, ""))
                if value is not None and value < need:
                    add(store, rule="art.contrast", area="artifacts", severity="high",
                        file=name, line=0, selector=f"{fg} на {bg}",
                        message=f"контраст {value}:1 < {need}:1 в собранной странице",
                        evidence=f"{tokens.get(fg)} на {tokens.get(bg)}",
                        hint="правится в рендерере статьи, а не в готовом HTML")


RULES = (
    rule_token_drift, rule_tokens_undefined, rule_contrast, rule_accent_fill, rule_font_scale, rule_spacing_scale,
    rule_motion, rule_inline_and_important,
    rule_button_names, rule_svg_hidden, rule_labels, rule_div_click, rule_aria_state,
    rule_focus_and_skip, rule_tap_targets, rule_headings_landmarks, rule_zoom_lang,
    rule_actions, rule_module_matrix, rule_stubs, rule_destructive, rule_copy_tone,
    rule_state_persistence,
    rule_layout_placement, rule_artifacts,
)


def audit(root: Path | None = None) -> dict[str, Any]:
    """Полный прогон: контекст → правила → сводка."""
    root = Path(root or REPO)
    files = load(root)
    ctx = Context(files)
    store: list[dict] = []
    for rule in RULES:
        try:
            rule(ctx, store)
        except Exception as exc:                     # noqa: BLE001 — аудит не должен падать
            add(store, rule="audit.crash", area="meta", severity="info",
                file=Path(rule.__name__).name, line=0, selector="",
                message=f"правило {rule.__name__} упало: {exc.__class__.__name__}: {exc}")
    store.sort(key=lambda row: (SEVERITIES.index(row["severity"]), row["rule"], row["file"],
                                row["line"]))
    summary = {severity: sum(1 for row in store if row["severity"] == severity)
               for severity in SEVERITIES}
    areas = {area: sum(1 for row in store if row["area"] == area)
             for area in sorted({row["area"] for row in store})}
    return {"root": str(root), "files": sorted(files), "findings": store, "summary": summary,
            "areas": areas, "rules": len(RULES),
            "total": len(store),
            "critical": summary["critical"]}


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Аудит визуального слоя Posiz")
    parser.add_argument("--root", default=str(REPO))
    parser.add_argument("--json", default="")
    parser.add_argument("--html", default="")
    parser.add_argument("--measured", default="", help="JSON замеров в браузере (measure-app.mjs)")
    parser.add_argument("--before", default="", help="JSON замеров «до» для колонки сравнения")
    parser.add_argument("--fail-on", default="", choices=["", "critical", "high"])
    args = parser.parse_args(argv)
    report = audit(Path(args.root))
    if args.json:
        Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
    if args.html:
        from .ui_audit_report import render_report

        measured = None
        before = None
        measured_path = Path(args.measured) if args.measured else None
        before_path = Path(args.before) if args.before else None
        if measured_path and measured_path.is_file():
            measured = json.loads(measured_path.read_text(encoding="utf-8"))
        if before_path and before_path.is_file():
            before = json.loads(before_path.read_text(encoding="utf-8"))
        Path(args.html).write_text(render_report(report, measured, before), encoding="utf-8")
    print(f"Правил: {report['rules']} · находок: {report['total']} · "
          f"critical {report['summary']['critical']} · high {report['summary']['high']} · "
          f"medium {report['summary']['medium']} · low {report['summary']['low']}")
    for area, count in report["areas"].items():
        print(f"  {area:14} {count}")
    for row in report["findings"][:40]:
        print(f"  [{row['severity'][:4]}] {row['rule']:28} {row['file']}:{row['line']} — "
              f"{row['message'][:90]}")
    if args.fail_on == "critical" and report["summary"]["critical"]:
        return 1
    if args.fail_on == "high" and (report["summary"]["critical"] or report["summary"]["high"]):
        return 1
    return 0


if __name__ == "__main__":                                  # pragma: no cover
    raise SystemExit(main())
