"""Демо этапа F: три первых кадра одного ролика + честный победитель.

Скрипт повторяет весь путь лаборатории на реальном посте:
1) строит варианты (различается только `headline` первой сцены);
2) рендерит **первый кадр** каждого варианта — то, что зритель решает досматривать или нет;
3) записывает метрики (приращениями, как это делает аналитика канала);
4) принимает решение и складывает отчёт в JSON.

Запуск (из 40-BACKEND):
    PYTHONPATH=src DATA_LAYER=memory python scripts/demo_hook_lab.py
"""
from __future__ import annotations

import io
import json
import os
import sys

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from orbita import media_lab, media_raster, media_reel  # noqa: E402

OUT = os.path.join(os.path.dirname(__file__), "..", "..", "20-DESIGN", "video", "backend")
HOOK_MOMENT = 1.9          # внутри первой сцены: хук уже виден, анимация ещё идёт
TILE_W, TILE_H = 405, 720  # 9:16 в масштабе 0.375 — читаемо в превью и не тяжело
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"        # кириллица в подписях листа
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

CANVAS_W, CANVAS_H = 1080, 1920   # кадр 9:16, под который свёрстан SVG

POST = {
    "title": "Минус 20% на всё до 30 ноября",
    "eyebrow": "ТехноЛайн · распродажа",
    "footer": "tehnolain.example",
    "items": [
        "Скидка 20% на весь каталог",
        "Бесплатная доставка от 3000 ₽",
    ],
    "cta": "Забрать скидку",
}
BRAND = {"theme": "dark", "font": "sans"}

#: метрики «как из аналитики»: показы, досмотры до конца, средний досмотр в секундах
METRICS = {
    "var_1": {"views": 420, "completions": 118, "avg_watch_seconds": 6.4},
    "var_2": {"views": 405, "completions": 236, "avg_watch_seconds": 10.1},
    "var_3": {"views": 412, "completions": 121, "avg_watch_seconds": 6.6},
}


def frame_image(spec: dict, moment: float) -> Image.Image:
    """Первый кадр варианта в PNG — тем же рендерером, что и видео.

    Растеризуем в полном размере кадра и уменьшаем: SVG свёрстан под 1080x1920,
    и растр «сразу мелким» обрезал бы текст по краям.
    """
    svg = media_reel.frame_at(spec, moment)
    png, _backend = media_raster.rasterize(svg, CANVAS_W, CANVAS_H)
    # ImageMagick подменяет гарнитуру на serif — для демо берём cairosvg (см. demo_recycle)
    if png and _backend != "imagemagick":
        image = Image.open(io.BytesIO(png)).convert("RGB")
    else:
        # в песочнице без Chromium — cairosvg; «toy» API cairo не знает системный стек
        # шрифтов браузера, поэтому подставляем установленную гарнитуру
        import cairosvg

        raw = cairosvg.svg2png(
            bytestring=svg.replace("-apple-system, Segoe UI, Roboto, Arial, sans-serif",
                                   "DejaVu Sans").encode("utf-8"),
            output_width=CANVAS_W, output_height=CANVAS_H)
        image = Image.open(io.BytesIO(raw)).convert("RGB")
    return image.resize((TILE_W, TILE_H), Image.LANCZOS)


def sheet(rows: list[tuple[str, str, Image.Image, str]]) -> str:
    """Контактный лист: кадры в ряд, подпись сверху (стратегия + метрика победителя)."""
    pad, header = 16, 78
    width = pad + len(rows) * (TILE_W + pad)
    height = header + TILE_H + pad
    canvas = Image.new("RGB", (width, height), "#0d1117")
    draw = ImageDraw.Draw(canvas)
    small = ImageFont.truetype(FONT, 15)
    title_font = ImageFont.truetype(FONT_BOLD, 17)
    for index, (title, hook, png, note) in enumerate(rows):
        x = pad + index * (TILE_W + pad)
        draw.text((x, 10), f"{index + 1}. {title}", fill="#e6edf3", font=title_font)
        draw.text((x, 32), hook, fill="#8b949e", font=small)
        draw.text((x, 52), note, font=small,
                  fill="#3fb950" if "победитель" in note else "#8b949e")
        canvas.paste(png, (x, header))
    path = os.path.join(OUT, "reel-hooks-storyboard.png")
    canvas.save(path, optimize=True)
    return path


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    built = media_lab.build_lab(POST, brand=BRAND, fmt="9x16", fps=30, count=3)
    print(f"lab {built['id']}: requested={built['requested']} built={built['built']} "
          f"skipped={[row['strategy'] for row in built['skipped']]} статус={built['status']}")

    for variant in built["variants"]:
        print(f"  {variant['id']} [{variant['strategy']}] {variant['hook']!r} "
              f"{variant['duration']}с {variant['spec_hash']}")

    for variant_id, row in METRICS.items():
        media_lab.record_metrics(built["id"], variant_id, **row)

    summary = media_lab.table(built["id"])
    print("решение до фиксации:", summary["decision"]["status"], summary["decision"].get("gap"))

    result = media_lab.decide(built["id"])
    assert result["ok"], result
    # повторное решение возвращает того же победителя — идемпотентность
    again = media_lab.decide(built["id"])
    assert again["reused"] and again["decision"]["winner_variant_id"] == result["decision"]["winner_variant_id"]

    winner_id = result["decision"]["winner_variant_id"]
    rows = []
    for variant in built["variants"]:
        note = (f"победитель: {result['decision']['metric']:.0%} досмотра"
                if variant["id"] == winner_id else "проиграл по досмотру")
        rows.append((variant["strategy_title"], variant["hook"],
                     frame_image(variant["spec"], HOOK_MOMENT), note))
    path = sheet(rows)
    print("контактный лист:", os.path.abspath(path))

    report = {
        "lab_id": built["id"], "format": built["format"], "fps": built["fps"],
        "shared_spec_hash": built["shared_spec_hash"], "base_headline": built["base_headline"],
        "variants": [{key: value for key, value in variant.items() if key != "spec"}
                     for variant in built["variants"]],
        "skipped": built["skipped"],
        "table": summary["rows"], "decision": result["decision"],
        "winner_spec_hash": media_lab.lab(built["id"])["winner_spec_hash"],
        "idempotent": again["reused"],
    }
    report_path = os.path.join(OUT, "hook-lab-demo.json")
    with open(report_path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print("отчёт:", os.path.abspath(report_path))
    print("summary:", media_lab.summary())


if __name__ == "__main__":
    main()
