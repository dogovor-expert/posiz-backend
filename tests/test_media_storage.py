"""Хранилище, растр и публичные ссылки медиа (этап B конвейера, ADR 0023).

Этап A проверял, что карточка **правильно свёрстана** (`test_media_cards.py`). Этап B
добавляет три вещи, каждая из которых может «тихо» сломаться в проде, поэтому проверяется
отдельно:

1. **Хранилище.** Артефакт должен лежать не «в памяти процесса», а в спуле/S3, потому что
   рендерит воркер, а отдаёт API (это разные контейнеры). Ключ приходит из кода, но всё
   равно валидируется: артефакт отдаётся по HTTP, и `../` в ключе — это чтение файлов сервера.
2. **Растр.** Каналы не показывают SVG. PNG делает внешний бинарь, и его может не быть:
   тогда честный отказ (`raster_unavailable`), а не «тихо SVG под видом PNG».
3. **Публичные ссылки.** Telegram тянет картинку сам, без наших заголовков, поэтому ссылка
   подписана (id + формат + срок + HMAC). Подделка, просрочка и чужая подпись — 403.
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from orbita import media, media_raster, media_storage, store
from orbita.main import app

client = TestClient(app)
H = {"X-Actor-Role": "owner"}

BRAND = {"name": "dogovor.expert"}
POST = {"title": "Хранилище и публичные ссылки", "footer": "posiz.ru"}


@pytest.fixture(autouse=True)
def spool(tmp_path, monkeypatch):
    """Свой каталог и чистые коллекции на каждый тест.

    Каталог обязателен: TTL-тесты удаляют файлы. Коллекции чистим потому, что дедупликация
    по `spec_hash` иначе вернула бы артефакт предыдущего теста — с ключом в чужом спуле,
    и падение выглядело бы как «сломался публичный эндпоинт».
    """
    monkeypatch.setenv("MEDIA_SPOOL_DIR", str(tmp_path / "media"))
    monkeypatch.setenv("MEDIA_STORAGE", "spool")
    monkeypatch.setenv("MEDIA_URL_SECRET", "test-media-url-secret")
    monkeypatch.delenv("MEDIA_RASTER_BIN", raising=False)
    for collection in (media._specs, media._jobs, media._assets):
        collection.clear_memory()
    yield tmp_path / "media"


# --------------------------------------------------------------- хранилище
def test_put_get_delete_roundtrip():
    info = media_storage.put("media/ws_demo/asset/a1.svg", b"<svg/>", mime="image/svg+xml")
    assert info["bytes"] == 6 and info["backend"] == "spool"
    assert media_storage.get("media/ws_demo/asset/a1.svg") == b"<svg/>"
    assert media_storage.exists("media/ws_demo/asset/a1.svg")
    assert media_storage.delete("media/ws_demo/asset/a1.svg") is True
    assert media_storage.get("media/ws_demo/asset/a1.svg") is None
    assert media_storage.delete("media/ws_demo/asset/a1.svg") is False


@pytest.mark.parametrize("key", ["../etc/passwd", "media/../../etc/passwd", "/etc/passwd",
                                 "", "media/ws asset/a.svg", "МЕДИА/a.svg"])
def test_key_cannot_escape_root(key: str):
    """Ключ с `..`, абсолютным путём или не-латиницей отвергается, а не «как-нибудь».'"""
    with pytest.raises(media_storage.StorageError):
        media_storage.put(key, b"x")


def test_empty_and_oversized_payloads_are_rejected(monkeypatch):
    with pytest.raises(media_storage.StorageError):
        media_storage.put("media/ws/a.svg", b"")
    monkeypatch.setattr(media_storage, "MAX_BYTES", 8)
    with pytest.raises(media_storage.StorageError):
        media_storage.put("media/ws/big.png", b"x" * 9)


def test_put_is_atomic_no_partial_files_left(spool):
    media_storage.put("media/ws/a.svg", b"data")
    files = [p.name for p in spool.rglob("*") if p.is_file()]
    assert files == ["a.svg"]                      # ни .tmp-хвостов, ни каталогов-мусора


def test_usage_reports_real_numbers(spool):
    media_storage.put("media/ws/a.svg", b"12345")
    media_storage.put("media/ws/b.svg", b"678")
    usage = media_storage.usage()
    assert usage["backend"] == "spool" and usage["files"] == 2 and usage["bytes"] == 8
    assert usage["disk_free"] > 0


