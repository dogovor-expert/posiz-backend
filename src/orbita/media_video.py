"""FFmpeg-шаг медиа-конвейера: кадры → MP4 (этап C, ADR 0023).

Почему отдельный модуль, а не «вызвать ffmpeg в двух местах». У кодирования есть три
свойства, которые ломаются тихо и дорого:

* **Длительность расходится с таймлайном.** Ролик на 30,1 с вместо 30,0 с — это сдвинутые
  титры и «дребезг» последней секунды. Длину проверяем `ffprobe`, а не «на глаз по плееру».
* **Файл не проигрывается в ленте.** Без `-movflags +faststart` браузер и VK тянут весь файл
  до первого кадра, а `yuv420p` нужен потому, что `yuv444p` не играет на части мобильных
  декодеров. Флаг `moov` проверяется в тесте.
* **Файл не влезает в лимит канала.** Telegram по URL — 20 МБ, по загрузке — 50 МБ. Поэтому
  кодируем с ограничением битрейта от целевого размера, а не «в максимум качества».

Если ffmpeg нет, это **не повод врать**: ``available()`` возвращает причину, а конвейер
оставляет растр (кадры и постер) — статус ролика становится ``storyboard``. Честный растр
лучше, чем «видео есть» и пустой файл.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

DEFAULT_TIMEOUT = 180                     # секунд на ролик в 30 с
#: лимиты каналов: по ним считается целевой битрейт (Telegram URL — 20 МБ, загрузка — 50 МБ)
CHANNEL_LIMITS_MB = {"telegram": 20, "vk": 50, "max": 50}
DEFAULT_LIMIT_MB = 25
VIDEO_CODEC = "libx264"
PIX_FMT = "yuv420p"


def detect() -> dict:
    """Найти ffmpeg/ffprobe. ``MEDIA_FFMPEG_BIN``/``MEDIA_FFPROBE_BIN`` — явные пути."""
    ffmpeg = (os.getenv("MEDIA_FFMPEG_BIN") or "").strip() or shutil.which("ffmpeg")
    ffprobe = (os.getenv("MEDIA_FFPROBE_BIN") or "").strip() or shutil.which("ffprobe")
    return {"ffmpeg": ffmpeg, "ffprobe": ffprobe,
            "available": bool(ffmpeg and ffprobe)}


def _version(binary: str) -> str | None:
    try:
        out = subprocess.run([binary, "-version"], capture_output=True, timeout=20, check=True)
    except (subprocess.SubprocessError, OSError):
        return None
    first = out.stdout.decode("utf-8", "replace").splitlines()
    return first[0].split(" Copyright")[0].strip() if first else None


def available() -> dict:
    state = detect()
    if not state["available"]:
        missing = [name for name in ("ffmpeg", "ffprobe") if not state[name]]
        return {**state, "reason": f"нет {', '.join(missing)}: ролик останется сторибордом",
                "version": None}
    return {**state, "version": _version(state["ffmpeg"] or "ffmpeg"), "reason": None}


def target_bitrate(duration: float, limit_mb: int = DEFAULT_LIMIT_MB, audio_kbps: int = 96
                   ) -> int:
    """Битрейт под лимит файла: лучше ограничить качество, чем не пройти модерацию канала."""
    if duration <= 0:
        return 4_000_000
    budget_bits = limit_mb * 8 * 1024 * 1024 * 0.92      # 8% оставляем на контейнер
    total_kbps = budget_bits / duration / 1000
    video_kbps = max(600, int(total_kbps - audio_kbps))
    return min(video_kbps, 8_000) * 1000


def encode(frames: list[bytes], *, fps: int = 30, audio: bytes | None = None,
           limit_mb: int = DEFAULT_LIMIT_MB, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Кадры PNG → MP4. Возвращает ``{ok, bytes, probe}`` или ``{ok: False, reason}``."""
    state = detect()
    if not state["available"]:
        return {"ok": False, "reason": available()["reason"]}
    if not frames:
        return {"ok": False, "reason": "нет кадров для кодирования"}

    duration = len(frames) / max(fps, 1)
    bitrate = target_bitrate(duration, limit_mb)
    with tempfile.TemporaryDirectory(prefix="posiz-reel-") as tmp:
        folder = Path(tmp)
        for index, png in enumerate(frames):
            (folder / f"f{index:05d}.png").write_bytes(png)
        out = folder / "reel.mp4"
        cmd = [state["ffmpeg"], "-hide_banner", "-loglevel", "error", "-y",
               "-framerate", str(fps), "-i", str(folder / "f%05d.png")]
        audio_path = None
        if audio:
            audio_path = folder / "voice.m4a"
            audio_path.write_bytes(audio)
            # apad: речь короче видео — добираем тишиной; длиннее — обрежет -t ниже
            cmd += ["-i", str(audio_path), "-af", "apad", "-c:a", "aac", "-b:a", "96k"]
        else:
            # тишина вместо дорожки: без звука часть клиентов считает файл битым,
            # а -shortest сам по себе оставлял контейнер длиннее видео (поймано тестом)
            cmd += ["-f", "lavfi", "-i", "anullsrc=channel_layout=mono:sample_rate=44100",
                    "-shortest", "-c:a", "aac", "-b:a", "32k"]
        # длительность задаём явно: контейнер не должен «дотягивать» ролик до целой секунды
        cmd += ["-t", f"{duration:.3f}"]
        cmd += ["-c:v", VIDEO_CODEC, "-preset", "medium", "-pix_fmt", PIX_FMT,
                "-b:v", str(bitrate), "-maxrate", str(int(bitrate * 1.2)),
                "-bufsize", str(int(bitrate * 2)),
                # faststart кладёт moov в начало: лента начинает проигрывать до полной загрузки
                "-movflags", "+faststart", str(out)]
        try:
            subprocess.run(cmd, check=True, timeout=timeout, capture_output=True)
        except subprocess.TimeoutExpired:
            return {"ok": False, "reason": f"ffmpeg не успел за {timeout} с"}
        except (subprocess.SubprocessError, OSError) as exc:
            detail = getattr(exc, "stderr", b"") or b""
            return {"ok": False, "reason": f"ffmpeg упал: {detail.decode('utf-8', 'replace')[:300]}"}
        if not out.is_file() or out.stat().st_size < 1024:
            return {"ok": False, "reason": "ffmpeg не создал файл"}
        data = out.read_bytes()
        probe = probe_bytes(data)
        return {"ok": True, "bytes": data, "probe": probe, "bitrate": bitrate}


