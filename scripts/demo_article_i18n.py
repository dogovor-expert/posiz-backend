"""Демо этапа I7: одна статья — четыре языка, `hreflang` и честная неполнота.

Что показывает скрипт:

1. **Перевод — данные, «хром» — код.** Английская версия собирается из перевода текстов, а
   подписи («Read next», «source», «min read»), `lang`, время чтения по темпу языка (220 слов/мин
   против 180 у русского) ставит движок.
2. **Неполнота называется.** Казахская версия приходит частичной: в отчёте — список полей без
   перевода, `complete: false`, и в строгом режиме такая версия не публикуется.
3. **`hreflang` — часть артефакта.** У каждой версии есть ссылка на себя, есть `x-default`,
   и это видно в `<head>` каждой страницы.
4. **Переводы живут отдельными статьями.** Свой `id`, свой слаг: публикация английской версии не
   перезаписывает русскую.

Запуск (из 40-BACKEND):
    PYTHONPATH=src DATA_LAYER=memory python scripts/demo_article_i18n.py
"""
from __future__ import annotations

import json
import os
import sys
from html import escape

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from orbita import article_i18n, articles  # noqa: E402

OUT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..",
                                   "20-DESIGN", "articles", "backend"))
BASE = "https://posiz.ru/blog"

RU = (
    "Отчёт по охватам нужен для решения, а не для отчётности. В нём три строки: сколько людей "
    "увидели материал, сколько перешли по ссылке и сколько из перешедших сделали целевое "
    "действие. Охват без перехода — потраченный показ, переход без действия — потраченный клик. "
    "Если в отчёте нет цены действия, канал нельзя ни оставить, ни закрыть: решение примут по "
    "вкусу, а не по данным. Сравнивать периоды нужно одинаковой длины — неделя с неделей, месяц "
    "с месяцем, иначе сезонность съест вывод.")

EN = (
    "The reach report exists for decisions, not for reporting. It has three lines: how many people "
    "saw the post, how many followed the link and how many of them took the target action. Reach "
    "without a click is a wasted impression, a click without an action is a wasted visit. Without "
    "cost per action a channel can be neither kept nor closed: the decision will be made by taste, "
    "not by data. Compare periods of equal length — week to week, month to month — otherwise "
    "seasonality eats the conclusion.")

BRIEF = {
    "topic": "Как читать отчёт по охватам и переходам",
    "keywords": ["отчёт по охватам", "переходы"],
    "sections": 4,
    "lead": ("Отчёт по охватам нужен для решений, а не для отчётности: охваты, переходы и цена "
             "действия — три строки, по которым оставляют или закрывают канал."),
    "summary": ["Охваты, переходы и цена действия — три строки отчёта",
                "Отчёт без цены действия не отвечает на главный вопрос"],
    "faq": [{"q": "Как часто нужен отчёт?",
             "a": "Раз в неделю: чаще — шум, реже — поздно."}],
    "cta": "Открыть отчёт по своим каналам",
    "cta_url": "/pricing",
    "outline": [
        {"heading": "Охваты и переходы", "text": RU,
         "columns": ["Строка", "Что показывает"],
         "rows": [["Охваты", "сколько увидели"], ["Переходы", "сколько перешли"],
                  ["Действие", "сколько дошли до цели"]],
         "source": "отчёт кабинета, ноябрь 2026"},
        {"heading": "Цена действия", "text": RU},
        {"heading": "Отчёт в кабинете", "text": RU},
        {"heading": "Что дальше", "text": RU,
         "steps": ["Сверить охваты", "Посчитать цену действия", "Оставить или сменить канал"]},
    ],
}

#: Полный перевод на английский: все текстовые поля, включая строки таблицы и подписи.
EN_TRANSLATIONS = {
    "seo": {"title": "How to read a reach report and clicks",
            "description": "Reach, clicks and cost per action: three lines that decide the fate "
                           "of a channel.",
            "keywords": ["reach report", "clicks"]},
    "cta": {"text": "Open the report for your channels"},
}