def test_purge_expired_removes_only_old(spool):
    import os

    old = spool / "media" / "ws" / "old.svg"
    new = spool / "media" / "ws" / "new.svg"
    media_storage.put("media/ws/old.svg", b"old")
    media_storage.put("media/ws/new.svg", b"new")
    past = time.time() - 10 * 86400
    os.utime(old, (past, past))
    report = media_storage.purge_expired(7 * 86400)
    assert report["deleted"] == 1 and report["kept"] == 1 and report["freed_bytes"] == 3
    assert not old.exists() and new.exists()


def test_s3_mode_without_boto3_raises_not_pretends(monkeypatch):
    """`MEDIA_STORAGE=s3` без boto3 — это 503 и внятное сообщение, а не «сохранено»."""
    monkeypatch.setenv("MEDIA_STORAGE", "s3")
    monkeypatch.setenv("MEDIA_S3_BUCKET", "posiz-media")
    pytest.importorskip  # noqa: B018 — читаемость: ниже проверяем именно отсутствие boto3
    try:
        import boto3  # noqa: F401
    except ModuleNotFoundError:
        with pytest.raises(media_storage.StorageError, match="boto3"):
            media_storage.put("media/ws/a.svg", b"x")
    else:                                               # прагматично: boto3 есть
        monkeypatch.setenv("MEDIA_S3_BUCKET", "")
        with pytest.raises(media_storage.StorageError, match="MEDIA_S3_BUCKET"):
            media_storage.put("media/ws/a.svg", b"x")


def test_s3_purge_defers_to_bucket_lifecycle(monkeypatch):
    monkeypatch.setenv("MEDIA_STORAGE", "s3")
    report = media_storage.purge_expired(86400)
    assert report["backend"] == "s3" and report["deleted"] == 0


# -------------------------------------------------------------------- растр
def test_raster_available_shape():
    state = media_raster.available()
    assert set(state) >= {"available", "backend", "fidelity"}
    if state["available"]:
        assert state["backend"] in {"rsvg", "chromium", "imagemagick"}
        assert state["fidelity"] == ("low" if state["backend"] == "imagemagick" else "high")


def test_png_size_reads_ihdr_and_rejects_garbage():
    media_storage_png = _tiny_png()
    assert media_raster.png_size(media_storage_png) == (2, 1)
    with pytest.raises(ValueError):
        media_raster.png_size(b"not a png")


def test_rasterize_refuses_oversized_input(monkeypatch):
    monkeypatch.setattr(media_raster, "MAX_INPUT", 10)
    assert media_raster.rasterize("<svg>hi</svg>", 10, 10) == (None, None)


