"""Демо этапа I2: одна тема — два пакета правил и два шаблона.

Что показывает скрипт:

1. **Ниша — данные.** Та же тема собирается дважды: с базовым пакетом (`default`) и с пакетом
   «темы с высокой ценой ошибки» (`high-stakes`). Второй добавляет оговорку отдельным блоком,
   поднимает минимум объёма до 500 слов, требует источники у сравнений и запрещает обещания
   результата — без единой ветки в коде.
2. **Шаблон — токены вёрстки.** `longread` меняет фон, антикву, шифр акцента и ширину колонки;
   акцент доезжает и до inline-SVG схемы, потому что рисунок берёт цвет из шаблона.
3. **Политика проверяема.** `check()` возвращает то же самое, что увидит редактор: `too_few_words`
   с новым минимумом, `banned_phrase`, `block_required`, `source_missing`.
4. **Отказ честный.** Неизвестный пакет или шаблон — код `pack_unknown`/`template_unknown` со
   списком реестра, а не «соберём как-нибудь по умолчанию».

Запуск (из 40-BACKEND):
    PYTHONPATH=src DATA_LAYER=memory python scripts/demo_article_packs.py
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from orbita import articles  # noqa: E402

OUT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..",
                                   "20-DESIGN", "articles", "backend"))

TOPIC = "Проверка договора перед подписанием: что смотреть и сколько это стоит"

MATERIALS = [
    {"kind": "numbers", "code": "terms", "title": "Сроки проверки", "unit": " дн.",
     "source": "регламент клиента, ноябрь 2026",
     "values": [{"label": "базовый", "value": 2}, {"label": "полный", "value": 4},
                {"label": "срочный", "value": 1}]},
]

OUTLINE = [
    {"heading": "Где риски в договоре",
     "callout": ("Оговорка: протокол проверки — мнение, а не гарантия исхода спора. "
                 "Итог зависит от суда и обстоятельств."),
     "text": (
        "Риск в договоре почти никогда не в одном пункте: он складывается из связки условий. "
        "Первое, что смотрим, — кто и когда может изменить цену в одностороннем порядке. "
        "Второе — ответственность: часто она ограничена суммой договора на стороне заказчика, "
        "а на стороне исполнителя — «фактическим ущербом», который невозможно посчитать. "
        "Третье — расторжение: если срок уведомления тридцать дней, а аванс уже перечислен, "
        "деньги возвращаются через суд, а не письмом. Четвёртое — подсудность и порядок "
        "претензий: пункт, по которому спор рассматривает суд другого региона, дороже любой "
        "правки. И пятое, самое частое: условия, которые в договоре нет, но есть в приложении, "
        "на которое договор ссылается. Поэтому проверка начинается с приложений, а не с "
        "первой страницы.")},
    {"heading": "Сколько стоит проверка и от чего зависит цена",
     "columns": ["Пакет", "Что входит", "Цена", "Срок"],
     "rows": [["Базовый", "договор до 20 страниц, протокол рисков", "9 000 ₽", "2 дня"],
              ["Полный", "договор, приложения, протокол и правки", "24 000 ₽", "4 дня"],
              ["Срочный", "только критические риски", "15 000 ₽", "1 день"]],
     "source": "прайс клиента, ноябрь 2026",
     "text": (
        "Цена проверки зависит не от объёма договора, а от количества связанных документов. "
        "Договор с одним приложением проверяется по базовому пакету: смотрим условия, считаем "
        "риски, отдаём протокол. Полный пакет нужен, когда к договору приложены регламенты, "
        "тарифы и правила, которые меняются без подписи: там риск живёт в ссылках. Срочный "
        "пакет — не «быстрее за деньги», а уже: фиксируем только то, что может стоить денег "
        "или суда, остальное оставляем на потом, и это прямо написано в протоколе. Если "
        "договор пришёл в пятницу, а подписать нужно в понедельник, честный ответ — срочный "
        "пакет с явно очерченной границей, а не «полный, но за сутки».")},
    {"heading": "Как проходит проверка", "material": "terms", "chart_kind": "bar",
     "steps": ["Читаем договор", "Смотрим приложения", "Отмечаем риски",
               "Пишем протокол"],
     "text": (
        "Проверка идёт в четыре шага, и каждый заканчивается артефактом. Сначала читаем сам "
        "договор: структура, обязательства, цена, сроки. Потом — приложения и регламенты: "
        "именно там живут изменения, которые не подписывали отдельно. Затем отмечаем риски "
        "по тяжести: «остановит сделку», «дороже денег», «принять можно». И в конце пишем "
        "протокол — не список замечаний, а рекомендацию: что менять обязательно, что можно "
        "принять с оговоркой, а что не имеет смысла менять, потому что цена правки выше "
        "риска. Клиент получает не «много важных слов», а решение по каждому пункту.")},
    {"heading": "Что остаётся у клиента",
     "columns": [{"title": "Своими силами", "items": ["дешевле", "риск пропустить связку",
                                                       "нет протокола для руководства"]},
                 {"title": "С проверкой", "items": ["протокол с приоритетами",
                                                     "правки в договоре",
                                                     "оценка цены каждого риска"]}],
     "source": "регламент клиента, ноябрь 2026",
     "text": (
        "После проверки у клиента остаётся протокол: документ, который можно показать "
        "руководству и на который можно ссылаться в переговорах. В нём три вещи: перечень "
        "рисков с ценой каждого, рекомендация по каждому пункту и список того, что мы "
        "сознательно не меняли. Последнее важнее, чем кажется: договор без единой правки "
        "выглядит недоверчиво, а протокол объясняет, почему часть условий принята. Если "
        "договор правят на стороне контрагента, протокол работает как чек-лист проверки "
        "новой редакции — не нужно читать всё заново, чтобы понять, что изменилось.")},
    {"heading": "Как выглядит протокол",
     "image_prompt": "Протокол проверки договора: риски, приоритеты, рекомендации",
     "text": (
        "Протокол — одна страница на десять пунктов, не больше. Слева риск и пункт договора, "
        "справа — приоритет и рекомендация, внизу — что делать, если контрагент не согласен. "
        "Формат держится специально: если протокол длиннее, его не читают, а значит, проверка "
        "не работает. Поэтому в отчёте нет юридических конструкций ради красоты: только то, "
        "что клиент может произнести на переговорах своими словами и что можно проверить "
        "через день после правки.")},
]

BRIEF = {
    "topic": TOPIC,
    "seo_title": "Проверка договора перед подписанием: риски и цена",
    "keywords": ["проверка договора", "риски договора", "стоимость проверки"],
    "sections": 5,
    "lead": ("Проверка договора перед подписанием: где риски, сколько стоит и что остаётся у "
             "клиента после проверки — с протоколом рисков и ценой каждого из них."),
    "outline": OUTLINE,
    "materials": MATERIALS,
    "summary": ["Риск живёт в связке условий и приложениях",
                "Цена проверки зависит от связанных документов",
                "Протокол — рекомендация, а не список замечаний"],
    "faq": [
        {"q": "Можно ли проверить только один раздел?",
         "a": "Да, но тогда протокол фиксирует границу проверки: остальные разделы "
              "считаются принятыми без анализа."},
        {"q": "Что если контрагент не согласен с правками?",
         "a": "В протоколе есть колонка «если не согласны»: какие риски можно принять "
              "с оговоркой, а какие — нет."},
    ],
    "cta": "Заказать проверку договора",
    "cta_url": "/pricing",
}


def build(brief: dict, name: str) -> dict:
    result = articles.compose(brief, materials=MATERIALS)
    if not result.get("ok"):
        raise SystemExit(f"статья не собралась: {result}")
    spec = result["spec"]
    page = os.path.join(OUT, f"demo-article-{name}.html")
    with open(page, "w", encoding="utf-8") as handle:
        handle.write(articles.render_html(spec))
    with open(os.path.join(OUT, f"demo-article-{name}.spec.json"), "w",
              encoding="utf-8") as handle:
        json.dump(spec, handle, ensure_ascii=False, indent=2)
    return {"spec": spec, "checks": result["checks"], "page": page}


def _wrap(draw, text: str, face, limit: int) -> list[str]:
    """Перенос по словам: строка не обрывается на середине слова."""
    chunks, line = [], ""
    for word in str(text).split():
        probe = f"{line} {word}".strip()
        if draw.textlength(probe, font=face) > limit and line:
            chunks.append(line)
            line = word
        else:
            line = probe
    if line:
        chunks.append(line)
    return chunks


def poster(base: dict, strict: dict) -> str:
    """Два столбца: что изменили пакет правил и шаблон. Видно без открытия HTML."""
    from PIL import Image, ImageDraw, ImageFont

    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 15)
    small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 13)
    bold = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 18)
    title_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 26)

    width, pad, col_w = 1240, 36, 570
    height = 640
    image = Image.new("RGB", (width, height), "#0f1117")
    draw = ImageDraw.Draw(image)
    draw.text((pad, 30), "Одна тема — два пака правил и два шаблона", fill="#ffffff",
              font=title_font)
    draw.text((pad, 72), "Ниша подключается данными: ARTICLE_RULE_PACKS и ARTICLE_TEMPLATES",
              fill="#7c5cff", font=small)

    for index, (title, row) in enumerate((("default · editorial", base),
                                          ("high-stakes · longread", strict))):
        spec, checks = row["spec"], row["checks"]
        tokens = articles.template_tokens(spec["template"])
        policy = spec["policy"] or {}
        x = pad + index * (col_w + 28)
        y = 110
        draw.rounded_rectangle((x, y, x + col_w, height - 44), radius=14, fill="#161a24",
                               outline="#232734")
        draw.text((x + 20, y + 18), title, fill="#ffffff", font=bold)
        draw.rectangle((x + 20, y + 50, x + 120, y + 86), fill=tokens["accent"])
        draw.text((x + 132, y + 50), f'акцент {tokens["accent"]}', fill="#e5e7eb", font=small)
        draw.text((x + 132, y + 68), f'фон {tokens["bg"]} · ширина {tokens["max_width"]}',
                  fill="#9aa3b5", font=small)

        lines = [
            f'пакет: {spec["rule_pack"]}   шаблон: {spec["template"]}',
            f'слов: {spec["stats"]["words"]}   визуалов: {spec["stats"]["visuals"]}',
            f'врезок: {sum(1 for b in spec["blocks"] if b["type"] == "callout")}   '
            f'блоков в статье: {len(spec["blocks"])}',
            f'минимум объёма: {policy.get("min_words") or articles.MIN_WORDS}',
            f'порог уникальности: {policy.get("max_duplicate_ratio") or articles.MAX_DUPLICATE_RATIO}',
            f'оговорка: {"да" if policy.get("disclaimer") else "нет"}',
            f'обязательные блоки: {", ".join(policy.get("required_blocks") or []) or "—"}',
            f'запрещено: {", ".join(policy.get("banned_phrases") or []) or "—"}',
            f'проверки: {"без замечаний" if checks["ok"] else str(len(checks["issues"])) + " замечаний"}',
        ]
        row_y = y + 110
        for text in lines:
            # длинные строки (запрещённые фразы) переносятся по словам, а не обрезаются
            for chunk in _wrap(draw, text, small, col_w - 40):
                draw.text((x + 20, row_y), chunk, fill="#c9cfdb", font=small)
                row_y += 21
            row_y += 3
        bar = "#8be9c0" if checks["ok"] else "#ffb4a2"
        draw.text((x + 20, height - 76), "· готово к публикации" if checks["ok"]
                  else "· черновик", fill=bar, font=font)

    path = os.path.join(OUT, "demo-article-packs.png")
    image.save(path)
    return path


def main() -> None:
    base = build({**BRIEF}, "default")
    strict = build({**BRIEF, "rule_pack": "high-stakes", "template": "longread"}, "strict")
    page = poster(base, strict)

    for name, row in (("default/editorial", base), ("high-stakes/longread", strict)):
        spec, checks = row["spec"], row["checks"]
        print(f"{name}: {spec['stats']['words']} слов, {spec['stats']['visuals']} визуалов, "
              f"врезок {sum(1 for b in spec['blocks'] if b['type'] == 'callout')}, "
              f"проверки {'ок' if checks['ok'] else [i['code'] for i in checks['issues']]}")
    print(f"паки: {[row['code'] for row in articles.rule_packs()]}, "
          f"шаблоны: {[row['code'] for row in articles.templates()]}")
    print(f"неизвестный пакет: {articles.plan({'topic': 'Тема', 'rule_pack': 'nope'})['code']}")
    print(f"HTML: {base['page']}")
    print(f"HTML: {strict['page']}")
    print(f"постер: {page}")


if __name__ == "__main__":
    main()
