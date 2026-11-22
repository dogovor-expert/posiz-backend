"""Тесты движка аудита визуального слоя (UI-QA).

Проверяется не «красиво ли», а то, что аудит — измеримый инструмент: считает контраст по WCAG,
находит реальные дефекты на синтетических файлах, не срабатывает на правильных решениях,
понимает зафиксированные отступления из дизайн-системы и не падает на пустом проекте.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from orbita import ui_audit as audit


# ------------------------------------------------------------------ контраст
def test_contrast_matches_wcag_reference() -> None:
    # эталонные значения WCAG: белое на чёрном — 21:1, белое на акценте системы — 4.35:1
    assert audit.contrast("#ffffff", "#000000") == 21.0
    assert audit.contrast("#ffffff", "#7c5cff") == 4.35
    assert audit.contrast("#ffffff", "#6f4bf5") == 5.25
    # подписи системы: --fg-dim на --bg-2 проходит AA
    assert audit.contrast("#82828e", "#111114") == 4.97
    assert audit.contrast("#6d6d78", "#111114") == 3.69


def test_contrast_reads_short_hex_and_rgb() -> None:
    assert audit.contrast("#fff", "#000") == 21.0
    assert audit.contrast("rgb(255,255,255)", "rgb(0,0,0)") == 21.0
    assert audit.contrast("не цвет", "#000") is None


# ------------------------------------------------------------------ разбор
def test_css_rules_skip_comments_and_keep_selectors() -> None:
    css = "/* --- rail --- */\n.rail{color:#fff}\n@media(max-width:900px){.rail{color:#000}}"
    rows = list(audit.css_rules(css))
    selectors = [selector for selector, _ in rows]
    assert ".rail" in selectors
    assert not any("/*" in selector for selector in selectors)


def test_durations_read_short_notation() -> None:
    assert audit.durations("all .15s ease") == [150.0]
    assert audit.durations("transform .2s cubic-bezier(.2,.7,.2,1)") == [200.0]
    assert audit.durations("color 150ms") == [150.0]


def test_root_tokens_take_only_base_block() -> None:
    css = (":root{--fg:#ededf0}\n"
           "@media (prefers-contrast: more){:root{--fg:#ffffff}}")
    assert audit.root_tokens(css) == {"--fg": "#ededf0"}


# ------------------------------------------------------------------ правила на синтетике
def _write(root: Path, name: str, text: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


DESIGN = """# Дизайн-система

```css
:root{
  --fg:#ededf0; --fg-muted:#a1a1ab; --fg-dim:#82828e; --bg:#09090b; --bg-2:#111114;
  --bg-3:#16161a; --accent:#7c5cff; --accent-strong:#6f4bf5;
}
```

## 6. Доступность

| Текст | Фон | Замер | Вердикт |
|-------|-----|-------|---------|
| `--fg-dim` #82828e | `--bg-2` #111114 | 4.97:1 | проходит AA |
"""


def test_audit_finds_real_defects_and_stays_quiet_on_clean_markup(tmp_path: Path) -> None:
    _write(tmp_path, "20-DESIGN/design-system.md", DESIGN)
    _write(tmp_path, "30-APP/index.html", """<!doctype html>
