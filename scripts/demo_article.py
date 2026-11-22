"""Демо этапа I: статья как готовая редакционная публикация.

Что показывает скрипт:

1. **Структуру придумывает система.** `plan()` раскладывает тему на разделы и для каждого
   решает, нужен ли визуал и какой — с причиной («здесь сравниваются варианты — таблица
   читается быстрее абзаца»).
2. **Визуал выбирается по данным раздела, а не по украшению.** Строки таблицы → таблица,
   числовой ряд → график, шаги → схема, `image_prompt` → иллюстрация. Каждая цифра ссылается
   на материал клиента, у каждого визуала есть `alt` и `reason`.
3. **HTML собирает код.** На выходе — самостоятельная страница: типографика, inline SVG для
   графика и схемы, подписи с источниками, разметка schema.org (Article + FAQPage).
4. **Проверки — часть результата.** В отчёт попадают замечания `check()`, покрытие ключевых
   фраз и уникальность по шинглам: то, что гейт релиза проверяет у движка, здесь видно на
   конкретной статье.

Запуск (из 40-BACKEND):
    PYTHONPATH=src DATA_LAYER=memory python scripts/demo_article.py
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from orbita import articles  # noqa: E402

OUT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..",
                                   "20-DESIGN", "articles", "backend"))

TOPIC = "Сколько стоит продвижение в мессенджерах в 2026 году"

MATERIALS = [
    {"kind": "numbers", "code": "reach", "title": "Охваты по месяцам", "unit": " тыс.",
     "source": "аналитика клиента, ноябрь 2026",
     "values": [{"label": "май", "value": 6.1}, {"label": "июн", "value": 7.0},
                {"label": "июл", "value": 8.2}, {"label": "авг", "value": 9.4},
                {"label": "сен", "value": 11.1}, {"label": "окт", "value": 12.4}]},
    {"kind": "numbers", "code": "cpm", "title": "Цена тысячи показов", "unit": " ₽",
     "source": "медиаплан клиента, ноябрь 2026",
     "values": [{"label": "Telegram", "value": 410}, {"label": "MAX", "value": 285},
                {"label": "VK", "value": 190}]},
]

OUTLINE = [
    {"heading": "Сколько стоит продвижение в мессенджерах",
     "columns": ["Канал", "Пост", "За 1 000 показов", "Охват"],
     "rows": [["Telegram", "12 000 ₽", "410 ₽", "18 000"],
              ["MAX", "9 000 ₽", "285 ₽", "12 400"],
              ["VK", "4 500 ₽", "190 ₽", "9 800"]],
     "source": "медиаплан клиента, ноябрь 2026",
     "text": (
        "Стоимость продвижения в мессенджерах считают не «за пост», а за тысячу показов: "
        "только так каналы сравнимы между собой. В медиаплане клиента на ноябрь 2026 года "
        "пост в Telegram стоит 12 000 ₽ при охвате 18 000 читателей, в MAX — 9 000 ₽ при "
        "12 400, в сообществах VK — 4 500 ₽ при 9 800. Цена за пост у площадок различается "
        "в 2,7 раза, а цена за показ — заметно меньше: часть подписчиков пост не открывает. "
        "Поэтому в таблицу рядом с ценой поста идёт цена за тысячу показов, и каналы "
        "сравнивают уже по ней, а не по прайсу размещения.")},
    {"heading": "Почему цена за тысячу показов разная", "material": "cpm",
     "chart_kind": "bar",
     "text": (
        "Разница в 2,2 раза объясняется не «дороговизной» площадки, а плотностью аудитории. "
        "В Telegram подписчик заходит в канал сам и видит один пост за раз, в VK показ часто "
        "попадает в ленту между чужими публикациями, поэтому стоит дешевле. Обратная сторона "
        "той же разницы — качество контакта: дешёвый показ в 190 ₽ и показ за 410 ₽ — это "
        "одно и то же действие, но не одно и то же внимание. Вывод «где дешевле» зависит от "
        "того, что вы считаете результатом: охват, переход или целевое действие.")},
    {"heading": "Как росли охваты за полгода", "material": "reach", "chart_kind": "line",
     "text": (
        "Охваты по данным аналитики клиента выросли с 6,1 тыс. в мае до 12,4 тыс. в октябре — "
        "вдвое за шесть месяцев. Рост почти линейный: ни один месяц не дал скачка больше "
        "1,7 тыс., то есть канал набирал аудиторию ровно, без всплесков от разовых закупок. "
        "Для планирования это важнее абсолютного числа: линейный рост предсказуем, и бюджет "
        "следующего месяца считается по той же цене за охват. Если бы график шёл ступенью, "
        "цену за показ пришлось бы пересчитывать каждый месяц заново.")},
    {"heading": "Из чего складывается цена размещения", "steps":
        ["Определяем аудиторию", "Выбираем формат", "Планируем частоту", "Сверяем отчёт"],
     "text": (
        "Цена складывается из четырёх шагов, и на каждом можно как сэкономить, так и потерять "
        "деньги. Сначала определяется аудитория: чем уже сегмент, тем дороже показ, но выше "
        "отклик. Затем формат: пост с одной ссылкой дешевле поста с нативной вставкой и "
        "видео. После — частота: второй показ тому же читателю стоит меньше, но приносит "
        "меньше. И в конце — отчётность: без неё нельзя понять, какая часть бюджета "
        "сработала, а какая ушла в пустой охват. Порядок шагов важен: экономный формат при "
        "неопределённой аудитории просто дешевле стоит и так же плохо работает.")},
    {"heading": "Как выглядит отчёт после размещения",
     "image_prompt": "Отчёт по охватам и переходам в кабинете клиента",
     "text": (
        "Отчёт — не таблица ради таблицы, а ответ на вопрос «что дальше». В кабинете видно "
        "три вещи: сколько человек увидели пост, сколько перешли по ссылке и сколько из "
        "перешедших сделали целевое действие. Эти три числа дают цену за переход и цену за "
        "действие, и уже они сравниваются между каналами. Отчёт без третьего числа "
        "бесполезен: он показывает интерес, но не результат, а бюджет выделяют под "
        "результат. Поэтому в отчёте всегда есть строка «цена действия» — даже если действие "
        "пока не случилось.")},
    {"heading": "Что сравнить перед стартом",
     "columns": [{"title": "Каналы", "items": ["быстрый охват", "цена за показ понятна",
                                               "эффект заканчивается с размещением"]},
                 {"title": "Свой блог", "items": ["трафик копится месяцами",
                                                  "не зависит от площадки",
                                                  "нужен постоянный контент"]},
                 {"title": "Посевы", "items": ["доверие аудитории автора",
                                               "плохо масштабируется",
                                               "отклик непредсказуем"]}],
     "text": (
        "Перед стартом сравнивают три варианта, и у каждого своя цена ошибки. Каналы дают "
        "быстрый охват и понятную цену за показ, но живут ровно столько, сколько идёт "
        "размещение. Собственный блог накапливает трафик месяцами, зато не зависит от "
        "площадки и работает годами. Посевы у смежных авторов дают доверие, но плохо "
        "масштабируются: у каждого автора своя аудитория и свой отклик. Выбор зависит от "
        "горизонта: если результат нужен на этой неделе — каналы, если в этом году — блог.")},
    {"heading": "Как проверить результат за неделю", "columns": ["Когда", "Что смотрим", "Норма"],
     "rows": [["День 1", "Сверка охватов", "Расхождение ≤ 20 %"],
              ["День 3", "Переходы", "Не меньше 0,5 % охвата"],
              ["День 7", "Цена действия", "Не выше прошлого размещения"]],
     "source": "регламент проверки клиента",
     "text": (
        "Проверка занимает неделю и не требует отдельного бюджета. В первый день сверяются "
        "охваты с ожидаемыми по цене за тысячу показов: расхождение больше 20 % значит, что "
        "аудитория не та. На третий день смотрят переходы: если их меньше 0,5 % от охвата, "
        "проблема в посадочной странице, а не в канале. На седьмой день считают цену "
        "действия и сравнивают с прошлым размещением. Такой цикл даёт ответ до того, как "
        "бюджет потрачен целиком, и позволяет остановить канал, который не работает, не "
        "дожидаясь конца месяца.")},
]

BRIEF = {
    "topic": TOPIC,
    "seo_title": "Продвижение в мессенджерах: цены и каналы 2026",
    "keywords": ["продвижение в мессенджерах", "стоимость рекламы", "цена за тысячу показов"],
    "sections": 7,
    "lead": ("Считаем стоимость продвижения в мессенджерах по цене за тысячу показов, "
             "показываем динамику охватов за полгода и объясняем, как проверить результат "
             "за неделю, а не в конце квартала."),
    "outline": OUTLINE,
    "materials": MATERIALS,
    "summary": ["Цена за показ в Telegram в 2,2 раза выше, чем в VK",
                "Охваты растут линейно: 6,1 тыс. → 12,4 тыс. за полгода",
                "Отчёт без цены действия не показывает результат"],
    "faq": [
        {"q": "Что входит в цену размещения?",
         "a": "Пост, ссылка, нативная вставка по договорённости и отчёт по охватам, "
              "переходам и цене действия."},
        {"q": "Можно ли начать с небольшого бюджета?",
         "a": "Да: минимальный тест — один канал и один формат на неделю, чтобы получить "
              "цену за переход на своей аудитории."},
        {"q": "Почему цена за пост и цена за показ дают разные выводы?",
         "a": "Пост покупают один раз, а показы зависят от того, сколько человек его "
              "действительно увидели: у площадок разная плотность ленты."},
    ],
    "quote": {"text": "Дешёвый показ, который никто не запомнил, стоит дороже всего.",
              "author": "медиапланер клиента"},
    "cta": "Посчитать бюджет по своим каналам",
    "cta_url": "/pricing",
    "canonical": "https://example.com/blog/prodvizhenie-v-messendzherah-2026/",
}

#: корпус «похожих» текстов: с ним сравнивается статья, чтобы доля общих шинглов была честной
CORPUS = [
    ("Продвижение в социальных сетях — это комплекс мер, направленный на привлечение "
     "внимания аудитории к продукту или услуге. Для эффективного продвижения необходимо "
     "учитывать особенности каждой площадки и регулярно публиковать контент."),
    ("Реклама в интернете позволяет охватить широкую аудиторию и получать измеримый "
     "результат. Грамотно настроенная рекламная кампания помогает увеличить продажи и "
     "узнаваемость бренда в короткие сроки."),
]


def build() -> dict:
    os.makedirs(OUT, exist_ok=True)
    result = articles.compose(BRIEF, materials=MATERIALS,
                              links=["kak-schitat-ohvat", "prodvizhenie-v-kanalah"])
    if not result.get("ok"):
        raise SystemExit(f"статья не собралась: {result}")
    spec, plan_data, checks = result["spec"], result["plan"], result["checks"]
    unique = articles.uniqueness(spec, CORPUS)
    html = articles.render_html(spec)
    page = os.path.join(OUT, "demo-article.html")
    with open(page, "w", encoding="utf-8") as handle:
        handle.write(html)
    with open(os.path.join(OUT, "demo-article.spec.json"), "w", encoding="utf-8") as handle:
        json.dump(spec, handle, ensure_ascii=False, indent=2)
    report = {
        "topic": TOPIC,
        "spec_version": spec["spec_version"],
        "plan": [{"heading": section["heading"],
                  "visual": (section["visual"] or {}).get("block"),
                  "reason": (section["visual"] or {}).get("reason")}
                 for section in plan_data["sections"]],
        # что реально попало в статью: план мог назвать блок, под который не было данных
        "materials": [{"section": block.get("section"), "type": block["type"],
                       "reason": block.get("reason"), "alt": block.get("alt"),
                       "source": block.get("source")}
                      for block in spec["blocks"]
                      if articles.BLOCKS[block["type"]]["visual"]],
        "checks": checks,
        "uniqueness": unique,
        "stats": spec["stats"],
        "seo": spec["seo"],
        "html_bytes": len(html.encode("utf-8")),
        "blocks": [{"type": block["type"],
                    "visual": bool(articles.BLOCKS[block["type"]]["visual"]),
                    "reason": block.get("reason")} for block in spec["blocks"]],
    }
    with open(os.path.join(OUT, "demo-article-report.json"), "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    return {"spec": spec, "checks": checks, "uniqueness": unique, "page": page,
            "report": report}


def poster(spec: dict, checks: dict, unique: dict) -> str:
    """Постер композиции: какие разделы и почему получили визуал. Читается без открытия HTML."""
    from PIL import Image, ImageDraw, ImageFont

    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 16)
    small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 13)
    bold = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 21)
    title_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 27)

    width, pad = 1240, 36
    headings, rows = {}, []
    for block in spec["blocks"]:
        if block["type"] == "heading":
            headings[block["section"] if "section" in block else None] = block["props"]["text"]
    ordered = [block["props"]["text"] for block in spec["blocks"] if block["type"] == "heading"]
    for block in spec["blocks"]:
        if articles.BLOCKS[block["type"]]["visual"]:
            index = block.get("section")
            heading = ordered[index - 1] if index and index <= len(ordered) else "—"
            rows.append((heading, block["type"], block.get("reason") or ""))
    rows.append(("Коротко · FAQ · призыв", "faq", "финал статьи: вопросы и следующий шаг"))

    def wrap(text: str, face, limit: int) -> list[str]:
        lines, line = [], ""
        for word in text.split():
            probe = f"{line} {word}".strip()
            if draw.textlength(probe, font=face) > limit:
                lines.append(line)
                line = word
            else:
                line = probe
        if line:
            lines.append(line)
        return lines

    probe = Image.new("RGB", (10, 10))
    draw = ImageDraw.Draw(probe)
    card_h = 96
    height = 190 + card_h * len(rows) + 120
    image = Image.new("RGB", (width, height), "#0f1117")
    draw = ImageDraw.Draw(image)

    draw.text((pad, 34), "Композиция статьи: визуалы и причины", fill="#ffffff", font=title_font)
    draw.text((pad, 78), spec["seo"]["title"], fill="#a9b1c6", font=font)
    meta = (f'{spec["stats"]["words"]} слов · {spec["stats"]["reading_minutes"]} мин чтения · '
            f'{spec["stats"]["visuals"]} визуалов · '
            f'проверки: {"без замечаний" if checks["ok"] else "есть замечания"} · '
            f'уникальность {unique["duplicate_ratio"]:.2f} при пороге {unique["limit"]}')
    draw.text((pad, 112), meta, fill="#7c5cff", font=small)

    y = 152
    for index, (heading, kind, reason) in enumerate(rows, start=1):
        draw.rounded_rectangle((pad, y, width - pad, y + card_h - 14), radius=12, fill="#161a24",
                               outline="#232734")
        draw.text((pad + 18, y + 16), f"{index:02d}", fill="#7c5cff", font=bold)
        draw.text((pad + 62, y + 14), heading[:52], fill="#ffffff", font=font)
        badge = kind
        draw.text((width - pad - 18 - draw.textlength(badge, font=bold), y + 16), badge,
                  fill="#8be9c0" if kind != "faq" else "#a9b1c6", font=bold)
        for line_no, line in enumerate(wrap(reason, small, width - pad * 2 - 90)[:2]):
            draw.text((pad + 62, y + 42 + line_no * 17), line, fill="#9aa3b5", font=small)
        y += card_h

    draw.text((pad, height - 62), "HTML собирает код: типографика, inline SVG, подписи с "
                                  "источниками, schema.org Article + FAQPage",
              fill="#6b7280", font=small)
    draw.text((pad, height - 38), "блоки: " + ", ".join(sorted({block["type"] for block in
                                                                spec["blocks"]})),
              fill="#4b5563", font=small)
    path = os.path.join(OUT, "demo-article-structure.png")
    image.save(path)
    return path


def main() -> None:
    built = build()
    spec, checks, unique = built["spec"], built["checks"], built["uniqueness"]
    page = poster(spec, checks, unique)
    print(f"статья: {spec['seo']['title']}")
    print(f"  слов {spec['stats']['words']}, чтение {spec['stats']['reading_minutes']} мин, "
          f"визуалов {spec['stats']['visuals']}, блоков {spec['stats']['blocks']}")
    plan_heads = [row["heading"] for row in built["report"]["plan"]]
    for row in built["report"]["materials"]:
        head = plan_heads[row["section"] - 1] if row["section"] else "—"
        print(f"  · {head[:44]:44} {row['type']:10} {row['reason'] or ''}")
    print(f"проверки: {'ок' if checks['ok'] else checks['issues']}")
    print(f"уникальность: {unique['duplicate_ratio']} при пороге {unique['limit']} "
          f"({unique['shingles']} шинглов)")
    print(f"HTML: {built['page']}")
    print(f"постер: {page}")


if __name__ == "__main__":
    main()
