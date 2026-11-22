"""Демо этапа I6: A/B заголовка и первого абзаца на одном теле статьи.

Что показывает скрипт:

1. **Три варианта на одном теле.** У теста один `body_hash`; варианты отличаются заголовком и
   вводкой (формулы из реестра: вопрос, число, инструкция) и гипотезой — что именно проверяем.
2. **Стабильное разбиение.** 3 000 «читателей» получают вариант из `sha256(test_id + visitor)`:
   один и тот же человек всегда видит один и тот же заголовок.
3. **Решение по статистике.** Клики разыгрываются детерминированно по заданным вероятностям;
   двухвыборочный z-тест объявляет победителя, а второй тест с близкими вероятностями честно
   получает `no_difference` — «менять не на чем».
4. **Применение меняет только заголовок и вводку.** Тело статьи остаётся прежним (отпечаток
   совпадает), версия растёт на одну, в истории — причина.

Запуск (из 40-BACKEND):
    PYTHONPATH=src DATA_LAYER=memory python scripts/demo_article_ab.py
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import sys
from html import escape

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from orbita import article_ab, articles  # noqa: E402

OUT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..",
                                   "20-DESIGN", "articles", "backend"))
VISITORS = 3000

TEXT = (
    "Отчёт по охватам нужен для решения, а не для отчётности. Поэтому в нём три строки: сколько "
    "людей увидели материал, сколько перешли по ссылке и сколько из перешедших сделали целевое "
    "действие. Охват без перехода — потраченный показ, переход без действия — потраченный клик. "
    "Если в отчёте нет цены действия, канал нельзя ни оставить, ни закрыть: решение примут по "
    "вкусу, а не по данным. Сравнивать периоды нужно одинаковой длины — неделя с неделей, месяц "
    "с месяцем, иначе сезонность съест вывод. Цифры должны приходить из кабинета, а не из памяти "
    "автора: отчёт, собранный по памяти, показывает не канал, а уверенность автора.")

BRIEF = {
    "topic": "Как читать отчёт по охватам и переходам",
    "keywords": ["отчёт по охватам", "переходы", "цена действия"],
    "sections": 4,
    "lead": ("Отчёт по охватам нужен для решений, а не для отчётности: охваты, переходы и цена "
             "действия — три строки, по которым оставляют или закрывают канал."),
    "summary": ["Охваты, переходы и цена действия — три строки отчёта",
                "Отчёт без цены действия не отвечает на главный вопрос",
                "Сравнивать можно только периоды одинаковой длины"],
    "faq": [{"q": "Как часто нужен отчёт?", "a": "Раз в неделю: чаще — шум, реже — поздно."}],
    "cta": "Открыть отчёт по своим каналам",
    "cta_url": "/pricing",
    "outline": [
        {"heading": "Охваты и переходы", "text": TEXT,
         "columns": ["Строка", "Что показывает"],
         "rows": [["Охваты", "сколько увидели"], ["Переходы", "сколько перешли"],
                  ["Действие", "сколько дошли до цели"]],
         "source": "отчёт кабинета, ноябрь 2026"},
        {"heading": "Цена действия", "text": TEXT},
        {"heading": "Отчёт в кабинете", "text": TEXT},
        {"heading": "Что дальше", "text": TEXT, "steps": ["Сверить охваты", "Посчитать цену действия",
                                                          "Оставить или сменить канал"]},
    ],
}

#: «истинные» вероятности клика, по которым разыгрывается демо-трафик
PROBABILITY = {"question": 0.045, "number": 0.078, "howto": 0.052, "benefit": 0.050}


def simulate(test_id: str, *, seed: int = 7) -> dict[str, dict[str, int]]:
    """Показы и клики по вариантам: разбиение настоящее, клики — по заданной вероятности."""
    rnd = random.Random(seed)
    stats: dict[str, dict[str, int]] = {}
    for index in range(VISITORS):
        code = article_ab.assign(test_id, f"reader-{index}")["code"]
        row = stats.setdefault(code, {"impressions": 0, "clicks": 0})
        row["impressions"] += 1
        if rnd.random() < PROBABILITY.get(code, 0.05):
            row["clicks"] += 1
    return stats


def bars(variants: list[dict], winner: str | None) -> str:
    """Столбцы CTR в inline-SVG: отчёт читается без библиотек и без внешних файлов."""
    width, height, pad = 560, 220, 34
    rates = [float(row.get("ctr") or 0) for row in variants]
    top = max(rates) if rates and max(rates) > 0 else 0.01
    step = (width - pad * 2) / max(len(variants), 1)
    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="CTR по вариантам" '
             f'style="width:100%;height:auto">',
             f'<rect width="{width}" height="{height}" fill="#0f1117" rx="12"/>']
    for line in range(4):
        y = pad + (height - pad * 2) * line / 3
        parts.append(f'<line x1="{pad}" y1="{y:.1f}" x2="{width - pad}" y2="{y:.1f}" '
                     f'stroke="#232734"/>')
    for index, row in enumerate(variants):
        rate = float(row.get("ctr") or 0)
        bar_height = (height - pad * 2) * (rate / top) if top else 0
        x = pad + step * index + step * 0.15
        y = height - pad - bar_height
        colour = "#7c5cff" if row["code"] == winner else "#3f4557"
        parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{step * 0.7:.1f}" '
                     f'height="{bar_height:.1f}" fill="{colour}" rx="6"/>')
        parts.append(f'<text x="{x + step * 0.35:.1f}" y="{height - pad + 18:.1f}" fill="#9aa1b1" '
                     f'font-size="12" text-anchor="middle" font-family="DejaVu Sans">'
                     f'{escape(row["code"])}</text>')
        parts.append(f'<text x="{x + step * 0.35:.1f}" y="{y - 8:.1f}" fill="#e7e9ee" '
                     f'font-size="12" text-anchor="middle" font-family="DejaVu Sans">'
                     f'{rate * 100:.1f}%</text>')
    parts.append("</svg>")
    return "".join(parts)


def page(title: str, blocks: str) -> str:
    return ("<!doctype html><html lang=\"ru\"><head><meta charset=\"utf-8\">"
            f"<title>{escape(title)}</title><style>"
            "body{margin:0;background:#0b0d12;color:#e7e9ee;font:16px/1.6 -apple-system,"
            "Segoe UI,Roboto,sans-serif}main{max-width:820px;margin:0 auto;padding:40px 24px}"
            "h1{font-size:30px;line-height:1.2;margin:0 0 8px}h2{font-size:20px;margin:34px 0 10px}"
            "p{margin:0 0 14px;color:#c7ccd6}.muted{color:#8b93a3;font-size:14px}"
            ".card{background:#141824;border:1px solid #232734;border-radius:14px;padding:18px;"
            "margin:0 0 14px}.win{border-color:#7c5cff}"
            "table{width:100%;border-collapse:collapse;font-size:15px}"
            "th,td{text-align:left;padding:8px 10px;border-bottom:1px solid #232734}"
            "th{color:#8b93a3;font-weight:600}code{background:#1b2030;padding:1px 6px;"
            "border-radius:6px;font-size:13px}.badge{display:inline-block;padding:2px 10px;"
            "border-radius:999px;background:#1b2030;color:#9aa1b1;font-size:13px}"
            "</style></head><body><main>" + blocks + "</main></body></html>")


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    articles.reset()
    article_ab.reset()

    result = articles.compose(BRIEF)
    if not result.get("ok"):
        raise SystemExit(f"статья не собралась: {result}")
    spec = result["spec"]
    published = articles.publish(spec)["article"]

    made = article_ab.headline_variants(spec, count=3)
    if not made.get("ok"):
        raise SystemExit(f"варианты не собраны: {made}")
    test = article_ab.create(spec, count=3, article_id=published["id"])["test"]
    stats = simulate(test["id"])
    for code, row in stats.items():
        article_ab.record(test["id"], code, impressions=row["impressions"], clicks=row["clicks"])
    decision = article_ab.evaluate(test["id"])
    applied = article_ab.apply(test["id"], spec) if decision.get("ok") else None
    if applied:
        articles.replace(published["id"], applied["spec"], reason="A/B заголовка по данным")

    # второй тест: варианты с близкими вероятностями — честный отказ вместо «победителя»
    noise = article_ab.create(spec, count=2, article_id=published["id"], salt="noise")["test"]
    PROBABILITY.update({"question": 0.05, "number": 0.051})
    noise_stats = simulate(noise["id"], seed=11)
    for code, row in noise_stats.items():
        article_ab.record(noise["id"], code, impressions=row["impressions"], clicks=row["clicks"])
    noise_decision = article_ab.evaluate(noise["id"])

    report = article_ab.report(test["id"])
    payload = {
        "article": {"id": published["id"], "slug": spec["slug"],
                    "title": spec["seo"]["title"], "words": spec["stats"]["words"],
                    "body_hash": test["body_hash"]},
        "test": {key: report[key] for key in ("test_id", "status", "variants", "skipped",
                                              "min_impressions", "alpha", "progress")},
        "simulation": {"visitors": VISITORS, "probability": PROBABILITY, "stats": stats},
        "decision": decision,
        "applied": None if not applied else {"winner": applied["winner"],
                                             "title": applied["title"],
                                             "lead": applied["lead"],
                                             "version": applied["version"],
                                             "body_hash_after": article_ab._body_hash(
                                                 applied["spec"]),
                                             "body_unchanged": article_ab._body_hash(
                                                 applied["spec"]) == test["body_hash"],
                                             "history": applied["spec"]["history"][-1]},
        "noise_test": {"test_id": noise["id"], "decision": noise_decision,
                       "stats": noise_stats},
    }
    with open(os.path.join(OUT, "demo-article-abtest.json"), "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    if applied:
        with open(os.path.join(OUT, "demo-article-abtest-applied.html"), "w",
                  encoding="utf-8") as handle:
            handle.write(articles.render_html(applied["spec"]))

    rows = "".join(
        f'<tr><td><code>{escape(row["code"])}</code></td>'
        f'<td>{row["impressions"]}</td><td>{row["clicks"]}</td>'
        f'<td>{(row["ctr"] or 0) * 100:.1f}%</td>'
        f'<td>{"победитель" if row["code"] == (decision.get("winner") or "") else ""}</td></tr>'
        for row in report["variants"])
    cards = "".join(
        f'<div class="card{" win" if row["code"] == (decision.get("winner") or "") else ""}">'
        f'<span class="badge">{escape(row["code"])}</span>'
        f'<h2 style="margin:10px 0 6px">{escape(row["title"])}</h2>'
        f'<p>{escape(row["lead"] or "")}</p>'
        f'<p class="muted">гипотеза: {escape(row["hypothesis"] or "")}'
        f' · вводка: {escape(row.get("lead_kind") or "")}</p></div>'
        for row in report["variants"])
    blocks = (
        f'<h1>Заголовок решают данные, а не вкус</h1>'
        f'<p class="muted">{escape(spec["seo"]["title"])} · {VISITORS} читателей · '
        f'порог {report["min_impressions"]} показов, α = {report["alpha"]}</p>'
        f'<p>Тело статьи у всех вариантов одно — отпечаток '
        f'<code>{test["body_hash"][:12]}…</code>. Это и делает сравнение честным: если бы у '
        f'вариантов расходился текст, «победил бы заголовок», которого на самом деле нет.</p>'
        f'{cards}'
        f'<h2>Что показал замер</h2>'
        f'{bars(report["variants"], decision.get("winner"))}'
        f'<table><thead><tr><th>вариант</th><th>показы</th><th>клики</th><th>CTR</th>'
        f'<th>решение</th></tr></thead><tbody>{rows}</tbody></table>'
        f'<p>z-тест для долей: p = {decision.get("p_value")}, разница '
        f'{decision.get("uplift")} в пользу «{escape(str(decision.get("winner")))}». '
        f'При пороге α = {decision.get("alpha")} этого достаточно, чтобы поменять заголовок '
        f'и вводку — и только их.</p>'
        f'<h2>Честный отказ</h2>'
        f'<p>Второй тест на близких вероятностях получил '
        f'<code>{escape(str(noise_decision.get("code")))}</code>: '
        f'{escape(str(noise_decision.get("reason")))}. Заголовок не меняется — данных на решение '
        f'нет, и это тоже результат.</p>'
        + (f'<h2>Что встало в статью</h2>'
           f'<div class="card win"><h2 style="margin:0 0 6px">{escape(applied["title"])}</h2>'
           f'<p>{escape(applied["lead"] or "")}</p>'
           f'<p class="muted">версия {applied["version"]} · тело не изменилось '
           f'({article_ab._body_hash(applied["spec"])[:12]}… = {test["body_hash"][:12]}…)</p>'
           f'</div>' if applied else "")
    )
    with open(os.path.join(OUT, "demo-article-abtest-report.html"), "w",
              encoding="utf-8") as handle:
        handle.write(page("A/B заголовка: отчёт", blocks))

    print(f"варианты: {', '.join(row['code'] for row in report['variants'])} "
          f"(тело {test['body_hash'][:12]}…)")
    for row in report["variants"]:
        clicks = article_ab.plural(row["clicks"], "клик", "клика", "кликов")
        print(f"  {row['code']:9} {row['impressions']:5} показов · {row['clicks']:4} {clicks} · "
              f"CTR {(row['ctr'] or 0) * 100:.1f}% · {row['title']}")
    print(f"решение: {decision.get('code')} — {decision.get('reason')}")
    if applied:
        print(f"применено: «{applied['title']}» · версия {applied['version']} · "
              f"тело не изменилось: {article_ab._body_hash(applied['spec']) == test['body_hash']}")
    print(f"второй тест: {noise_decision.get('code')} — {noise_decision.get('reason')}")
    print(f"отчёт: {os.path.join(OUT, 'demo-article-abtest-report.html')}")
    if applied:
        print(f"статья: {os.path.join(OUT, 'demo-article-abtest-applied.html')}")


if __name__ == "__main__":
    main()
