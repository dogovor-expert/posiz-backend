from __future__ import annotations

import logging

import os

import httpx

from ..config import dry_run as _dry, settings
from .base import ChannelAdapter, NormalizedError, PublishInput, PublishResult

API = "https://platform-api.max.ru"
log = logging.getLogger("orbita.max")


class MaxAdapter(ChannelAdapter):
    """MAX Bot API: токен в заголовке Authorization (не в query).

    Ограничения/особенности:
      * бот создаётся через @MasterBot, только для ИП/ООО с верификацией (1–48 ч);
      * Markdown нестабилен → отправляем plain-safe текст;
      * лимиты технические, стоимость отсутствует.
    """

    channel = "max"
    capabilities = {"publish": True, "edit": False, "delete": False, "media": ["image", "video", "document"],
                    "buttons": True, "markdown": "plain-safe"}

    def __init__(self, token: str | None = None, client: httpx.AsyncClient | None = None):
        self._token = token
        self._client = client

    @property
    def token(self) -> str:
        return self._token or os.getenv("MAX_BOT_TOKEN") or settings.max_bot_token

    async def publish(self, payload: PublishInput, idempotency_key: str) -> PublishResult:
        # токен аккаунта из хранилища приоритетнее окружения; состояние адаптера не мутируем:
        # один экземпляр обслуживает разные аккаунты одновременно
        token = payload.token or self.token
        if _dry() or not token:
            return PublishResult(external_id=f"dry_{idempotency_key[:10]}", external_url="https://max.ru/dogovor_expert")

        client = self._client or httpx.AsyncClient(timeout=20)
        try:
            if payload.reconcile:
                found = await self._find_published(client, payload, token)
                if found:
                    log.info("max: сообщение уже отправлено, повтор не нужен, id=%s", found)
                    return PublishResult(external_id=found, external_url="https://max.ru/dogovor_expert")
            attachments = await self._upload_media(client, payload.media_urls, token)
            body: dict = {"text": self._plain(payload.text)}
            if attachments:
                body["attachments"] = attachments
            if payload.buttons:
                body["attachments"] = (body.get("attachments") or []) + [
                    {"type": "inline_keyboard", "payload": {"buttons": payload.buttons}}]
            resp = await client.post(
                f"{API}/messages",
                params={"chat_id": payload.account_external_id},
                headers={"Authorization": token},
                json=body,
            )
        finally:
            if self._client is None:
                await client.aclose()
        if resp.status_code == 401:
            raise NormalizedError("auth", "MAX: токен отклонён", raw={"status": 401})
        if resp.status_code == 429:
            raise NormalizedError("rate_limit", "MAX: слишком много запросов",
                                  retry_after=int(resp.headers.get("Retry-After", "5")))
        if resp.status_code >= 500:
            raise NormalizedError("transient", f"MAX: {resp.status_code}")
        if resp.status_code >= 400:
            raise NormalizedError("policy", f"MAX: {resp.text[:200]}")

        body = resp.json()
        mid = str(body.get("message", {}).get("body", {}).get("mid", body.get("message_id", "unknown")))
        return PublishResult(external_id=mid, external_url="https://max.ru/dogovor_expert")

    async def _find_published(self, client, payload: PublishInput, token: str) -> str | None:
        """Поиск уже отправленного сообщения с тем же текстом (защита от дубля после сбоя)."""
        try:
            resp = await client.get(f"{API}/messages",
                                    params={"chat_id": payload.account_external_id, "count": 20},
                                    headers={"Authorization": token})
            if resp.status_code >= 400:
                return None
            probe = self._plain(payload.text)[:80]
            for item in (resp.json().get("messages") or []):
                body = ((item.get("body") or {}).get("text") or "")
                if probe and probe in body:
                    return str((item.get("body") or {}).get("mid") or item.get("id") or "")
        except Exception as exc:                        # noqa: BLE001 — сверка не должна ломать публикацию
            log.warning("max: сверка недоступна (%s)", exc)
        return None

    async def _upload_media(self, client, media_urls: list[str], token: str) -> list[dict]:
        """MAX: загрузка начинается с ``POST /uploads?type=image`` → PUT файла → attachment."""
        out: list[dict] = []
        for url in media_urls:
            try:
                init = await client.post(f"{API}/uploads", params={"type": "image"},
                                         headers={"Authorization": token})
                slot = init.json() if init.status_code < 400 else {}
                upload_url = slot.get("url")
                if not upload_url:
                    log.warning("max: upload-слот не выдан (%s), медиа пропущено", init.status_code)
                    continue
                source = await client.get(url)
                await client.post(upload_url, content=source.content,
                                  headers={"Content-Type": "image/jpeg"})
                out.append({"type": "image", "payload": {"url": upload_url.split("?")[0]}})
            except Exception as exc:                     # noqa: BLE001 — медиа не должно валить пост
                log.warning("max: медиа %s не загружено (%s)", url, exc)
        return out

    @staticmethod
    def _plain(text: str) -> str:
        """Markdown в MAX работает нестабильно — снимаем разметку, оставляем читаемый текст."""
        return (text.replace("**", "").replace("__", "").replace("`", "").strip())