EN_BLOCK_TEXT = {
    "lead": "The reach report exists for decisions: reach, clicks and cost per action are the "
            "three lines that keep or close a channel.",
    "heading": {"Охваты и переходы": "Reach and clicks",
                "Цена действия": "Cost per action",
                "Отчёт в кабинете": "The report in the dashboard",
                "Что дальше": "What to do next"},
    "paragraph": EN,
    "table_columns": ["Line", "What it shows"],
    "table_rows": [["Reach", "how many saw it"], ["Clicks", "how many followed"],
                   ["Action", "how many reached the goal"]],
    "source": "dashboard report, November 2026",
    "steps": ["Check reach", "Compute cost per action", "Keep or change the channel"],
    "faq": [{"q": "How often is the report needed?",
             "a": "Once a week: more often is noise, less often is late."}],
    "caption": "Reach, clicks and action",
    "alt": "Table: reach and clicks",
}

#: Частичный перевод на казахский: только заголовок и вводка — остальное честно остаётся пустым.
KK_TRANSLATIONS = {
    "seo": {"title": "Қамту есебін қалай оқу керек",
            "description": "Қамту, өтулер және әрекет құны — шешім қабылданатын үш жол.",
            "keywords": ["қамту есебі"]},
    "blocks": {},
}


def _en_translations(spec: dict) -> dict:
    blocks: dict[str, dict] = {}
    for block in spec["blocks"]:
        props = block.get("props") or {}
        kind = block["type"]
        given: dict = {}
        if kind == "lead":
            given["text"] = EN_BLOCK_TEXT["lead"]
        elif kind == "heading":
            given["text"] = EN_BLOCK_TEXT["heading"].get(props.get("text"), "Section")
        elif kind == "paragraph":
            given["text"] = EN_BLOCK_TEXT["paragraph"]
        elif kind == "table":
            given["columns"] = EN_BLOCK_TEXT["table_columns"]
            given["rows"] = EN_BLOCK_TEXT["table_rows"]
            given["caption"] = EN_BLOCK_TEXT["caption"]
        elif kind == "diagram":
            given["steps"] = EN_BLOCK_TEXT["steps"]
            given["title"] = "From numbers to decision"
            given["caption"] = "Steps"
        elif kind == "list":
            given["items"] = ["Reach, clicks and cost per action — three lines of the report",
                              "A report without cost per action does not answer the main question"]
        elif kind == "cta":
            given["text"] = EN_TRANSLATIONS["cta"]["text"]
        elif kind == "faq":
            given["items"] = EN_BLOCK_TEXT["faq"]
        elif kind == "callout":
            given["text"] = "Check the numbers before deciding."
        if props.get("alt"):
            given["alt"] = EN_BLOCK_TEXT["alt"]
        if given:
            blocks[block["id"]] = given
    return {"seo": EN_TRANSLATIONS["seo"], "blocks": blocks, "cta": EN_TRANSLATIONS["cta"]}