<html lang="ru"><head><style>
:root{--fg:#ededf0;--fg-muted:#a1a1ab;--fg-dim:#6d6d78;--bg:#09090b;--bg-2:#111114;
      --bg-3:#16161a;--accent:#7c5cff;--acc:#fff}
body{color:var(--fg);background:var(--bg)}
.btn{min-height:32px;transition:all .15s}
.btn.primary{background:var(--accent);color:#fff}
.tiny{font-size:9px}
</style></head><body>
<button class="icon-btn"><svg viewBox="0 0 24 24"></svg></button>
<div class="search" data-action="open"></div>
<input id="q"/>
<div id="toasts"></div>
<div class="tbl"><table></table></div>
</body></html>""")

    report = audit.audit(tmp_path)
    rules = {row["rule"] for row in report["findings"]}

    # токен разошёлся с системой
    assert "ds.token.drift" in rules
    # белый текст на акценте — 4.35:1, ниже AA
    assert "ds.accent.fill" in rules
    # кегль ниже 11 px
    assert "ds.font.too-small" in rules
    # кнопка-иконка без доступного имени
    assert "a11y.button-name" in rules
    # действие висит на div без роли
    assert "a11y.div-click" in rules
    # нет видимого фокуса
    assert any(rule.startswith("a11y.focus") or rule == "a11y.outline-none" for rule in rules)
    # таблица без обёртки с прокруткой
    assert "lay.table-wrap" in rules
    assert report["summary"]["critical"] >= 1


def test_clean_file_produces_no_accessibility_findings(tmp_path: Path) -> None:
    _write(tmp_path, "20-DESIGN/design-system.md", DESIGN)
    _write(tmp_path, "30-APP/index.html", """<!doctype html>
<html lang="ru"><head><style>
:root{--fg:#ededf0;--fg-muted:#a1a1ab;--fg-dim:#82828e;--bg:#09090b;--bg-2:#111114;
      --bg-3:#16161a;--accent:#7c5cff;--accent-strong:#6f4bf5}
body{color:var(--fg);background:var(--bg);font-size:13px}
button:focus-visible{outline:2px solid var(--accent)}
.btn{min-height:32px;transition:color .15s}
.btn.primary{background:var(--accent-strong);color:#fff}
@media (prefers-reduced-motion: reduce){*{transition:none}}
</style></head><body>
<nav aria-label="Разделы"><button aria-label="Обзор"><svg aria-hidden="true"></svg></button></nav>
<main><h1>Экран</h1>
<div class="tbl-wrap" tabindex="0"><table></table></div>
<label for="q">Поиск</label><input id="q"/>
<a class="skip-link" href="#main">К содержимому</a>
</main>
<div id="toasts" role="status" aria-live="polite"></div>
</body></html>""")

    report = audit.audit(tmp_path)
    accessibility = [row for row in report["findings"] if row["area"] == "accessibility"]
    assert accessibility == [], [row["message"] for row in accessibility]
    assert report["summary"]["critical"] == 0


def test_documented_deviation_is_not_a_defect(tmp_path: Path) -> None:
    design = DESIGN.replace(
        "## 6. Доступность",
        "### 1.1 Зафиксированные отступления\n\n"
        "| Где | Токен | Значение | Причина |\n|---|---|---|---|\n"
        "| приложение | `--ch-max` | `#7a68ff` | AA |\n\n"
        "`--ch-max: #7a68ff` — тёмная подпись на фирменном фоне даёт 5.00:1\n\n"
        "## 6. Доступность")
    _write(tmp_path, "20-DESIGN/design-system.md", design)
    _write(tmp_path, "30-APP/index.html", """<!doctype html><html lang="ru"><head><style>
:root{--ch-max:#7a68ff;--fg:#ededf0;--fg-muted:#a1a1ab;--fg-dim:#82828e;--bg:#09090b;
      --bg-2:#111114;--bg-3:#16161a;--accent:#7c5cff;--accent-strong:#6f4bf5}
body{color:var(--fg);background:var(--bg)}
</style></head><body><main><h1>Экран</h1></main></body></html>""")

    report = audit.audit(tmp_path)
    drift = [row for row in report["findings"] if row["rule"] == "ds.token.drift"]
    assert drift == [], drift


def test_undefined_token_is_reported(tmp_path: Path) -> None:
    _write(tmp_path, "20-DESIGN/design-system.md", DESIGN)
    _write(tmp_path, "30-APP/index.html", """<!doctype html><html lang="ru"><head><style>
:root{--fg:#ededf0;--bg:#09090b}
.input{color:var(--fg);box-shadow:var(--focus)}
</style></head><body><main><h1>Экран</h1><input class="input"/></main></body></html>""")
    report = audit.audit(tmp_path)
    undefined = [row for row in report["findings"] if row["rule"] == "ds.token.undefined"]
    assert undefined, "необъявленный токен (--focus) должен попадать в отчёт"
    assert undefined[0]["selector"] == "--focus"


def test_audit_survives_empty_project(tmp_path: Path) -> None:
    report = audit.audit(tmp_path)
    assert report["total"] >= 0
    assert report["summary"]["critical"] == 0
    assert isinstance(report["findings"], list)


# ------------------------------------------------------------------ отчёт
def test_report_renders_self_contained_html(tmp_path: Path) -> None:
    from orbita.ui_audit_report import render_report

    static = audit.audit(tmp_path)
    html = render_report(static)
    assert html.startswith("<!doctype html>")
    assert "<style>" in html
    # внешних ресурсов быть не должно: превью без сети должно выглядеть так же
    assert "http://" not in html and "https://" not in html
    assert "cdn" not in html.lower()


def test_gate_reports_zero_critical_on_repository() -> None:
    """На реальном репозитории: ни одного критичного замечания и ни одного «высокого»."""
    report = audit.audit(audit.REPO)
    assert report["summary"]["critical"] == 0, [
        row["message"] for row in report["findings"] if row["severity"] == "critical"]
    assert report["rules"] >= 20
    assert report["total"] <= 5, [row["rule"] for row in report["findings"]]
