"""SVG → PNG: растровый шаг медиа-конвейера (этап B, ADR 0023).

Зачем растр, если SVG уже картинка: VK, Telegram и MAX принимают файл, но превью в ленте
и «нативное» изображение ведут себя предсказуемо только с PNG/JPEG — SVG не
растеризуют мобильные клиенты частично или вовсе. Поэтому этап A отдавал SVG (он диффится
в тестах), а этап B добавил растр.

Кто растеризует. Мы не тащим Chromium в образ API ради карточек: растр делает внешний
бинарь, и порядок выбора — самый дешёвый из доступных:

1. ``MEDIA_RASTER_BIN`` — явное указание (нужно в проде: предсказуемость важнее автопоиска);
2. ``rsvg-convert`` — быстрый и корректный на SVG;
3. ``chromium``/``chromium-browser``/``google-chrome`` — **тот же движок, что у дизайнера в
   браузере**: градиенты, шрифты и тени совпадают с превью один в один (проверено на
   макетах из `20-DESIGN/video/`);
4. ``magick``/``convert`` (ImageMagick) — только если больше ничего нет.

Про ImageMagick отдельно: без делегата `librsvg` он рисует SVG **своим** движком — шрифт
подменяется на serif, градиенты теряются. Поэтому он последний в списке, а бэкенд
записывается в артефакт (`raster_backend`), чтобы такой артефакт было видно, а не
«внезапно некрасиво у клиента».

Про ``MEDIA_CHROME_LIBS``: Chromium из кеша playwright зависит от системных библиотек
(`libnspr4`, `libnss3`, `libatk`, …). На «тонком» образе их нет, и бинарь падает с
`error while loading shared libraries` — тогда мы **не** тихо откатываемся на ImageMagick,
а сообщаем причину в `available()["reason"]`, а каталоги с библиотеками можно передать
списком через `MEDIA_CHROME_LIBS` (через ``:``, попадают в `LD_LIBRARY_PATH` только у
дочернего процесса-растеризатора). В проде правильнее поставить зависимости в образ
(`playwright install-deps chromium`), а переменная остаётся для песочниц и CI.

Если бинаря нет — это **не ошибка**: карточка остаётся SVG, в артефакте пишется
``raster_backend: null``, а API отвечает `raster: {available: false, reason: …}`.
Так же честнее, чем «упало всё из-за отсутствия ImageMagick в песочнице».

Безопасность: аргументы передаются списком (без `shell=True`), входной SVG — это наш
собственный вывод, но на всякий случай ограничен по размеру, а результат проверяется по
сигнатуре PNG. Таймаут — чтобы «зависший» растеризатор не держал воркер.
"""
from __future__ import annotations

import glob
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

#: PNG-сигнатура: результат, который не начинается с неё, — не картинка
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
DEFAULT_TIMEOUT = 20                # секунд на один артефакт
PROBE_TIMEOUT = 10                  # секунд на проверку «бинарь живой»
_PROBE_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" width="8" height="8">'
              '<rect width="8" height="8" fill="#000"/></svg>')
MAX_INPUT = 4 * 1024 * 1024         # наш SVG — это единицы КБ

_CANDIDATES: list[tuple[str, str]] = [
    ("rsvg", "rsvg-convert"),
    ("chromium", "chromium"),
    ("chromium", "chromium-browser"),
    ("chromium", "google-chrome"),
    ("imagemagick", "magick"),
    ("imagemagick", "convert"),
]

#: ImageMagick без librsvg-делегата подменяет шрифт и теряет градиенты: помечаем как
#: «низкая точность», чтобы это было видно в артефакте, а не только на глаз
LOW_FIDELITY = {"imagemagick"}


def _explicit() -> tuple[str, str] | None:
    value = (os.getenv("MEDIA_RASTER_BIN") or "").strip()
    if not value:
        return None
    name = Path(value).name
    kind = "rsvg" if "rsvg" in name else ("chromium" if "chrom" in name or "chrome" in name
                                          else "imagemagick")
    return kind, value