def _write(name: str, spec: dict) -> str:
    path = os.path.join(OUT, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(articles.render_html(spec))
    return path


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    articles.reset()

    result = articles.compose(BRIEF)
    if not result.get("ok"):
        raise SystemExit(f"статья не собралась: {result}")
    source = result["spec"]
    source["seo"]["canonical"] = f"{BASE}/kak-chitat-otchet-po-ohvatam/"
    source_locale = "ru"
    published = articles.publish(source)["article"]
    urls = {source_locale: source["seo"]["canonical"]}

    en = article_i18n.translate(source, "en", translations=_en_translations(source),
                                article_id=published["id"])
    if not en.get("ok"):
        raise SystemExit(f"английская версия не собралась: {en.get('missing')}")
    en_spec = en["spec"]
    en_spec["seo"]["canonical"] = f"{BASE}/en/how-to-read-a-reach-report/"
    urls["en"] = en_spec["seo"]["canonical"]

    kk_partial = article_i18n.translate(source, "kk", translations=KK_TRANSLATIONS, strict=False)
    kk_strict = article_i18n.translate(source, "kk", translations=KK_TRANSLATIONS, strict=True)

    group = article_i18n.hreflang(en_spec, urls, default="ru")
    for spec in (source, en_spec):
        spec["hreflang"] = group["rows"]
    articles.publish(en_spec)
    en_id = en_spec["id"]

    report = {
        "source": {"id": published["id"], "slug": source["slug"], "locale": source_locale,
                   "title": source["seo"]["title"], "canonical": source["seo"]["canonical"],
                   "reading": article_i18n.reading(source, "ru"),
                   "words": source["stats"]["words"]},
        "english": {"id": en_id, "slug": en_spec["slug"], "Locale": "en",
                    "title": en_spec["seo"]["title"],
                    "canonical": en_spec["seo"]["canonical"],
                    "reading": en_spec["reading"],
                    "labels": {key: en_spec["labels"][key] for key in
                               ("related", "source", "reading_time", "words")},
                    "applied": len(en["applied"]), "missing": en["missing"]},
        "kazakh": {"strict": {"ok": kk_strict.get("ok"), "code": kk_strict.get("code"),
                              "reason": kk_strict.get("reason"),
                              "missing": len(kk_strict.get("missing") or [])},
                   "draft": {"ok": kk_partial.get("ok"),
                             "complete": kk_partial["spec"]["translation"]["complete"],
                             "missing_first": (kk_partial.get("missing") or [])[:3],
                             "reading": kk_partial["spec"]["reading"]}},
        "hreflang": group,
        "locales": article_i18n.catalog(),
        "articles": len(articles.articles(20)),
    }
    with open(os.path.join(OUT, "demo-article-i18n.json"), "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)

    ru_page = _write("demo-article-i18n-ru.html", source)
    en_page = _write("demo-article-i18n-en.html", en_spec)

    rows = "".join(
        f'<tr><td>{escape(row["hreflang"])}</td><td><code>{escape(row["href"])}</code></td>'
        f'<td>{"своя версия" if row["self"] else ""}</td></tr>'
        for row in group["rows"])
    locale_rows = "".join(
        f'<tr><td>{escape(row["code"])}</td><td>{row["words_per_minute"]} слов/мин</td>'
        f'<td>{row["title_limit"]} знаков</td>'
        f'<td>{escape(str(row["reel_words_per_second"]))} слова/с (титры)</td></tr>'
        for row in article_i18n.catalog())
    missing_rows = "".join(
        f'<li><code>{escape(row["field"])}</code></li>'
        for row in (kk_partial.get("missing") or [])[:8])
    page = (
        "<!doctype html><html lang=\"ru\"><head><meta charset=\"utf-8\">"
        "<title>Одна статья — четыре языка</title><style>"
        "body{margin:0;background:#0b0d12;color:#e7e9ee;font:16px/1.6 -apple-system,Segoe UI,"
        "Roboto,sans-serif}main{max-width:820px;margin:0 auto;padding:40px 24px}"
        "h1{font-size:30px;line-height:1.2;margin:0 0 8px}h2{font-size:20px;margin:34px 0 10px}"
        "p{margin:0 0 14px;color:#c7ccd6}.muted{color:#8b93a3;font-size:14px}"
        ".card{background:#141824;border:1px solid #232734;border-radius:14px;padding:18px;"
        "margin:0 0 14px}table{width:100%;border-collapse:collapse;font-size:15px}"
        "th,td{text-align:left;padding:8px 10px;border-bottom:1px solid #232734}"
        "th{color:#8b93a3;font-weight:600}code{background:#1b2030;padding:1px 6px;"
        "border-radius:6px;font-size:13px}.badge{display:inline-block;padding:2px 10px;"
        "border-radius:999px;background:#1b2030;color:#9aa1b1;font-size:13px}"
        "a{color:#9d8bff}</style></head><body><main>"
        f'<h1>Одна статья — четыре языка</h1>'
        f'<p class="muted">{escape(source["seo"]["title"])} · {source["stats"]["words"]} слов · '
        f'{report["source"]["reading"]["minutes"]} мин (ru) / '
        f'{en_spec["reading"]["minutes"]} мин (en)</p>'
        f'<p>Тексты приходят переводом, «хром» переводит движок. Русская и английская версии — '
        f'отдельные статьи со своими адресами: '
        f'<a href="{escape(source["seo"]["canonical"])}">ru</a> · '
        f'<a href="{escape(en_spec["seo"]["canonical"])}">en</a>.</p>'
        f'<h2>Группа hreflang</h2>'
        f'<table><thead><tr><th>hreflang</th><th>адрес</th><th></th></tr></thead>'
        f'<tbody>{rows}</tbody></table>'
        f'<p class="muted">Своя версия в наборе обязательна: без неё поисковик не связывает '
        f'переводы, и они конкурируют друг с другом.</p>'
        f'<h2>Темп чтения — свой у статьи</h2>'
        f'<table><thead><tr><th>язык</th><th>статья</th><th>лимит заголовка</th>'
        f'<th>титры ролика</th></tr></thead><tbody>{locale_rows}</tbody></table>'
        f'<p class="muted">Профиль ролика меряет слова в секунду (титры), статья — слова в '
        f'минуту: если считать статью темпом титров, «5 минут» превращаются в 8.</p>'
        f'<h2>Честная неполнота</h2>'
        f'<div class="card"><span class="badge">kk · черновик</span>'
        f'<p>Строгий режим: <code>{escape(str(kk_strict.get("code")))}</code> — '
        f'{escape(str(kk_strict.get("reason")))}</p>'
        f'<p class="muted">Поля без перевода ({len(kk_partial.get("missing") or [])}):</p>'
        f'<ul>{missing_rows}</ul>'
        f'<p>Черновик сохранён с <code>complete: false</code> и в публикацию не идёт: '
        f'полурусская казахская версия — худший вид локализации.</p></div>'
        f'<h2>Что встало в английскую версию</h2>'
        f'<div class="card"><h2 style="margin:0 0 6px">'
        f'{escape(en_spec["seo"]["title"])}</h2>'
        f'<p class="muted">подписи: {escape(en_spec["labels"]["related"])} · '
        f'{escape(en_spec["labels"]["source"])} · {escape(en_spec["labels"]["reading_time"])}'
        f' · lang="en" · хром и hreflang поставил код</p></div>'
        "</main></body></html>")
    with open(os.path.join(OUT, "demo-article-i18n-report.html"), "w",
              encoding="utf-8") as handle:
        handle.write(page)

    print(f"ru: {source['seo']['title']} · {source['stats']['words']} слов · "
          f"{report['source']['reading']['minutes']} мин при "
          f"{report['source']['reading']['words_per_minute']} слов/мин")
    print(f"en: {en_spec['seo']['title']} · {en_spec['reading']['words']} слов · "
          f"{en_spec['reading']['minutes']} мин · подписи: {en_spec['labels']['related']}")
    print(f"kk строго: {kk_strict.get('code')} — без перевода "
          f"{len(kk_strict.get('missing') or [])} полей")
    print(f"kk черновик: complete={kk_partial['spec']['translation']['complete']}, "
          f"пропущено {len(kk_partial.get('missing') or [])}")
    print(f"hreflang: " + ", ".join(f"{row['hreflang']}" for row in group["rows"]))
    print(f"статей в хранилище: {report['articles']} (ru и en отдельно)")
    print(f"HTML: {ru_page}")
    print(f"HTML: {en_page}")


if __name__ == "__main__":
    main()
