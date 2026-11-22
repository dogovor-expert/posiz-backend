"""Демо этапа I3: статья с галереей и роликом из собственной спеки.

Что показывает скрипт:

1. **Галерея — это визуалы, а не «место под картинку».** Каждая плитка рисуется спекой
   медиа-движка (`media.build_spec` → inline-SVG в разметке), у каждой свой `alt` и подпись.
   Один элемент галереей не считается: он превращается в обычную иллюстрацию.
2. **Видео в статье — ролик из спеки статьи.** Заголовок раздела, пункты и подпись уходят в
   `media_reel.build_reel_spec`: получается `reel.spec.v2` на 16:9 с таймлайном и сторибордом.
   В статью вставляется полоса сцен с временем, а не четыре кадра 1080×1920.
3. **Лимиты проверяются.** Длительность и число сцен сверяются с `media_reel.limits()`: ролик,
   который не влезает в лимит, — замечание `video_too_long`, а не «как-нибудь отрендерим».
4. **Ссылка клиента — законный путь.** Если ролик уже готов, раздел может принести `src`:
   тогда в разметке появится `<video controls>` с субтитрами, а спека не выдумывается.

Запуск (из 40-BACKEND):
    PYTHONPATH=src DATA_LAYER=memory python scripts/demo_article_media.py
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from orbita import articles, media_reel  # noqa: E402

OUT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..",
                                   "20-DESIGN", "articles", "backend"))

TOPIC = "Один пост — четыре формата: как оформить материал без дизайнера"

TEXT_PRACTICE = (
    "Оформление поста — это не «красивая картинка», а четыре решения подряд: что читатель "
    "увидит первым, сколько времени он потратит, что он запомнит и куда пойдёт дальше. "
    "Хук решает первое, сетка — второе, акцент — третье, призыв — четвёртое. Если хотя бы "
    "одно решение принято «по вкусу», пост выглядит дорого, но не работает: читатель "
    "останавливается, кивает и уходит. Поэтому оформление начинается не с палитры, а с "
    "вопроса «что человек должен сделать после того, как увидит этот пост».")
TEXT_GALLERY = (
    "Четыре формата закрывают разные сценарии: карточка 1:1 для ленты, обложка 16:9 для "
    "видео и статьи, вертикаль 9:16 для клипов, карусель — когда мыслей больше, чем один "
    "экран. Форматы не «версии одного макета»: у каждого своя плотность текста и своя "
    "безопасная зона. Поэтому в примерах ниже одинаковый смысл подан четырьмя разными "
    "способами — и видно, что первый экран каждый раз решает разную задачу.")
TEXT_VIDEO = (
    "Ролик из поста собирается по спеке: хук печатается посимвольно, пункты появляются по "
    "одному, финал отдаёт бренд и призыв. Длительность считается по объёму текста и темпу "
    "чтения, а не «по пять секунд на сцену»: короткий пункт держится две секунды, длинный — "
    "четыре. В статье такой ролик показывается полосой сцен: видно, сколько сцен, сколько "
    "секунд и что происходит в каждой, — без вставки четырёх кадров 1080×1920.")
TEXT_TIME = (
    "Время — главный ограничитель. Пост в одном формате занимает три минуты, полный набор "
    "из четырёх форматов — двенадцать, ролик добавляет ещё десять. Это не «монтаж ради "
    "монтажа»: если у материала нет инфоповода, второй формат не приносит охвата, а забирает "
    "время, которое можно потратить на следующий пост. Поэтому в регламенте есть правило: "
    "четыре формата — для материалов, которые живут дольше недели, один формат — для всего "
    "остального.")
TEXT_CHOICE = (
    "Выбор между «только пост» и «пост плюс ролик» решается не вкусом, а тем, где живёт "
    "аудитория. Если лента — пост самодостаточен. Если клипы — без вертикали материал "
    "просто не увидят. Если почта и сайт — нужна обложка 16:9 и текст, который читается без "
    "картинки. В каждом случае сначала выбирается носитель, потом оформление, и только потом "
    "палитра: иначе получается красивый макет, который некуда положить.")

BRIEF = {
    "topic": TOPIC,
    "seo_title": "Оформление поста: четыре формата, галерея и ролик",
    "keywords": ["оформление поста", "форматы постов", "ролик из поста"],
    "sections": 5,
    "lead": ("Как один пост превращается в четыре формата и ролик: примеры оформления, "
             "спека ролика, время на каждый формат и правило выбора."),
    "outline": [
        {"heading": "Что решает оформление поста", "text": TEXT_PRACTICE},
        {"heading": "Примеры оформления в четырёх форматах", "text": TEXT_GALLERY,
         "gallery": [
             {"title": "Карточка 1:1 для ленты", "caption": "Лента: один экран, один акцент",
              "alt": "Карточка 1:1: заголовок и акцентная полоса на тёмном фоне"},
             {"title": "Обложка 16:9 для статьи", "caption": "Статья и видео: заголовок крупно",
              "alt": "Обложка 16:9: крупный заголовок и подпись рубрики"},
             {"title": "Вертикаль 9:16 для клипов", "caption": "Клипы: текст в безопасной зоне",
              "alt": "Вертикальная обложка 9:16 с текстом внутри безопасной зоны"},
         ]},
        {"heading": "Как выглядит ролик из этого поста", "text": TEXT_VIDEO,
         "video": {"items": ["Хук печатается посимвольно",
                             "Пункты появляются по одному",
                             "Финал отдаёт бренд и призыв"],
                   "note": "Ролик 16:9 из одного поста",
                   "cta": "Смотреть целиком"}},
        {"heading": "Сколько времени занимает каждый формат", "text": TEXT_TIME,
         "columns": ["Формат", "Время", "Когда нужен"],
         "rows": [["Пост 1:1", "3 минуты", "каждый день"],
                  ["Обложка 16:9", "4 минуты", "статьи и видео"],
                  ["Вертикаль 9:16", "5 минут", "клипы и сторис"],
                  ["Ролик 16:9", "10 минут", "материалы, живущие дольше недели"]],
         "source": "регламент клиента, ноябрь 2026"},
        {"heading": "Что выбрать: пост или пост с роликом", "text": TEXT_CHOICE,
         "columns": [{"title": "Только пост", "items": ["3 минуты", "хватает для ленты",
                                                        "не нужен монтаж"]},
                     {"title": "Пост и ролик", "items": ["13 минут", "шире охват",
                                                         "нужны субтитры и озвучка"]}],
         "source": "регламент клиента, ноябрь 2026"},
    ],
    "summary": ["Оформление решает четыре вопроса, а не «как красиво»",
                "Форматы отличаются плотностью текста и безопасной зоной",
                "Ролик собирается из спеки поста, а не снимается заново"],
    "faq": [
        {"q": "Нужен ли дизайнер для четырёх форматов?",
         "a": "Нет: форматы собираются из одной спеки поста, дизайнер нужен для "
              "нестандартных материалов."},
        {"q": "Сколько сцен в ролике из поста?",
         "a": "От трёх до пяти: хук, пункты, финал. Длительность считается по тексту."},
    ],
    "quote": {"text": "Оформление — это не украшение, а скорость понимания.",
              "author": "редполитика клиента"},
    "cta": "Собрать пост в четырёх форматах",
    "cta_url": "/pricing",
    "canonical": "https://example.com/blog/odin-post-chetyre-formata/",
}


def reel_frame(reel: dict, path: str) -> str | None:
    """Один кадр ролика из спеки статьи: доказательство, что спека не декоративная.

    Растр в песочнице не гарантирован (Chromium нет, cairosvg/ImageMagick — по обстоятельствам),
    поэтому отсутствие кадра — не ошибка демо, а честный ``None``.
    """
    if not reel.get("spec"):
        return None
    try:
        import io as _io

        from PIL import Image

        from orbita import media_raster, media_reel

        # кадр в конце первой сцены: хук уже допечатан, а не «на середине буквы»
        shots = reel.get("storyboard") or [{"duration": 2.0}]
        moment = round(float(shots[0].get("duration") or 2.0) * 0.9, 2)
        svg = media_reel.frame_at(reel["spec"], moment)
        png, backend = media_raster.rasterize(svg, 1280, 720)
        if not png:
            return None
        image = Image.open(_io.BytesIO(png)).convert("RGB")
        image.save(path)
        return f"{path} ({backend})"
    except Exception as exc:                      # noqa: BLE001 — демо не должно падать из-за растра
        print(f"кадр ролика не собран: {exc.__class__.__name__}: {exc}")
        return None


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    result = articles.compose(BRIEF, links=["kak-schitat-ohvat", "formaty-postov"])
    if not result.get("ok"):
        raise SystemExit(f"статья не собралась: {result}")
    spec, checks = result["spec"], result["checks"]
    html = articles.render_html(spec)

    page = os.path.join(OUT, "demo-article-media.html")
    with open(page, "w", encoding="utf-8") as handle:
        handle.write(html)
    with open(os.path.join(OUT, "demo-article-media.spec.json"), "w", encoding="utf-8") as handle:
        json.dump(spec, handle, ensure_ascii=False, indent=2)

    gallery = next((block for block in spec["blocks"] if block["type"] == "gallery"), None)
    video = next((block for block in spec["blocks"] if block["type"] == "video"), None)
    reel = (video or {}).get("props", {}).get("reel") or {}
    report = {
        "topic": TOPIC,
        "stats": spec["stats"],
        "checks": checks,
        "gallery": {"items": [{"title": item["title"], "caption": item["caption"],
                               "alt": item["alt"]} for item in (gallery or {}).get(
                                   "props", {}).get("items", [])]},
        "video": {"provider": (video or {}).get("props", {}).get("provider"),
                  "duration": reel.get("duration"), "scenes": reel.get("scenes"),
                  "format": reel.get("format"), "fps": reel.get("fps"),
                  "limits": media_reel.limits(),
                  "storyboard": reel.get("storyboard")},
        "html_bytes": len(html.encode("utf-8")),
        "inline_svg": html.count("<svg"),
    }
    with open(os.path.join(OUT, "demo-article-media-report.json"), "w",
              encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)

    print(f"статья: {spec['seo']['title']}")
    print(f"  слов {spec['stats']['words']}, визуалов {spec['stats']['visuals']}, "
          f"блоков {spec['stats']['blocks']}, inline-SVG {report['inline_svg']}")
    if gallery:
        print(f"  галерея: {len(gallery['props']['items'])} плитки — "
              + "; ".join(item["title"] for item in gallery["props"]["items"]))
    if reel:
        print(f"  ролик: {reel['scenes']} сцены, {reel['duration']} с, {reel['format']}, "
              f"{reel['fps']} fps (лимит {media_reel.limits()['max_duration']} с)")
        for shot in reel["storyboard"]:
            print(f"    · {shot['t']:>4.1f} с  {shot['template']:<14} {shot['label'][:44]}")
    frame = reel_frame(reel, os.path.join(OUT, "demo-article-media-reel-frame.png"))
    if frame:
        print(f"кадр ролика: {frame}")
    print(f"проверки: {'без замечаний' if checks['ok'] else checks['issues']}")
    print(f"HTML: {page}")


if __name__ == "__main__":
    main()
