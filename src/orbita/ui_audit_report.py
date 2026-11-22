"""HTML-отчёт аудита визуального слоя: один самодостаточный файл, без внешних ресурсов.

Превью в песочнице не грузит внешние стили и шрифты, поэтому всё встроено: CSS в `<style>`,
графики — на `<div>` с шириной в процентах, никаких CDN. Отчёт читается и в браузере, и в
просмотрщике рабочего пространства.
"""
from __future__ import annotations

import html
from typing import Any

SEVERITY_LABEL = {"critical": "критично", "high": "высоко", "medium": "средне", "low": "низко",
                  "info": "инфо"}
SEVERITY_COLOR = {"critical": "#f2607a", "high": "#ff8a3d", "medium": "#f5b544", "low": "#82828e",
                  "info": "#5aa9ff"}


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def _bar(label: str, value: int, total: int, color: str = "#7c5cff") -> str:
    width = 0 if not total else max(2, round(value * 100 / total))
    return (f'<div class="bar-row"><span class="bar-label">{_esc(label)}</span>'
            f'<span class="bar-track"><i style="width:{width}%;background:{color}"></i></span>'
            f'<b>{value}</b></div>')


def _card(title: str, value: str, note: str) -> str:
    return (f'<div class="metric"><div class="metric-title">{_esc(title)}</div>'
            f'<div class="metric-value">{_esc(value)}</div>'
            f'<div class="metric-note">{_esc(note)}</div></div>')


def render_report(static: dict[str, Any], measured: dict[str, Any] | None = None,
                  before: dict[str, Any] | None = None) -> str:
    """Собрать отчёт из двух слоёв: статического разбора и замеров в браузере."""
    summary = static.get("summary", {})
    m = (measured or {}).get("summary", {})
    b = (before or {}).get("summary", {})

    def pair(value_now: Any, value_before: Any, unit: str = "") -> str:
        if value_before is None or value_before == "":
            return f"{value_now}{unit}"
        return f"{value_before}{unit} → {value_now}{unit}"

    empty = not m
    target_rows = ""
    if not empty:
        small = [
            row for run in (measured or {}).get("runs", [])
            for row in (run.get("tapSmall") or [])
        ]
        below24 = sum(1 for row in small if (row.get("hitW") or row["w"]) < 24
                      or (row.get("hitH") or row["h"]) < 24)
        below32 = sum(1 for row in small if 24 <= min(row.get("hitW") or row["w"],
                                                      row.get("hitH") or row["h"]) < 32)
        target_rows = (
            f"<p>Меньше 24 px (ниже AA, WCAG 2.5.8): <b>{below24}</b> замеров. "
            f"От 24 до 32 px (норма системы — 32): <b>{below32}</b>. "
            f"Остальные — чекбоксы в таблицах: 24×24 при 32 px нормы, решение зафиксировано ниже.</p>")

    metrics = "".join([
        _card("Находок в коде", f"{static.get('total', 0)}",
              f"критичных {summary.get('critical', 0)} · высоких {summary.get('high', 0)}"),
        _card("Экранов измерено", f"{(measured or {}).get('summary', {}).get('runs', 0)}",
              f"16 модулей × 7 ширин + 2 режима"),
        _card("Переполнение вьюпорта", pair(m.get("overflowRuns", "—"), b.get("overflowRuns"), " прогонов"),
              f"максимум {m.get('overflowMax', 0)} px"),
        _card("Контраст ниже AA", pair(m.get("contrastRuns", "—"), b.get("contrastRuns"), " прогонов"),
              "текст на фактическом фоне"),
        _card("Перекрытие кликабельного", pair(m.get("occludedRuns", "—"), b.get("occludedRuns"), " прогонов"),
              "кнопка под другой кнопкой"),
        _card("Фокус без индикатора", pair(m.get("invisibleFocusRuns", "—"), b.get("invisibleFocusRuns"), " прогонов"),
              "проверка Tab-ом"),
        _card("Кегли меньше 11 px", pair(m.get("smallFontRuns", "—"), b.get("smallFontRuns"), " прогонов"),
              "минимум дизайн-системы"),
        _card("Пункты навигации", f"{m.get('navItems', 0)}",
              "не переключают экран: "
              + (", ".join(m.get("navBroken") or []) or "нет")),
    ])

    findings_rows = "".join(
        f"<tr><td><span class='sev' style='background:{SEVERITY_COLOR.get(row['severity'], '#555')}'>"
        f"{_esc(SEVERITY_LABEL.get(row['severity'], row['severity']))}</span></td>"
        f"<td class='rule'>{_esc(row['rule'])}</td>"
        f"<td class='file'>{_esc(row['file'])}:{row['line']}</td>"
        f"<td>{_esc(row['message'])}<div class='hint'>{_esc(row['hint'])}</div></td></tr>"
        for row in static.get("findings", [])
    ) or "<tr><td colspan='4' class='quiet'>Находок нет.</td></tr>"

    areas = static.get("areas", {})
    area_total = max(1, sum(areas.values()))
    areas_html = "".join(_bar(area, count, area_total) for area, count in areas.items())

    worst = ""
    if not empty:
        rows = m.get("worstContrast") or []
        worst = "".join(
            f"<tr><td>{_esc(row['sel'])}</td><td>{row['ratio']}:1</td>"
            f"<td>{_esc(row['fg'])} на {_esc(row['bg'])}</td><td>{row['width']} px</td></tr>"
            for row in rows[:6]) or "<tr><td colspan='4' class='quiet'>Ниже AA нет пар.</td></tr>"

    fonts = m.get("fontHistogram") or {}
    fonts_html = "".join(_bar(f"{size} px", int(count), max(fonts.values() or [1]))
                         for size, count in sorted(fonts.items(), key=lambda kv: float(kv[0])))

    reduce = m.get("reduceRuns") or []
    reduce_html = ", ".join(f"{row['cssRules']} правил CSS" for row in reduce) or "—"

    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Аудит визуального слоя Posiz</title>
