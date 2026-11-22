from __future__ import annotations

import os

import httpx

from ..config import dry_run as _dry, settings
from .base import ChannelAdapter, NormalizedError, PublishInput, PublishResult

API = "https://api.telegram.org"


class TelegramAdapter(ChannelAdapter):
    """Bot API: бот должен быть админом канала.

    Лимиты: ~20 сообщений/мин в один чат. Ошибки: 429 -> rate_limit, 401/403 -> auth.
    """

    channel = "telegram"
    capabilities = {"publish": True, "edit": True, "delete": True, "media": ["image", "video", "album"],
                    "buttons": True, "markdown": "markdown_v2"}

    def __init__(self, token: str | None = None):
        self._token = token

    @property
    def token(self) -> str:
        return self._token or os.getenv("TELEGRAM_BOT_TOKEN") or settings.telegram_bot_token

    async def publish(self, payload: PublishInput, idempotency_key: str) -> PublishResult:
        # токен аккаунта из хранилища приоритетнее переменной окружения процесса
        token = payload.token or self.token
        if _dry() or not token:
            return PublishResult(external_id=f"dry_{idempotency_key[:10]}",
                                 external_url="https://t.me/dogovor_expert/1")

        async with httpx.AsyncClient(timeout=20) as client:
            if payload.reconcile:
                found = await self._find_published(client, payload, token)
                if found:
                    log.info("telegram: сообщение уже отправлено, повтор не нужен, id=%s", found)
                    return PublishResult(external_id=found, external_url="https://t.me/dogovor_expert/1")
            resp = await client.post(
                f"{API}/bot{token}/sendMessage",
                json={
                    "chat_id": payload.account_external_id,
                    "text": payload.text,
                    "parse_mode": "MarkdownV2",
                    "disable_web_page_preview": False,
                },
            )
        data = resp.json()
        if not data.get("ok"):
            code = data.get("error_code")
            if code == 429:
                raise NormalizedError("rate_limit", data.get("description", "too many requests"),
                                     retry_after=data.get("parameters", {}).get("retry_after"), raw=data)
            if code in (401, 403):
                raise NormalizedError("auth", data.get("description", "forbidden"), raw=data)
            if code == 400:
                raise NormalizedError("policy", data.get("description", "bad request"), raw=data)
            raise NormalizedError("transient", data.get("description", "unknown"), raw=data)

        msg = data["result"]
        chat = str(msg.get("chat", {}).get("username") or payload.account_external_id)
        return PublishResult(external_id=str(msg["message_id"]),
                             external_url=f"https://t.me/{chat}/{msg['message_id']}")

    async def _find_published(self, client, payload: PublishInput, token: str) -> str | None:
        """Сверка: есть ли в чате сообщение с тем же началом текста (дубль после сбоя)."""
        try:
            resp = await client.get(f"{API}/bot{token}/getUpdates",
                                    params={"limit": 20, "allowed_updates": ["channel_post"]})
            if resp.status_code >= 400:
                return None
            probe = payload.text.strip()[:60]
            for update in resp.json().get("result") or []:
                post = update.get("channel_post") or {}
                if probe and probe in (post.get("text") or ""):
                    return str(post.get("message_id") or "")
        except Exception as exc:                        # noqa: BLE001
            log.warning("telegram: сверка недоступна (%s)", exc)
        return None