def probe_bytes(data: bytes) -> dict:
    """Метаданные MP4 без запуска ffprobe: размеры из `tkhd`, длительность из `mvhd`.

    Свой разбор нужен потому, что ffprobe есть не всегда (и это не мешает отдать файл),
    а проверить «длительность совпадает с таймлайном» надо всегда.
    """
    result = {"duration": None, "width": None, "height": None, "faststart": False,
              "bytes": len(data), "brand": data[8:12].decode("ascii", "replace")
              if len(data) > 12 else None}
    if len(data) < 32 or data[4:8] != b"ftyp":
        return result
    moov = data.find(b"moov")
    mdat = data.find(b"mdat")
    result["faststart"] = moov != -1 and (mdat == -1 or moov < mdat)
    mvhd = data.find(b"mvhd")
    if mvhd != -1:
        version = data[mvhd + 4]
        try:
            if version == 1:
                scale = int.from_bytes(data[mvhd + 24:mvhd + 28], "big")
                duration = int.from_bytes(data[mvhd + 28:mvhd + 36], "big")
            else:
                scale = int.from_bytes(data[mvhd + 16:mvhd + 20], "big")
                duration = int.from_bytes(data[mvhd + 20:mvhd + 24], "big")
            if scale:
                result["duration"] = round(duration / scale, 3)
        except (IndexError, ValueError):
            pass
    # tkhd: [size][tkhd][version|flags] + время и id (20 или 32 байта) + 16 байт служебных
    # + матрица 36 байт, дальше width/height как 16.16 fixed-point
    tkhd = data.find(b"tkhd")
    if tkhd != -1:
        body = tkhd + 8
        offset = body + (32 if data[tkhd + 4] == 1 else 20) + 16 + 36
        raw_w = data[offset:offset + 4]
        raw_h = data[offset + 4:offset + 8]
        if len(raw_w) == 4 and len(raw_h) == 4:
            result["width"] = int.from_bytes(raw_w, "big") // 65536
            result["height"] = int.from_bytes(raw_h, "big") // 65536
    if not result["width"] or not result["height"]:
        precise = _ffprobe_if_possible(data)
        result.update({k: precise.get(k) for k in ("width", "height") if precise.get(k)})
        result["duration"] = result["duration"] or precise.get("duration")
    return result