def _tiny_png() -> bytes:
    """Собственный минимальный PNG 2×1 — тест не должен зависеть от растеризатора узла."""
    import struct
    import zlib

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + kind + payload +
                struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", 2, 1, 8, 2, 0, 0, 0)      # 2×1, 8 бит, RGB
    raw = b"\x00" + b"\x00\x00\x00" + b"\xff\xff\xff"    # строка: фильтр + два пикселя
    return (media_raster.PNG_MAGIC + chunk(b"IHDR", ihdr) +
            chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


# ------------------------------------------------------- подписанные ссылки
def test_signed_token_roundtrip():
    token = media.signed_token("as_demo123", "png", ttl=60)
    assert media.resolve_token(token) == ("as_demo123", "png")
    assert media.resolve_token(media.signed_token("as_demo123", "svg", ttl=60)) == \
        ("as_demo123", "svg")


def test_expired_token_is_rejected():
    assert media.resolve_token(media.signed_token("as_demo123", "png", ttl=-5)) is None


def test_tampered_token_is_rejected():
    token = media.signed_token("as_demo123", "png", ttl=60)
    broken = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
    assert media.resolve_token(broken) is None
    assert media.resolve_token("не-токен") is None
    assert media.resolve_token("") is None


def test_token_without_secret_fails_loud(monkeypatch):
    monkeypatch.delenv("MEDIA_URL_SECRET", raising=False)
    monkeypatch.delenv("JWT_SECRET", raising=False)
    with pytest.raises(ValueError, match="MEDIA_URL_SECRET"):
        media.signed_token("as_demo123", "png")


def test_public_url_points_to_public_endpoint(monkeypatch):
    monkeypatch.setenv("MEDIA_PUBLIC_BASE", "https://posiz.ru/")
    url = media.public_url("as_demo123", "png")
    assert url.startswith("https://posiz.ru/api/v1/media/public/")
    token = url.rsplit("/", 1)[-1]
    assert media.resolve_token(token) == ("as_demo123", "png")


# ---------------------------------------------------------------------- API
def _card(format_: str = "1x1") -> str:
    response = client.post("/api/v1/media/cards", headers=H,
                           json={"post": POST, "formats": [format_], "brand": BRAND})
    assert response.status_code == 201, response.text
    return response.json()["items"][0]["asset_id"]


def test_asset_endpoint_serves_svg_and_png_by_extension():
    asset_id = _card()
    svg = client.get(f"/api/v1/media/assets/{asset_id}", headers=H)
    assert svg.status_code == 200 and svg.headers["content-type"].startswith("image/svg+xml")

    png = client.get(f"/api/v1/media/assets/{asset_id}.png", headers=H)
    if png.status_code == 409:                          # узел без растеризатора
        assert png.json()["detail"]["issue"] == "raster_unavailable"
        return
    assert png.status_code == 200 and png.headers["content-type"] == "image/png"
    assert media_raster.png_size(png.content) == (1080, 1080)


def test_asset_endpoint_rejects_unknown_format():
    asset_id = _card()
    response = client.get(f"/api/v1/media/assets/{asset_id}?format=webp", headers=H)
    assert response.status_code == 400


def test_public_link_works_without_authorization():
    asset_id = _card()
    fmt = media.best_format(asset_id) or "svg"
    response = client.get(f"/api/v1/media/public/{media.signed_token(asset_id, fmt)}")
    assert response.status_code == 200
    assert response.headers["cache-control"].startswith("public")
    assert len(response.content) > 500


def test_public_link_bad_tokens_are_403():
    assert client.get("/api/v1/media/public/musor").status_code == 403
    expired = media.signed_token("as_demo123", "png", ttl=-1)
    assert client.get(f"/api/v1/media/public/{expired}").status_code == 403


def test_public_link_unknown_asset_is_404():
    token = media.signed_token("as_нетакого", "svg", ttl=60)
    assert client.get(f"/api/v1/media/public/{token}").status_code == 404


def test_purge_endpoint_reports_counters():
    _card()
    response = client.post("/api/v1/media/purge?ttl_days=1", headers=H)
    assert response.status_code == 200
    body = response.json()
    assert body["ttl_days"] == 1 and "deleted" in body and "kept" in body


def test_svg_survives_purge_of_file_because_spec_is_deterministic():
    """Файл удалили — артефакт всё равно отдаётся: SVG восстанавливается из спеки."""
    asset_id = _card()
    for key in [k for k in (media.asset(asset_id) or {}).get("svg_key") and
                [media.asset(asset_id)["svg_key"]] or []]:
        media_storage.delete(key)
    response = client.get(f"/api/v1/media/assets/{asset_id}", headers=H)
    assert response.status_code == 200 and b"<svg" in response.content


# ------------------------------------------------- публикация с вложениями
def _variant_and_account() -> tuple[str, str]:
    """Вариант и аккаунт канала через публичный контракт — как это делает клиент."""
    signal = client.post("/api/v1/signals", json={
        "kind": "manual", "title": "Публикация с карточкой", "goal": "lead"})
    assert signal.status_code == 201, signal.text
    generated = client.post(f"/api/v1/signals/{signal.json()['id']}/generate",
                            json={"channels": ["vk"]})
    assert generated.status_code == 200, generated.text
    variant = generated.json()["variants"][0]
    accounts = {c["channel"]: c["id"] for c in client.get("/api/v1/channels").json()}
    return variant["id"], accounts["vk"]


def test_publication_with_unknown_media_is_rejected():
    variant_id, account_id = _variant_and_account()
    response = client.post("/api/v1/publications", headers=H, json={
        "variant_id": variant_id, "channel_account_id": account_id,
        "scheduled_at": "2030-01-01T10:00:00+00:00", "media_ids": ["as_нетакого"],
    })
    assert response.status_code == 409
    assert response.json()["detail"]["kind"] == "media_asset_unknown"


def test_publication_keeps_media_ids_and_order():
    variant_id, account_id = _variant_and_account()
    asset_id = _card()
    response = client.post("/api/v1/publications", headers=H, json={
        "variant_id": variant_id, "channel_account_id": account_id,
        "scheduled_at": "2030-01-02T10:00:00+00:00",
        "media_ids": [asset_id, asset_id],              # дубль схлопывается
    })
    assert response.status_code == 201, response.text
    assert response.json()["media_ids"] == [asset_id]
    stored = store.get_publication(response.json()["id"])
    assert stored["media_ids"] == [asset_id]


def test_best_format_falls_back_to_svg_without_raster():
    asset_id = _card()
    original = media.asset(asset_id)
    media._assets.put({**original, "png_key": None})     # растеризатора «не было»
    assert media.best_format(asset_id) == "svg"
    assert media.best_format("as_нетакого") is None
