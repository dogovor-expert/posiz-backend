"""Демо этапа G: recycle топ-поста, RU/EN-версии и видео-отчёт агентства.

Скрипт проходит путь целиком на демо-данных, но **не подменяет** логику: кандидаты, ролик,
локализация и отчёт собираются теми же модулями, что в проде
(`media_recycle`, `media_reel.localize`, `media_report`).

Про растр: продуктовый путь — `media_raster` (Chromium/rsvg). В песочнице браузера нет, поэтому
скрипт сначала пробует продуктовый растеризатор, а если его нет — рисует через cairosvg (тоже
настоящий SVG-рендерер, а не «нарисованная картинка»). Это подмена инструмента, а не результата.

Запуск (из 40-BACKEND):
    PYTHONPATH=src DATA_LAYER=memory python scripts/demo_recycle.py
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from orbita import media_locale, media_raster, media_recycle, media_reel, media_report, store  # noqa: E402

OUT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..",
                                   "20-DESIGN", "video", "backend"))
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
CANVAS = {"9x16": (1080, 1920), "16x9": (1920, 1080), "1x1": (1080, 1080), "4x5": (1080, 1350)}
TILE_H = 620
WS = "ws_de"

PUBLICATIONS = [
    ("pub_1", "Минус 20% на всё до 30 ноября",
     "Скидка 20% на весь каталог до 30 ноября.\n"
     "• Скидка действует на всё, кроме товаров с финальной ценой\n"
     "• Бесплатная доставка от 3000 ₽\n"
     "• Промокод NOV20 суммируется с бонусами",
     "vk", 5200, 210, 140, 6),
    ("pub_2", "Как выбрать подрядчика и не потерять деньги",
     "Три критерия, которые экономят месяцы. Смотрите на договор, а не на презентацию.",
     "max", 800, 12, 3, 12),
    ("pub_3", "Пять ошибок в отчётности", "Короткий пост про ошибки.", "tg", 40, 3, 0, 3),
    ("pub_4", "Пост, по которому ещё нет аналитики", "Свежая публикация.", "tg", 0, 0, 0, 1),
]


def seed() -> None:
    store.PUBLICATIONS.clear()
    store.METRICS.clear()
    for pid, title, body, channel, impressions, reactions, clicks, days_ago in PUBLICATIONS:
        variant = store.save_variant("sig_demo", channel, "post", title, body, None, False,
                                     0.9, WS)
        published = datetime.now(timezone.utc) - timedelta(days=days_ago)
        store.PUBLICATIONS[pid] = {
            "id": pid, "variant_id": variant["id"], "workspace_id": WS, "channel": channel,
            "status": "published", "media_ids": [], "is_ad": False,
            "published_at": published.isoformat(timespec="seconds"),
        }
        if impressions:
            store.METRICS.append({"publication_id": pid, "channel": channel,
                                  "impressions": impressions, "reactions": reactions,
                                  "clicks": clicks, "cost_rub": 0.0})


def render(svg: str, width: int, height: int) -> Image.Image:
    """SVG → PNG: продуктовый растеризатор, иначе cairosvg (в песочнице нет Chromium)."""
    png, backend = media_raster.rasterize(svg, width, height)
    # ImageMagick подменяет гарнитуру на serif (тот же дефект ловил гейт `media.raster`),
    # поэтому его растр для демо не годится: лучше cairosvg, который уважает font-family
    if png and backend != "imagemagick":
        return Image.open(io.BytesIO(png)).convert("RGB")
    import cairosvg

    # cairosvg идёт через «toy» API cairo и не понимает системный стек шрифтов браузера:
    # заменяем его на установленную гарнитуру, иначе весь текст демо уезжает в serif.
    svg = svg.replace("-apple-system, Segoe UI, Roboto, Arial, sans-serif",
                      "DejaVu Sans")
    raw = cairosvg.svg2png(bytestring=svg.encode("utf-8"),
                           output_width=width, output_height=height)
    return Image.open(io.BytesIO(raw)).convert("RGB")


def sheet(rows: list[tuple[str, str, Image.Image]], path: str, *, title: str) -> str:
    """Контактный лист: кадры в ряд, подписи сверху — по одной строке на сцену."""
    pad, header = 16, 92
    width = pad + len(rows) * (int(TILE_H * 9 / 16) + pad)
    canvas = Image.new("RGB", (width, header + TILE_H + pad), "#0d1117")
    draw = ImageDraw.Draw(canvas)
    big = ImageFont.truetype(FONT_BOLD, 18)
    small = ImageFont.truetype(FONT, 14)
    draw.text((pad, 12), title, fill="#e6edf3", font=big)
    for index, (scene, note, picture) in enumerate(rows):
        x = pad + index * (int(TILE_H * 9 / 16) + pad)
        draw.text((x, 42), f"{index + 1}. {scene}", fill="#e6edf3", font=small)
        draw.text((x, 64), note, fill="#8b949e", font=small)
        canvas.paste(picture.resize((int(TILE_H * 9 / 16), TILE_H), Image.LANCZOS), (x, header))
    canvas.save(path, optimize=True)
    return path


def scene_frames(spec: dict) -> list[tuple[str, str, Image.Image]]:
    """Кадр из каждой сцены — та же выборка моментов, что у `media_reel.storyboard`."""
    width, height = CANVAS.get(spec.get("format") or "9x16", CANVAS["9x16"])
    rows = []
    for scene in media_reel.timeline(spec):
        moment = round(scene["start"] + scene["duration"] * 0.75, 3)
        picture = render(media_reel.frame_at(spec, moment), width, height)
        rows.append((scene["template"], f"{moment:.1f} с", picture))
    return rows


def encode_mp4(spec: dict, path: str, *, fps: int | None = None) -> dict | None:
    """Кадры → MP4 через ffmpeg. Возвращает размеры/длительность для отчёта.

    fps берётся **из спеки**, а не из аргумента по умолчанию: 391 кадр при 25 fps вместо 30
    дал бы 15,6 с вместо 13,0 — ролик замедлился бы на 20 %, а длительность в ответе API
    осталась бы прежней.
    """
    fps = int(fps or spec.get("fps") or 30)
    ffmpeg = os.path.expanduser("~/.local/bin/ffmpeg")
    if not os.path.exists(ffmpeg):
        ffmpeg = "ffmpeg"
    width, height = CANVAS.get(spec.get("format") or "9x16", CANVAS["9x16"])
    total = media_reel.frames_count(spec)
    started = time.time()
    with tempfile.TemporaryDirectory(prefix="recycle-frames-") as tmp:
        for index in range(total):
            moment = round(index / fps, 4)
            render(media_reel.frame_at(spec, moment), width, height).save(
                os.path.join(tmp, f"{index:04d}.png"))
        cmd = [ffmpeg, "-y", "-loglevel", "error", "-framerate", str(fps),
               "-i", os.path.join(tmp, "%04d.png"), "-c:v", "libx264", "-preset", "medium",
               "-crf", "22", "-pix_fmt", "yuv420p", "-movflags", "+faststart", path]
        subprocess.run(cmd, check=True, timeout=900)
    return {"path": path, "frames": total, "fps": fps,
            "size_kb": round(os.path.getsize(path) / 1024, 1),
            "seconds": round(time.time() - started, 1)}


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    seed()

    listed = media_recycle.candidates()
    print("кандидаты:", [(row["publication_id"], row["engagement"]) for row in listed["rows"]])
    print("отказы:", [(row["publication_id"], row["reason"][:34]) for row in listed["skipped"]])

    started = time.time()
    recycled = media_recycle.recycle()
    assert recycled["ok"], recycled
    ru = recycled["spec"]
    origin = ru["origin"]
    print(f"recycle: {origin['publication_id']} · {origin['impressions']} показов · "
          f"ER {origin['engagement']:.1%} · {ru['duration']} с · {recycled['spec_hash']}")

    localized = media_reel.localize(ru, locale="en", texts={
        "headline": "20% off storewide until Nov 30",
        "eyebrow": "vk · top post",
        "items": ["20% off the whole catalogue",
                  "Free delivery over 3,000 RUB",
                  "Promo code NOV20 stacks with bonuses"],
        "cta": "Read the full post",
    })
    assert localized["ok"], localized["issues"]
    en = localized["spec"]
    print(f"en: {en['duration']} с (ru {ru['duration']} с), язык в спеке: {en['locale']}")

    report = media_report.build(days=30, render=False)
    assert report["ok"], report
    print(f"отчёт: {report['numbers']['impressions']} показов, "
          f"{report['numbers']['publications']} публикации, ER {report['numbers']['engagement']:.1%}")

    sheet(scene_frames(ru), os.path.join(OUT, "recycle-storyboard.png"),
          title="Recycle: топ-пост из аналитики → ролик (ru)")
    sheet(scene_frames(en), os.path.join(OUT, "recycle-en-storyboard.png"),
          title="Та же спека на английском: тайминги пересчитаны профилем языка (en)")
    sheet(scene_frames(report["spec"]), os.path.join(OUT, "report-storyboard.png"),
          title="Видео-отчёт агентства: цифры периода, каждая с источником")

    if os.getenv("DEMO_SKIP_VIDEO"):
        video = None
        print("видео пропущено (DEMO_SKIP_VIDEO=1)")
    else:
        video = encode_mp4(ru, os.path.join(OUT, "recycle.mp4"))
        print("видео:", video)

    demo = {
        "candidates": {"threshold": listed["threshold"], "days": listed["days"],
                       "rows": listed["rows"], "skipped": listed["skipped"],
                       "counts": listed["counts"]},
        "recycle": {"publication_id": origin["publication_id"], "origin": origin,
                    "spec_hash": recycled["spec_hash"], "duration": ru["duration"],
                    "post": recycled["post"], "checks": recycled["checks"]["ok"]},
        "locales": {"ru": {"duration": ru["duration"],
                           "scenes": [scene["duration"] for scene in ru["scenes"]]},
                    "en": {"duration": en["duration"],
                           "scenes": [scene["duration"] for scene in en["scenes"]],
                           "profile": media_locale.profile("en")["words_per_second"]},
                    "changes": localized["changes"]},
        "report": {"numbers": report["numbers"], "post": report["post"],
                   "sources": report["sources"], "spec_hash": report["spec_hash"]},
        "video": video,
        "built_in_seconds": round(time.time() - started, 1),
    }
    path = os.path.join(OUT, "recycle-demo.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(demo, handle, ensure_ascii=False, indent=2, default=str)
        handle.write("\n")
    print("отчёт:", path)


if __name__ == "__main__":
    main()
