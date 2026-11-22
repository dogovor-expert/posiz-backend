"""Go-Live: боевые HTTP-вызовы адаптеров на подменённом транспорте.

Проверяем ровно то, что нельзя проверить в DRY_RUN: последовательность запросов,
параметры платформенных API и восстановление после таймаута.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from orbita import feed
from orbita.adapters.base import NormalizedError, PublishInput
from orbita.adapters.max import MaxAdapter
from orbita.adapters.special import HeadlessAdapter, RssAdapter
from orbita.adapters.vk import VKAdapter
from orbita.workers import headless


def live(monkeypatch):
    """Выключить DRY_RUN и подставить токены: адаптер пойдёт в «сеть»."""
    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setenv("VK_COMMUNITY_TOKEN", "vk-token")
    monkeypatch.setenv("MAX_BOT_TOKEN", "max-token")


def transport(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5)


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ VK
def test_vk_two_step_media_upload(monkeypatch):
    live(monkeypatch)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(url)
        if url.endswith("/photos.getWallUploadServer"):
            return httpx.Response(200, json={"response": {
                "upload_url": "https://upload.vk.test/photo"}})
        if url.startswith("https://upload.vk.test/photo"):
            assert request.headers["content-type"].startswith("multipart/form-data")
            return httpx.Response(200, json={"server": 17, "photo": '[{"id":"1"}]', "hash": "h"})
        if url.endswith("/photos.saveWallPhoto"):
            body = request.content.decode()
            assert "server=17" in body and "hash=h" in body and "group_id=42" in body
            return httpx.Response(200, json={"response": [{"owner_id": -42, "id": 555}]})
        if url == "https://cdn.test/a.jpg":
            return httpx.Response(200, content=b"\xff\xd8\xff")
        if url.endswith("/wall.post"):
            body = request.content.decode()
            assert "attachments=" in body and "photo-42_555" in body
            return httpx.Response(200, json={"response": {"post_id": 900}})
        return httpx.Response(404, json={"error": {"error_code": 1, "error_msg": url}})

    adapter = VKAdapter(client=transport(handler))
    result = run(adapter.publish(
        PublishInput(account_external_id="-42", text="Пост с фото",
                     media_urls=["https://cdn.test/a.jpg"]), "idem-1"))
    assert result.external_id == "900"
    assert result.external_url == "https://vk.com/wall-42_900"
    assert any("getWallUploadServer" in c for c in calls)
    assert any("saveWallPhoto" in c for c in calls)


def test_vk_reconcile_after_timeout(monkeypatch):
    """wall.post упал по сети, но пост уже есть — возвращаем его вместо дубля."""
    live(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/wall.post"):
            return httpx.Response(200, json={"error": {"error_code": 10, "error_msg": "internal"}})
        if url.endswith("/wall.get"):
            return httpx.Response(200, json={"response": {"items": [
                {"id": 700, "text": "  Пост с фото "}]}})
        return httpx.Response(404, json={})

    adapter = VKAdapter(client=transport(handler))
    result = run(adapter.publish(PublishInput(account_external_id="-42", text="Пост с фото"), "idem-2"))
    assert result.external_id == "700"


def test_vk_auth_error_is_not_retried(monkeypatch):
    live(monkeypatch)
    state = {"wall_get": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/wall.post"):
            return httpx.Response(200, json={"error": {"error_code": 27, "error_msg": "no rights"}})
        state["wall_get"] += 1
        return httpx.Response(200, json={"response": {"items": []}})

    adapter = VKAdapter(client=transport(handler))
    with pytest.raises(NormalizedError) as err:
        run(adapter.publish(PublishInput(account_external_id="-42", text="x"), "idem-3"))
    assert err.value.kind == "auth" and err.value.retryable is False
    assert state["wall_get"] == 0        # reconcile не нужен: ошибка не сетевая


# ------------------------------------------------------------------ MAX
def test_max_uploads_media_before_message(monkeypatch):
    live(monkeypatch)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen.append(url)
        if url.endswith(":8000/uploads") or url.endswith("/uploads?type=image") or "/uploads" in url:
            return httpx.Response(200, json={"url": "https://up.max.test/slot?sign=1"})
        if url.startswith("https://up.max.test/slot"):
            return httpx.Response(200, json={})
        if url == "https://cdn.test/b.jpg":
            return httpx.Response(200, content=b"\xff\xd8")
        if "/messages" in url:
            import json
            body = json.loads(request.content)
            assert body["attachments"][0]["type"] == "image"
            assert request.headers["authorization"] == "max-token"
            return httpx.Response(200, json={"message": {"body": {"mid": "mid-1"}}})
        return httpx.Response(404, json={})

    adapter = MaxAdapter(client=transport(handler))
    result = run(adapter.publish(PublishInput(account_external_id="chat-1", text="**Жирный** текст",
                                              media_urls=["https://cdn.test/b.jpg"]), "idem-4"))
    assert result.external_id == "mid-1"
    assert any("/uploads" in s for s in seen)


# ------------------------------------------------------------------ RSS Дзен
def test_rss_feed_has_yandex_full_text():
    adapter = RssAdapter("dzen")
    xml = adapter.render([
        {"title": "Как вернуть залог", "body": "Первый абзац.\n\nВторой абзац.",
         "url": "https://dogovor.expert/zalog", "guid": "g-1"},
        {"title": "Проверка договора", "body": "Текст.", "url": "https://dogovor.expert/check",
         "guid": "g-2"},
    ])
    assert "<yandex:full-text><![CDATA[<p>Первый абзац.</p><p>Второй абзац.</p>]]></yandex:full-text>" in xml
    report = feed.validate(xml)
    assert report["ok"] and report["items"] == 2
    assert "<language>ru</language>" in xml


def test_rss_cdata_is_escaped():
    """``]]>`` внутри текста не должен закрывать CDATA — иначе лента ломается."""
    assert feed._cdata("опасно ]]> тут") == "<![CDATA[опасно ]]]]><![CDATA[> тут]]>"
    xml = feed.build("t", "https://e.x", "d", [
        {"title": "Закрывающий тег", "body": "опасно ]]> тут", "url": "https://e.x/1"}])
    report = feed.validate(xml)
    assert report["ok"] and report["items"] == 1
    assert xml.count("<![CDATA[") == xml.count("]]>")


def test_rss_publish_returns_feed_url(monkeypatch):
    monkeypatch.setenv("LINK_DOMAIN", "posiz.ru")
    result = run(RssAdapter("dzen").publish(PublishInput(account_external_id="dzen",
                                                         text="Пост\n\nТело"), "idem-5"))
    assert result.external_url.endswith("/feed/dogovor.xml")


# ------------------------------------------------------------------ headless (Playwright)
def test_headless_reports_draft_when_disabled(monkeypatch):
    monkeypatch.setenv("HEADLESS_ENABLED", "0")
    res = run(headless.run(headless.Job(channel="tenchat", account="a", text="Текст")))
    assert res["status"] == "draft_fallback" and res["text"] == "Текст"


def test_headless_publishes_with_injected_driver(monkeypatch):
    monkeypatch.setenv("HEADLESS_ENABLED", "1")
    captured = {}

    async def driver(payload):
        captured.update(payload)
        return {"ok": True, "url": "https://tenchat.ru/post/abc", "external_id": "abc"}

    headless.register_driver(driver)
    try:
        res = run(headless.run(headless.Job(channel="tenchat", account="acc",
                                            text="Пост в TenChat", media=["x.jpg"])))
        assert res["status"] == "published"
        assert res["url"] == "https://tenchat.ru/post/abc"
        assert captured["text"] == "Пост в TenChat" and captured["media"] == ["x.jpg"]
    finally:
        headless.register_driver(None)


def test_headless_expired_session_falls_back_to_draft(monkeypatch):
    monkeypatch.setenv("HEADLESS_ENABLED", "1")
    monkeypatch.setenv("DRY_RUN", "0")

    async def driver(payload):
        return {"ok": False, "reason": "сессия истекла", "draft_fallback": True}

    headless.register_driver(driver)
    try:
        adapter = HeadlessAdapter("tenchat")
        with pytest.raises(NormalizedError) as err:
            run(adapter.publish(PublishInput(account_external_id="acc", text="Текст"), "idem-6"))
        assert "сессия истекла" in str(err.value)
        assert "черновиком" in str(err.value)
    finally:
        headless.register_driver(None)


def test_headless_status_and_selectors():
    st = headless.status()
    assert set(st["channels"]) == {"tenchat", "rutube"}
    assert st["driver"] in {"real", "injected"}