def _ffprobe_if_possible(data: bytes) -> dict:
    """Уточнить метаданные через ffprobe, если он есть (свой разбор — только страховка)."""
    state = detect()
    if not state["available"]:
        return {}
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".mp4") as handle:
        handle.write(data)
        handle.flush()
        probed = probe_file(handle.name)
    return probed if probed.get("ok") else {}


def probe_file(path: str | Path) -> dict:
    """Точные метаданные через ffprobe (используется в проверках и в гейте)."""
    state = detect()
    if not state["available"]:
        return {"ok": False, "reason": "ffprobe недоступен"}
    path = Path(path)
    cmd = [state["ffprobe"] or "ffprobe", "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", str(path)]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=60, check=True)
    except (subprocess.SubprocessError, OSError) as exc:
        return {"ok": False, "reason": f"ffprobe упал: {exc}"}
    data = json.loads(out.stdout.decode("utf-8", "replace") or "{}")
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
    audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), {})
    return {"ok": True,
            "duration": float(data.get("format", {}).get("duration") or 0),
            "bytes": int(data.get("format", {}).get("size") or path.stat().st_size),
            "bitrate": int(data.get("format", {}).get("bit_rate") or 0),
            "codec": video.get("codec_name"), "pix_fmt": video.get("pix_fmt"),
            "width": int(video.get("width") or 0), "height": int(video.get("height") or 0),
            "fps": video.get("r_frame_rate"), "audio": audio.get("codec_name")}


def checks(probe: dict, *, expected_duration: float, fps: int,
           limit_mb: int = DEFAULT_LIMIT_MB,
           size: tuple[int, int] | None = None) -> list[str]:
    """Что должно быть верно у готового файла. Возвращает список нарушений (пусто — ок)."""
    problems: list[str] = []
    duration = float(probe.get("duration") or 0)
    tolerance = max(0.25, 2 / max(fps, 1))
    if abs(duration - expected_duration) > tolerance:
        problems.append(f"длительность {duration:.2f} с ≠ {expected_duration:.2f} с")
    if probe.get("bytes", 0) > limit_mb * 1024 * 1024:
        problems.append(f"файл {probe['bytes'] / 1048576:.1f} МБ > {limit_mb} МБ")
    if probe.get("faststart") is False:
        problems.append("moov не в начале файла: лента не начнёт проигрывание")
    # размеры — из формата ролика, а не «1080×1920 для всего»: 16:9 и 1:1 тоже бывают
    want_w, want_h = size or (1080, 1920)
    if probe.get("width") and probe["width"] != want_w:
        problems.append(f"ширина {probe['width']} ≠ {want_w}")
    if probe.get("height") and probe["height"] != want_h:
        problems.append(f"высота {probe['height']} ≠ {want_h}")
    return problems


def poster_from_frames(frames: list[bytes], at: float, fps: int) -> bytes | None:
    """Постер: кадр из готового набора, а не отдельный рендер (дешевле и точнее)."""
    if not frames:
        return None
    index = min(len(frames) - 1, max(0, int(round(at * max(fps, 1)))))
    return frames[index]
