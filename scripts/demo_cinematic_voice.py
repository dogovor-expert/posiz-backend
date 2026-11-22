"""Демо этапа H: кинематографичная озвучка с драматическими паузами.

Что показывает скрипт:

1. **Подача как данные.** Ролик собирается с `performance="cinematic-baritone"`: паузы ×1,45,
   темп 0,95, у ElevenLabs это stability 40 % / style 20 %.
2. **Паузы — время в кадре.** Длительности сцен включают паузы по пунктуации (тире, многоточие,
   конец фразы), поэтому титры не отстают от диктора и не обгоняют его.
3. **Ударения не попадают в кадр.** В тексте для синтеза стоит `Дагест+ана`, в кадре — «Дагестана».
4. **Провайдеры и цены.** Одна и та же фраза: что применилось у ElevenLabs, что у SpeechKit
   попало в `skipped`, сколько стоит каждый вариант.

Озвучка в демо подана как файл (`provider="file"`): в песочнице нет ни ключа ElevenLabs, ни
SteosVoice. Это тот же путь, что в проде — меняется только источник аудио, а не конвейер:
синтез → выравнивание → подгонка таймлайна → кадры → MP4.

Запуск (из 40-BACKEND):
    PYTHONPATH=src DATA_LAYER=memory python scripts/demo_cinematic_voice.py
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from orbita import media_locale, media_reel, media_voice  # noqa: E402

OUT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..",
                                   "20-DESIGN", "video", "backend"))
AUDIO = os.path.join(OUT, "cinematic-narration.mp3")
FPS = 25
CANVAS = (1080, 1920)

PERFORMANCE = "cinematic-baritone"
#: ровно тот текст, что озвучен в демо-файле: тайминги титров считаются по нему
TEXT = ("Он приехал в горы Дагестана — и остался навсегда. "
        "Здесь нет ни дорог, ни связи. Только камень, ветер — и тишина, которая громче любого "
        "слова. Именно так начинается история, о которой не рассказывают в путеводителях.")
STRESS = {"Дагестана": "Дагест+ана"}
POST = {
    "title": "Он приехал в горы Дагестана — и остался навсегда",
    "eyebrow": "История · Дагестан",
    "items": ["Высота 2 000 метров — и тишина",
              "Дорога занимает четыре часа",
              "Связи нет совсем"],
    "note": "Так начинается история, о которой не рассказывают в путеводителях.",
    "footer": "posiz.ru",
    "cta": "Смотреть целиком",
}


def render(svg: str, width: int, height: int):
    """SVG → PNG. В песочнице без Chromium — cairosvg (ImageMagick портит гарнитуру)."""
    from PIL import Image

    from orbita import media_raster

    png, backend = media_raster.rasterize(svg, width, height)
    if png and backend != "imagemagick":
        return Image.open(io.BytesIO(png)).convert("RGB")
    import cairosvg

    raw = cairosvg.svg2png(
        bytestring=svg.replace("-apple-system, Segoe UI, Roboto, Arial, sans-serif",
                               "DejaVu Sans").encode("utf-8"),
        output_width=width, output_height=height)
    return Image.open(io.BytesIO(raw)).convert("RGB")


def sheet(spec: dict, path: str) -> str:
    """Контактный лист ключевых кадров с подписями «сцена + время + пауза»."""
    from PIL import Image, ImageDraw, ImageFont

    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 15)
    bold = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 17)
    tile = (405, 720)
    rows = media_reel.timeline(spec)
    pad, header = 16, 78
    width = pad + len(rows) * (tile[0] + pad)
    canvas = Image.new("RGB", (width, header + tile[1] + pad), "#0d1117")
    draw = ImageDraw.Draw(canvas)
    draw.text((pad, 10), "Кинематографичная озвучка: паузы диктора — это время в кадре",
              fill="#e6edf3", font=bold)
    for index, row in enumerate(rows):
        scene = spec["scenes"][index]
        moment = round(row["start"] + row["duration"] * 0.8, 3)
        x = pad + index * (tile[0] + pad)
        draw.text((x, 40), f"{index + 1}. {scene['template']} · {moment:.1f} с",
                  fill="#e6edf3", font=font)
        draw.text((x, 58), f"сцена {scene['duration']:.2f} с (с паузами ×1,45)",
                  fill="#8b949e", font=font)
        canvas.paste(render(media_reel.frame_at(spec, moment), *CANVAS).resize(tile), (x, header))
    canvas.save(path, optimize=True)
    return path


def encode(spec: dict, audio: str | None, path: str) -> dict:
    """Кадры → MP4 (+ аудио, если оно есть). fps берём из спеки, а не «по привычке»."""
    fps = int(spec.get("fps") or FPS)
    ffmpeg = os.path.expanduser("~/.local/bin/ffmpeg")
    if not os.path.exists(ffmpeg):
        ffmpeg = "ffmpeg"
    total = media_reel.frames_count(spec)
    started = time.time()
    with tempfile.TemporaryDirectory(prefix="cinematic-") as tmp:
        for index in range(total):
            render(media_reel.frame_at(spec, round(index / fps, 4)), *CANVAS).save(
                os.path.join(tmp, f"{index:04d}.png"))
        cmd = [ffmpeg, "-y", "-loglevel", "error", "-framerate", str(fps),
               "-i", os.path.join(tmp, "%04d.png")]
        if audio:
            cmd += ["-i", audio, "-c:a", "aac", "-b:a", "128k", "-shortest"]
        cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "22",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart", path]
        subprocess.run(cmd, check=True, timeout=1800)
    return {"path": path, "frames": total, "fps": fps,
            "size_kb": round(os.path.getsize(path) / 1024, 1),
            "seconds": round(time.time() - started, 1)}


def providers_table() -> dict:
    """Что применилось у каждого движка для этой подачи — часть отчёта, а не «на словах»."""
    rows = {}
    for code in ("elevenlabs", "steosvoice", "yandex", "local"):
        state = media_voice.settings_for(code, performance=PERFORMANCE)
        row = media_voice.PROVIDERS[code]
        rows[code] = {
            "title": row["title"], "audience": row.get("audience"),
            "expressiveness": row.get("expressiveness"),
            "stress": bool(row.get("stress")),
            "applied": state["applied"], "skipped": state["skipped"],
            # смета за одну и ту же фразу: 105 знаков
            "cost_rub_per_1000": media_voice.price_per_1000(code),
        }
    return rows


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    if not os.path.exists(AUDIO):
        raise SystemExit(f"нет демо-озвучки {AUDIO}: положите mp3 рядом или сгенерируйте её")

    plan = media_voice.synthesize(TEXT, provider="file", path=AUDIO,
                                  performance=PERFORMANCE, stress=STRESS)
    if not plan.get("ok"):
        raise SystemExit(f"озвучка не собралась: {plan.get('reason')}")

    spec = media_reel.build_reel_spec(POST, fps=FPS, performance=PERFORMANCE)
    spec = media_reel.apply_voice(spec, plan)
    report = media_reel.check(spec)
    assert report["ok"], report["issues"]

    storyboard = []
    for scene, row in zip(spec["scenes"], media_reel.timeline(spec)):
        moment = round(row["start"] + row["duration"] * 0.8, 3)
        storyboard.append({"t": moment, "template": scene["template"],
                           "duration": scene["duration"],
                           "headline": (scene.get("props") or {}).get("headline") or
                                       (scene.get("props") or {}).get("cta") or ""})

    image = sheet(spec, os.path.join(OUT, "reel-cinematic-storyboard.png"))
    video = None if os.getenv("DEMO_SKIP_VIDEO") else encode(
        spec, AUDIO, os.path.join(OUT, "reel-cinematic.mp4"))

    demo = {
        "performance": PERFORMANCE,
        "profile": media_voice.PERFORMANCES[PERFORMANCE],
        "audio": {"duration": plan["duration"], "chars": plan["chars"],
                  "cost_rub": plan["cost_rub"], "cost_cloud_rub": plan["cost_cloud_rub"],
                  "alignment": plan["aligned"], "rate": plan["rate"]},
        "pauses": plan["pauses"],
        "pause_seconds": plan["pause_seconds"],
        "stress": {"marks": STRESS, "spoken": plan["spoken_text"],
                   "screen": plan["screen_text"]},
        "spec": {"duration": spec["duration"], "fps": spec["fps"],
                 "scenes": [{"template": scene["template"], "duration": scene["duration"]}
                            for scene in spec["scenes"]],
                 "spec_hash": media_reel.spec_hash(spec)},
        "storyboard": storyboard,
        "providers": providers_table(),
        "locales": {code: media_locale.profile(code)["words_per_second"]
                    for code in ("ru", "en")},
        "video": video,
    }
    path = os.path.join(OUT, "cinematic-voice-demo.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(demo, handle, ensure_ascii=False, indent=2, default=str)
        handle.write("\n")
    print("озвучка:", plan["duration"], "с,", plan["chars"], "знаков, паузы",
          plan["pause_seconds"], "с; в кадр уходит:", plan["screen_text"][:60])
    print("ролик:", spec["duration"], "с, сцены", [round(s["duration"], 2) for s in spec["scenes"]])
    print("видео:", video)
    print("отчёт:", path)


if __name__ == "__main__":
    main()
