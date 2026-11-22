"""Демо этапа I5: «Читайте также» по смыслу и обновление вечнозелёной статьи.

Что показывает скрипт — на блоге из четырёх статей, собранных тем же движком:

1. **Близость отличима от случайной.** Своя статья об охватах и соседняя статья об охватах
   дают косинус около 0.7, статья о ценах — около нуля. Хэш-эмбеддинг из `llm` такого различия
   не даёт, поэтому ссылки по нему были бы шумом (числа — в отчёте).
2. **Ссылка ставится по смыслу и объясняет себя.** Блок «Читайте также» встаёт перед призывом,
   у блока есть причина, у каждой ссылки — анкор из заголовка статьи, а не «читать далее».
3. **Порог — данные.** Статья о ценах не получает ссылку на статью об охватах: ближайшая ниже
   порога, и это названо (`no_candidates`) с числом, а не спрятано.
4. **Обновление только по материалам.** Вечнозелёная статья старше срока ревью обновляет цены и
   ряд переходов из присланных материалов; ряд без материала остаётся прежним и попадает в
   `skipped`. Повторное обновление с теми же данными — `no_changes`, версия не растёт.

Запуск (из 40-BACKEND):
    PYTHONPATH=src DATA_LAYER=memory python scripts/demo_article_links.py
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from orbita import article_links, articles  # noqa: E402

OUT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..",
                                   "20-DESIGN", "articles", "backend"))
NOW = "2027-06-01T09:00:00+00:00"     # ревью было в ноябре 2026 → срок 180 дней истёк

TEXT = (
    "Первое, что стоит понять: отчёт нужен для решения, а не для отчётности. Поэтому смотрим на "
    "три строки — сколько людей увидели материал, сколько перешли по ссылке и сколько из "
    "перешедших сделали целевое действие. Охват без перехода — это потраченный показ, переход "
    "без действия — потраченный клик. Если в отчёте нет цены действия, канал нельзя ни оставить, "
    "ни закрыть: решение примут по вкусу, а не по данным. Дальше сравниваем периоды одинаковой "
    "длины: неделя с неделей, месяц с месяцем, иначе сезонность съест вывод. И последнее: "
    "цифры должны приходить из кабинета, а не из памяти автора.")


def _section(heading: str, extra: dict | None = None) -> dict:
    return {"heading": heading, "text": TEXT, **(extra or {})}


CORPUS = [
    {"slug": "kak-chitat-otchet-po-ohvatam",
     "topic": "Как читать отчёт по охватам и переходам",
     "keywords": ["отчёт по охватам", "переходы", "цена действия"],
     "evergreen": True,
     "heads": ["Охваты и переходы", "Отчёт в кабинете", "Цена действия"],
     "extra": {"index": 0, "columns": ["Пакет", "Что входит", "Цена"],
               "rows": [["Базовый", "охваты и переходы", "0 ₽"],
                        ["Расширенный", "плюс цена действия", "3 900 ₽"]],
               "source": "прайс клиента, ноябрь 2026"},
     #: ряд переходов — чтобы после обновления было видно и пропуск: материал о ценах пришёл,
     #: а ряда переходов в нём нет
     "chart": {"heading": "Переходы по неделям", "text": TEXT, "material": "переходы",
               "chart_kind": "line"},
     "materials": [{"kind": "numbers", "code": "переходы", "title": "Переходы по неделям",
                    "unit": " шт", "values": [{"label": "нед. 1", "value": 820},
                                              {"label": "нед. 2", "value": 910},
                                              {"label": "нед. 3", "value": 1040}],
                    "source": "отчёт кабинета, ноябрь 2026"}]},
    {"slug": "chto-pokazyvaet-otchet-po-ohvatam",
     "topic": "Что показывает отчёт по охватам в цифрах",
     "keywords": ["отчёт по охватам", "цена действия", "динамика"],
     "heads": ["Охваты и переходы", "Цена действия", "Как менять канал"]},
    {"slug": "otchet-po-ohvatam-oshibki",
     "topic": "Отчёт по охватам: пять ошибок в подсчёте",
     "keywords": ["отчёт по охватам", "ошибки подсчёта"],
     "heads": ["Переходы считают дважды", "Охваты без периода", "Цена действия"]},
    {"slug": "prays-na-vedenie-kanala",
     "topic": "Прайс на ведение канала: пакеты и сроки",
     "keywords": ["прайс", "ведение канала", "пакеты"],
     "heads": ["Пакеты", "Сроки", "Оплата"]},
]

BRIEFS = {
    "kak-chitat-otchet-po-ohvatam": {
        "cta": "Открыть отчёт по своим каналам", "cta_url": "/pricing",
        "faq": [{"q": "Как часто нужен отчёт?",
                 "a": "Раз в неделю: чаще — шум, реже — поздно."}],
        "summary": ["Охваты, переходы и цена действия — строки, по которым решают",
                    "Отчёт без цены действия не отвечает на главный вопрос",
                    "Сравнивать можно только периоды одинаковой длины"]},
}


def build(entry: dict) -> dict:
    head = _section(entry["heads"][0])
    for key in ("columns", "rows", "source"):
        if key in entry.get("extra", {}):
            head[key] = entry["extra"][key]
    sections = [head] + [_section(head) for head in entry["heads"][1:]]
    if entry.get("chart"):
        sections.insert(1, dict(entry["chart"]))
    brief = {
        "topic": entry["topic"], "keywords": entry["keywords"], "sections": len(sections),
        "outline": sections, "evergreen": entry.get("evergreen", False),
        **BRIEFS.get(entry["slug"], {}),
    }
    result = articles.compose(brief, materials=entry.get("materials"))
    if not result.get("ok"):
        raise SystemExit(f"статья не собралась: {result}")
    return result["spec"]


def write(name: str, spec: dict) -> str:
    page = os.path.join(OUT, name)
    with open(page, "w", encoding="utf-8") as handle:
        handle.write(articles.render_html(spec))
    return page


def write_json(name: str, payload: dict) -> str:
    path = os.path.join(OUT, name)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    return path


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    articles.reset()
    article_links.reset()
    # домен блога известен — значит у статей есть canonical; без него ссылка не появляется
    saved_base = os.environ.get("ARTICLE_CANONICAL_BASE")
    os.environ["ARTICLE_CANONICAL_BASE"] = "https://posiz.ru/blog"

    specs = {entry["slug"]: build(entry) for entry in CORPUS}
    for entry in CORPUS:
        articles.publish(specs[entry["slug"]])
    article_links.index_all(articles.articles(20))

    target = specs["kak-chitat-otchet-po-ohvatam"]
    plan = article_links.link_plan(target)
    linked = article_links.apply_links(target, plan["links"]) if plan["ok"] else None
    if not linked:
        raise SystemExit(f"перелинковка не нашла ссылок: {plan}")
    linked_spec = linked["spec"]

    # отказ: статья о ценах не получает ссылку на статьи об охватах — тема разная
    refused = article_links.link_plan(specs["prays-na-vedenie-kanala"])

    # обновление вечнозелёной статьи: цены и (если есть) ряд переходов — по материалам
    materials = [{"key": "прайс клиента, ноябрь 2026",
                  "rows": [["Базовый", "охваты и переходы", "0 ₽"],
                           ["Расширенный", "плюс цена действия", "4 400 ₽"]],
                  "source": "прайс клиента, февраль 2027"}]
    refreshed = article_links.refresh(linked_spec, materials, now=NOW,
                                      reason="прайс обновлён в феврале 2027, цены выросли")
    repeat = article_links.refresh(refreshed["spec"], materials, now="2028-06-01T09:00:00+00:00") \
        if refreshed.get("ok") else {}
    freshness_before = article_links.freshness(linked_spec, now=NOW)
    report = article_links.evergreen_report(articles.articles(20), now=NOW)

    write("demo-article-links.html", linked_spec)
    if refreshed.get("ok"):
        write("demo-article-refresh.html", refreshed["spec"])
    report_payload = {
        "corpus": [{"slug": entry["slug"], "topic": entry["topic"],
                    "evergreen": bool(entry.get("evergreen")),
                    "words": specs[entry["slug"]]["stats"]["words"]} for entry in CORPUS],
        "similarity": article_links.random_embed_gap(),
        "plan": {"ok": plan["ok"], "threshold": plan["threshold"], "considered": plan["considered"],
                 "reason": plan["reason"],
                 "links": [{key: item[key] for key in ("slug", "anchor", "score", "url")}
                           for item in plan["links"]]},
        "refusal": {key: refused[key] for key in
                    ("ok", "code", "threshold", "best_score", "reason")},
        "block": {"position": linked["position"], "added": linked["added"],
                  "reason": (linked["block"] or {}).get("reason"),
                  "stats": article_links.link_stats(linked_spec)},
        "freshness_before": freshness_before,
        "refresh": {"ok": refreshed.get("ok", False),
                    "changes": refreshed.get("changes", []),
                    "skipped": refreshed.get("skipped", []),
                    "version": refreshed.get("version"),
                    "updated_at": refreshed.get("updated_at"),
                    "repeat_code": repeat.get("code")},
        "evergreen_report": report,
    }
    write_json("demo-article-links-report.json", report_payload)

    print(f"индекс: {len(article_links.index_rows())} статьи")
    print(f"близость: одна тема {report_payload['similarity']['ours']['related']}, "
          f"другая тема {report_payload['similarity']['ours']['unrelated']} "
          f"(хэш: {report_payload['similarity']['hash']['related']} / "
          f"{report_payload['similarity']['hash']['unrelated']})")
    print(f"ссылки: {plan['reason']}")
    for item in plan["links"]:
        print(f"  {item['score']} — {item['anchor']} → {item['url']}")
    print(f"отказ: {refused['code']} — {refused['reason']}")
    print(f"ревью: {freshness_before['reason']} (срок {freshness_before['review_days']} дн.)")
    if refreshed.get("ok"):
        print(f"обновление: версия {refreshed['version']}, изменений {len(refreshed['changes'])}, "
              f"пропущено {len(refreshed['skipped'])}")
        print(f"  повтор без изменений: {repeat.get('code')}")
    print(f"вечнозелёные: {report['evergreen']} из {report['total']}, просрочено {report['due']}")
    print(f"canonical: {linked_spec['seo']['canonical']}")
    print(f"HTML: {os.path.join(OUT, 'demo-article-links.html')}")
    print(f"HTML: {os.path.join(OUT, 'demo-article-refresh.html')}")
    if saved_base is None:
        os.environ.pop("ARTICLE_CANONICAL_BASE", None)
    else:
        os.environ["ARTICLE_CANONICAL_BASE"] = saved_base


if __name__ == "__main__":
    main()
