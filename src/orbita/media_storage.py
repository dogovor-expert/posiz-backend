"""Хранилище медиа-артефактов: локальный спул и S3 за одним интерфейсом.

Зачем отдельный модуль, а не «записать файл рядом»: артефакт карточки должен пережить
рестарт контейнера, попасть в общий том между API и воркером и уметь удаляться по TTL.
В проде это S3/MinIO, в разработке и тестах — каталог на диске (``MEDIA_SPOOL_DIR``).

Ключи строятся только кодом (``media/<workspace>/<kind>/<id>.<ext>``) и проходят проверку
:func:`safe_key`: путь, пришедший извне, не может выйти за корень хранилища. Это не
формальность — артефакт отдаётся по HTTP, и ``../../etc/passwd`` в ключе был бы
чтением файлов сервера.

Режимы:

* ``MEDIA_STORAGE=spool`` (по умолчанию) — локальный каталог;
* ``MEDIA_STORAGE=s3`` — S3/MinIO, требует ``boto3`` и ``MEDIA_S3_BUCKET``; при
  отсутствии зависимости модуль **не притворяется, что сохранил**: поднимается
  :class:`StorageError`, и вызывающий код честно возвращает 503, а не «ок».
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Literal

Backend = Literal["spool", "s3"]

_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9/_.-]{0,200}$")
MAX_BYTES = 40 * 1024 * 1024        # 40 МБ: карточка весит десятки КБ, видео — единицы МБ


class StorageError(RuntimeError):
    """Хранилище недоступно или запрошен недопустимый ключ."""


def backend() -> Backend:
    value = (os.getenv("MEDIA_STORAGE") or "spool").strip().lower()
    return "s3" if value == "s3" else "spool"


def root() -> Path:
    """Корень локального спула. В тестах подменяется `MEDIA_SPOOL_DIR`."""
    path = Path(os.getenv("MEDIA_SPOOL_DIR") or "var/media").expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def safe_key(key: str) -> str:
    """Проверить ключ: только латиница, цифры, ``/``, ``_``, ``.``, ``-``, без ``..``."""
    if not key or not _KEY_RE.match(key):
        raise StorageError(f"недопустимый ключ хранилища: {key!r}")
    if ".." in key.split("/") or key.startswith("/"):
        raise StorageError(f"ключ выходит за корень хранилища: {key!r}")
    return key


def _spool_path(key: str) -> Path:
    base = root()
    target = (base / safe_key(key)).resolve()
    if base not in target.parents and target != base:
        raise StorageError(f"ключ выходит за корень хранилища: {key!r}")
    return target


def put(key: str, data: bytes, *, mime: str = "application/octet-stream") -> dict:
    """Записать артефакт. Возвращает описание: ключ, размер, mime, бэкенд."""
    safe_key(key)
    if not data:
        raise StorageError("пустой артефакт не сохраняем")
    if len(data) > MAX_BYTES:
        raise StorageError(f"артефакт больше {MAX_BYTES // (1024 * 1024)} МБ")

    if backend() == "s3":
        return _s3_put(key, data, mime)

    path = _spool_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    # атомарная запись: воркер и API могут читать один и тот же каталог
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return {"key": key, "bytes": len(data), "mime": mime, "backend": "spool"}


def get(key: str) -> bytes | None:
    safe_key(key)
    if backend() == "s3":
        return _s3_get(key)
    path = _spool_path(key)
    return path.read_bytes() if path.is_file() else None


def exists(key: str) -> bool:
    safe_key(key)
    if backend() == "s3":
        return _s3_get(key) is not None
    return _spool_path(key).is_file()


def delete(key: str) -> bool:
    safe_key(key)
    if backend() == "s3":
        return _s3_delete(key)
    path = _spool_path(key)
    if path.is_file():
        path.unlink()
        return True
    return False


def mtime(key: str) -> float | None:
    """Время создания артефакта — по нему работает TTL."""
    safe_key(key)
    if backend() == "s3":
        return None                       # в S3 TTL живёт на бакете (lifecycle), не в коде
    path = _spool_path(key)
    return path.stat().st_mtime if path.is_file() else None


def purge_expired(ttl_seconds: int, *, now: float | None = None) -> dict:
    """Удалить артефакты старше TTL. Возвращает счётчики, а не «сделано»."""
    if ttl_seconds <= 0:
        raise ValueError("TTL должен быть положительным")
    if backend() == "s3":
        return {"backend": "s3", "deleted": 0,
                "note": "в S3 срок жизни задаётся lifecycle-правилом на бакете"}
    moment = now if now is not None else time.time()
    base = root()
    deleted = 0
    freed = 0
    kept = 0
    for path in base.rglob("*"):
        if not path.is_file():
            continue
        age = moment - path.stat().st_mtime
        if age > ttl_seconds:
            freed += path.stat().st_size
            path.unlink()
            deleted += 1
        else:
            kept += 1
    for folder in sorted((p for p in base.rglob("*") if p.is_dir()), reverse=True):
        if folder != base and not any(folder.iterdir()):
            folder.rmdir()
    return {"backend": "spool", "deleted": deleted, "kept": kept, "freed_bytes": freed}


def usage() -> dict:
    """Сколько занимает хранилище — нужно для квот и для диагностики."""
    if backend() == "s3":
        return {"backend": "s3", "files": None, "bytes": None}
    base = root()
    files = [p for p in base.rglob("*") if p.is_file()]
    return {"backend": "spool", "root": str(base), "files": len(files),
            "bytes": sum(p.stat().st_size for p in files),
            "disk_free": shutil.disk_usage(base).free}


def reset_for_tests() -> None:
    """Очистить спул (только для тестов и демо-стенда)."""
    if backend() == "s3":
        raise StorageError("очистка S3 из тестов запрещена")
    base = root()
    for path in sorted(base.rglob("*"), reverse=True):
        if path.is_file():
            path.unlink()
        elif path.is_dir():
            path.rmdir()


# ------------------------------------------------------------------ S3
def _s3_client():
    try:
        import boto3                                    # noqa: PLC0415 — необязательная зависимость
    except ModuleNotFoundError as exc:                  # pragma: no cover — проверяется тестом
        raise StorageError("MEDIA_STORAGE=s3, но boto3 не установлен") from exc
    bucket = os.getenv("MEDIA_S3_BUCKET")
    if not bucket:
        raise StorageError("MEDIA_STORAGE=s3, но MEDIA_S3_BUCKET не задан")
    return boto3.client(
        "s3",
        endpoint_url=os.getenv("MEDIA_S3_ENDPOINT") or None,
        region_name=os.getenv("MEDIA_S3_REGION") or None,
    ), bucket


def _s3_put(key: str, data: bytes, mime: str) -> dict:
    client, bucket = _s3_client()
    client.put_object(Bucket=bucket, Key=key, Body=data, ContentType=mime)
    return {"key": key, "bytes": len(data), "mime": mime, "backend": "s3"}


def _s3_get(key: str) -> bytes | None:
    client, bucket = _s3_client()
    try:
        return client.get_object(Bucket=bucket, Key=key)["Body"].read()
    except Exception:                                   # noqa: BLE001 — NoSuchKey и права доступа
        return None


def _s3_delete(key: str) -> bool:
    client, bucket = _s3_client()
    client.delete_object(Bucket=bucket, Key=key)
    return True