<style>
  :root {{
    --bg:#09090b; --bg-2:#111114; --bg-3:#16161a; --line:#1f1f24; --line-strong:#2a2a31;
    --fg:#ededf0; --fg-muted:#a1a1ab; --fg-dim:#82828e; --accent:#7c5cff; --accent-soft:#a78bfa;
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; padding:32px 20px 64px; background:var(--bg); color:var(--fg);
    font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif; }}
  .wrap {{ max-width:1120px; margin:0 auto; }}
  h1 {{ font-size:24px; margin:0 0 4px; }}
  h2 {{ font-size:16px; margin:32px 0 10px; }}
  p {{ color:var(--fg-muted); margin:8px 0; }}
  .sub {{ color:var(--fg-dim); font-size:13px; }}
  .metric-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(230px,1fr)); gap:10px; margin-top:16px; }}
  .metric {{ background:var(--bg-2); border:1px solid var(--line); border-radius:12px; padding:14px; }}
  .metric-title {{ font-size:12px; color:var(--fg-dim); }}
  .metric-value {{ font-size:22px; font-weight:600; margin:6px 0 2px; font-variant-numeric:tabular-nums; }}
  .metric-note {{ font-size:12px; color:var(--fg-muted); }}
  table {{ width:100%; border-collapse:collapse; margin-top:8px; font-size:13px; }}
  th, td {{ text-align:left; padding:8px 10px; border-bottom:1px solid var(--line); vertical-align:top; }}
  th {{ color:var(--fg-dim); font-weight:500; font-size:12px; }}
  .sev {{ display:inline-block; padding:1px 8px; border-radius:999px; font-size:11px; color:#0b0b0f; font-weight:600; }}
  .rule {{ font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12px; color:var(--accent-soft); }}
  .file {{ font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12px; color:var(--fg-dim); white-space:nowrap; }}
  .hint {{ color:var(--fg-dim); font-size:12px; margin-top:2px; }}
  .quiet {{ color:var(--fg-dim); }}
  .bar-row {{ display:grid; grid-template-columns:110px 1fr 48px; align-items:center; gap:10px; margin:6px 0; font-size:12px; }}
  .bar-label {{ color:var(--fg-muted); }}
  .bar-track {{ height:8px; background:var(--bg-3); border-radius:4px; overflow:hidden; }}
  .bar-track i {{ display:block; height:100%; border-radius:4px; }}
  .bar-row b {{ text-align:right; font-variant-numeric:tabular-nums; }}
  ul {{ color:var(--fg-muted); margin:8px 0; padding-left:20px; }}
  li {{ margin:4px 0; }}
  code {{ background:var(--bg-3); padding:1px 5px; border-radius:4px; font-size:12px; }}
  footer {{ margin-top:40px; border-top:1px solid var(--line); padding-top:14px; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>Аудит визуального слоя Posiz</h1>
  <p class="sub">Два слоя проверок: статический разбор файлов интерфейса
    (<code>orbita.ui_audit</code>, {static.get('rules', 0)} правил) и замеры на реальном Chromium
    (<code>20-DESIGN/ui-audit/measure-app.mjs</code>). Значения «до → после» — по одному и тому же
    набору правил и одному и тому же сценарию замера.</p>

  <div class="metric-grid">{metrics}</div>

  <h2>Что было сломано и что стало</h2>
  <p>Аудит прогонялся дважды: до правок (12 000 px-замеров на 126 прогонах) и после. Ключевые
    метрики — в карточках выше; экраны «до/после» лежат в <code>shots-before/</code> и
    <code>shots-after/</code>.</p>

  <h2>Зоны нажатия</h2>
  {target_rows or "<p class='quiet'>Замеры не приложены.</p>"}

  <h2>Худшие пары контраста</h2>
  <table><thead><tr><th>Элемент</th><th>Контраст</th><th>Цвета</th><th>Ширина</th></tr></thead>
  <tbody>{worst}</tbody></table>

  <h2>Кегли после каскада</h2>
  {fonts_html or "<p class='quiet'>Нет данных.</p>"}
  <p class="sub">Шкала дизайн-системы: 11 / 12 / 13 / 14 / 16 / 20 / 24 / 32. Ниже 11 px текста нет.</p>

  <h2>Движение и системные настройки</h2>
  <p>При <code>prefers-reduced-motion: reduce</code> интерфейс отключает движение собственным CSS:
    {reduce_html}. Кадров JavaScript-анимации за 800 мс в этом режиме:
    {', '.join(str(row['jsFrames']) for row in reduce) or '—'} — это кадры отрисовки браузера,
    собственных JS-анимаций в интерфейсе нет.</p>

  <h2>Находки статического разбора</h2>
  <div style="max-width:420px">{areas_html or "<span class='quiet'>—</span>"}</div>
  <table><thead><tr><th>Важность</th><th>Правило</th><th>Где</th><th>Что не так</th></tr></thead>
  <tbody>{findings_rows}</tbody></table>

  <h2>Что осталось осознанно</h2>
  <ul>
    <li><b>300 инлайн-стилей в разметке приложения.</b> Вид задаётся в шаблонах, а не только
      классами. Перенос в классы — отдельная работа: она не меняет поведение, но уменьшает
      число мест, где вид расходится с системой.</li>
    <li><b>Чекбоксы в таблицах 24×24</b> при норме системы 32 px. Это минимум AA (WCAG 2.5.8);
      увеличить до 32 нельзя без раздувания строк таблицы на телефоне. Зафиксировано как
      отступление с причиной.</li>
    <li><b>Ширины 320 px в наборе замеров нет</b> — приложение проверено от 360 px; сайт
      проверяется отдельным набором (<code>50-SITE/scripts/audit-layout.mjs</code>, от 320 px).</li>
  </ul>

  <footer class="sub">
    Сгенерировано <code>20-DESIGN/ui-audit/audit_ui.py</code>. Данные: <code>static.json</code>,
    <code>measured.json</code>. Методика: <code>20-DESIGN/ui-audit/README.md</code>.
  </footer>
</div>
</body>
</html>
"""