def detect() -> tuple[str, str] | None:
    """Какой растеризатор доступен: ``("chromium", "/path/chrome")`` или ``None``.

    Порядок важен: сначала то, что даёт точное совпадение с браузерным превью, и только
    в конце ImageMagick (он без librsvg-делегата подменяет шрифт и теряет градиенты).
    """
    explicit = _explicit()
    if explicit:
        kind, path = explicit
        if Path(path).exists() or shutil.which(path):
            return kind, path
        return None
    for kind, binary in (("rsvg", "rsvg-convert"),
                         ("chromium", "chromium"),
                         ("chromium", "chromium-browser"),
                         ("chromium", "google-chrome")):
        if shutil.which(binary):
            return kind, binary
    cached = _playwright_chromium()
    if cached:
        return "chromium", cached
    for binary in ("magick", "convert"):
        if shutil.which(binary):
            return "imagemagick", binary
    return None


def _playwright_chromium() -> str | None:
    """Chromium из кеша playwright: в PATH его нет, но на узлах с аудитами он обычно есть."""
    for pattern in ("~/.cache/ms-playwright/chromium-*/chrome-linux64/chrome",
                    "~/.cache/ms-playwright/chromium-*/chrome-linux/chrome",
                    "~/.cache/ms-playwright/chromium_headless_shell-*/chrome-linux/headless_shell",
                    "/usr/lib/chromium/chromium"):
        for candidate in sorted(glob.glob(os.path.expanduser(pattern)), reverse=True):
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
    return None


def _probe_works(kind: str, binary: str) -> bool:
    """Проверяем, что бинарь реально запускается: «нашёлся» ≠ «работает»."""
    probe = _PROBE_SVG
    with tempfile.TemporaryDirectory(prefix="posiz-probe-") as tmp:
        src = Path(tmp) / "probe.svg"
        out = Path(tmp) / "probe.png"
        src.write_text(probe, encoding="utf-8")
        cmd, env = _command(kind, binary, src, out, 8, 8)
        try:
            subprocess.run(cmd, check=True, timeout=PROBE_TIMEOUT, capture_output=True, env=env)
        except (subprocess.SubprocessError, OSError):
            return False
        return out.is_file() and out.read_bytes().startswith(PNG_MAGIC)


def available() -> dict:
    """Состояние растеризатора — попадает в ответ API, чтобы UI не обещал PNG молча."""
    found = detect()
    if not found:
        return {"available": False, "backend": None, "fidelity": None,
                "reason": "нет rsvg-convert / Chromium / ImageMagick: карточки отдаются в SVG"}
    kind, binary = found
    info = {"available": True, "backend": kind, "binary": binary,
            "fidelity": "low" if kind in LOW_FIDELITY else "high"}
    if not _probe_works(kind, binary):
        info["available"] = False
        info["reason"] = (f"{kind} найден ({binary}), но не запускается — проверьте "
                          "зависимости или MEDIA_CHROME_LIBS / MEDIA_RASTER_BIN")
    return info


def _child_env() -> dict:
    """Окружение растеризатора: ``MEDIA_CHROME_LIBS`` дописывается в ``LD_LIBRARY_PATH``."""
    env = dict(os.environ)
    extra = [p for p in (os.getenv("MEDIA_CHROME_LIBS") or "").split(":") if p.strip()]
    if extra:
        current = env.get("LD_LIBRARY_PATH")
        env["LD_LIBRARY_PATH"] = ":".join(extra + ([current] if current else []))
    return env


def _command(kind: str, binary: str, src: Path, out: Path, width: int, height: int
             ) -> tuple[list[str], dict]:
    if kind == "rsvg":
        return [binary, "-w", str(width), "-h", str(height), "-o", str(out), str(src)], _child_env()
    if kind == "imagemagick":
        # -density поднимаем, чтобы мелкий текст не «мылился» при растеризации
        return ([binary, "-density", "144", "-background", "none", str(src),
                 "-resize", f"{width}x{height}", str(out)], _child_env())
    # chromium: --virtual-time-budget даёт движку дорисовать шрифты и градиенты до снимка
    return ([binary, "--headless", "--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage",
             "--hide-scrollbars", "--no-first-run", "--virtual-time-budget=1500",
             "--force-device-scale-factor=1", f"--window-size={width},{height}",
             f"--screenshot={out}", f"file://{src}"], _child_env())


def rasterize(svg: str, width: int, height: int, *, timeout: int = DEFAULT_TIMEOUT
              ) -> tuple[bytes | None, str | None]:
    """Отрисовать SVG в PNG. Возвращает ``(png_bytes, backend)`` или ``(None, None)``."""
    data = svg.encode("utf-8")
    if len(data) > MAX_INPUT:
        return None, None
    found = detect()
    if not found:
        return None, None
    kind, binary = found

    with tempfile.TemporaryDirectory(prefix="posiz-raster-") as tmp:
        src = Path(tmp) / "card.svg"
        out = Path(tmp) / "card.png"
        src.write_bytes(data)

        cmd, env = _command(kind, binary, src, out, width, height)
        try:
            subprocess.run(cmd, check=True, timeout=timeout, capture_output=True, env=env)
        except (subprocess.SubprocessError, OSError):
            return None, None

        if not out.is_file():
            return None, None
        png = out.read_bytes()
        if not png.startswith(PNG_MAGIC) or len(png) < 200:
            return None, None
        return png, kind


def playwright_available() -> bool:
    """Есть ли playwright: он даёт пакетный растр — один браузер на весь ролик."""
    try:
        import playwright.sync_api  # noqa: F401, PLC0415 — необязательная зависимость
    except Exception:                                     # noqa: BLE001
        return False
    found = detect()
    return bool(found and found[0] == "chromium")


def rasterize_many(svgs: list[str], width: int, height: int, *, timeout: int = 120
                  ) -> tuple[list[bytes] | None, str | None]:
    """Много кадров за один запуск браузера.

    Зачем: ролик — это 300–900 кадров. CLI-запуск Chromium на кадр стоит ~3 с
    (`--virtual-time-budget`), то есть полчаса на 30-секундный ролик; один браузер и
    `page.screenshot()` на кадр — ~50 мс. Поэтому пакетный путь есть, а пофреймовый
    остаётся запасным: честная деградация важнее скорости.
    """
    if not svgs:
        return [], None
    found = detect()
    if not found or found[0] != "chromium":
        return None, None
    try:
        from playwright.sync_api import sync_playwright          # noqa: PLC0415
    except Exception:                                          # noqa: BLE001
        return None, None
    shots: list[bytes] = []
    env = _child_env()
    try:
        with sync_playwright() as session:
            browser = session.chromium.launch(
                executable_path=found[1], args=["--no-sandbox", "--disable-dev-shm-usage"],
                env=env, timeout=timeout * 1000)
            page = browser.new_page(viewport={"width": width, "height": height},
                                    device_scale_factor=1)
            for svg in svgs:
                page.set_content(f"<html><body style=\"margin:0;background:#000\">{svg}</body></html>")
                shots.append(page.screenshot(type="png", timeout=timeout * 1000))
            browser.close()
    except Exception:                                          # noqa: BLE001 — любой сбой браузера
        return None, None
    if len(shots) != len(svgs) or not all(png.startswith(PNG_MAGIC) for png in shots):
        return None, None
    return shots, "chromium"


def png_size(data: bytes) -> tuple[int, int]:
    """Размер PNG из заголовка IHDR — без зависимостей и без декодирования картинки."""
    if len(data) < 24 or not data.startswith(PNG_MAGIC):
        raise ValueError("это не PNG")
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    return width, height
