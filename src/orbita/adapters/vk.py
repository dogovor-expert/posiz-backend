from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx

from ..config import dry_run as _dry, settings
from .base import ChannelAdapter, NormalizedError, PublishInput, PublishResult

API = "https://api.vk.com/method"
V = "5.199"
log = logging.getLogger("orbita.vk")

IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp")


class VKAdapter(ChannelAdapter):
    """VK: публикация в сообщество токеном сообщества, медиа — двухшаговой загрузкой.

    Особенности (живые проверки 2026):
      * community-token пишет, но не читает/не редактирует — два токена на канал;
      * ошибки 27/1051 значат «этому типу токена нельзя» — переключаемся на service key
        и НЕ ретраим write;
      * лимит 20 rps (service) / 3 rps (user);
      * фото нельзя приложить ссылкой: ``photos.getWallUploadServer`` → загрузка файла →
        ``photos.saveWallPhoto`` → ``wall.post`` с ``attachments``;
      * ``guid`` гасит дубли на стороне VK, но при таймауте ответа публикация могла
        состояться — поэтому есть reconcile по последним постам стены.
    """

    channel = "vk"
    capabilities = {"publish": True, "edit": False, "delete": False,
                    "media": ["image", "video", "carousel"], "buttons": False, "markdown": "limited"}

    def __init__(self, community_token: str | None = None, service_key: str | None = None,
                 client: httpx.AsyncClient | None = None):
        self._community_token = community_token
        self._service_key = service_key
        self._client = client

    # Токены читаются в момент вызова: переключение окружения без перезапуска процесса
    @property
    def community_token(self) -> str:
        return self._community_token or os.getenv("VK_COMMUNITY_TOKEN") or settings.vk_community_token

    @property
    def service_key(self) -> str:
        return self._service_key or os.getenv("VK_SERVICE_KEY") or settings.vk_service_key

    # ---------------- транспорт ----------------
    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=20)
            self._owns = True
        else:
            self._owns = False
        return self._client

    async def _call(self, method: str, params: dict[str, Any],
                    upload: bool = False, token: str | None = None) -> Any:
        url = method if upload else f"{API}/{method}"
        client = self._http()
        try:
            if upload:
                resp = await client.post(url, data=params)      # multipart отдаём как files
            else:
                body = {**params, "v": V}
                resp = await client.post(url, data=body,
                                         headers={"Authorization": f"Bearer {token or self.community_token}"})
        except httpx.TimeoutException as exc:
            raise NormalizedError("transient", f"VK таймаут ({method})", raw={"timeout": str(exc)})
        try:
            data = resp.json()
        except ValueError as exc:
            raise NormalizedError("transient", f"VK вернул не-JSON ({method})") from exc
        if "error" in data:
            self._raise(data["error"])
        return data.get("response", data)

    @staticmethod
    def _raise(err: dict) -> None:
        code = err.get("error_code")
        if code in (27, 1051):
            raise NormalizedError(
                "auth", "токен не имеет прав на запись (27/1051) — нужен другой тип токена", raw=err)
        if code == 6:
            raise NormalizedError("rate_limit", "too many requests per second", retry_after=1, raw=err)
        if code == 14:
            raise NormalizedError("policy", "captcha required", raw=err)
        raise NormalizedError("transient", err.get("error_msg", "vk error"), raw=err)

    # ---------------- медиа ----------------
    async def upload_photo(self, group_id: str, media_url: str) -> str:
        """Двухшаговая загрузка фото: вернуть attachment вида ``photo-123_456``."""
        client = self._http()
        prepared = await client.get(media_url)             # фото должно быть реальным файлом
        if prepared.status_code >= 400:
            raise NormalizedError("transient", f"медиа недоступно: {media_url} ({prepared.status_code})")
        server = await self._call("photos.getWallUploadServer",
                                  {"group_id": group_id.replace("-", "")})
        upload_url = server["upload_url"]
        resp = await client.post(
            upload_url,
            files={"photo": ("photo.jpg", prepared.content, "image/jpeg")},
        )
        payload = resp.json()
        saved = await self._call("photos.saveWallPhoto", {
            "group_id": group_id.replace("-", ""),
            "photo": payload.get("photo"),
            "server": payload.get("server"),
            "hash": payload.get("hash"),
        })
        if not saved:
            raise NormalizedError("transient", "VK не вернул сохранённое фото")
        photo = saved[0]
        return f"photo{photo['owner_id']}_{photo['id']}"

    async def upload_video(self, group_id: str, media_url: str, *, name: str = "video") -> str:
        """Видео: ``video.save`` отдаёт upload_url, затем PUT файла; attachment — ``video-1_2``."""
        client = self._http()
        saved = await self._call("video.save", {
            "group_id": group_id.replace("-", ""), "name": name[:100],
            "is_private": 1, "wallpost": 1,
        })
        upload_url = saved.get("upload_url")
        if upload_url and media_url:
            source = await client.get(media_url)
            await client.post(upload_url, files={"video_file": ("video.mp4", source.content)})
        return f"video{saved['owner_id']}_{saved['video_id']}"

    async def _attachments(self, group_id: str, payload: PublishInput) -> list[str]:
        attachments: list[str] = []
        for url in payload.media_urls:
            lower = url.lower().split("?")[0]
            if lower.endswith(IMAGE_EXT):
                attachments.append(await self.upload_photo(group_id, url))
            elif lower.endswith((".mp4", ".mov")):
                attachments.append(await self.upload_video(group_id, url))
            else:
                log.warning("vk: пропускаем медиа неизвестного типа %s", url)
        if payload.link_url and not payload.link_in_reply:
            attachments.append(payload.link_url)
        return attachments

    # ---------------- reconcile ----------------
    async def find_published(self, owner_id: str, text: str) -> str | None:
        """Пост уже на стене? Ищем по точному совпадению текста среди последних 20 записей.

        Нужно после таймаута/сетевой ошибки: VK мог принять публикацию, а ответ — нет.
        """
        try:
            resp = await self._call("wall.get", {"owner_id": owner_id, "count": 20,
                                                 "filter": "owner"})
        except NormalizedError as exc:
            log.warning("vk reconcile: не смогли прочитать стену (%s)", exc)
            return None
        target = _normalize(text)
        for item in (resp.get("items") if isinstance(resp, dict) else []) or []:
            if _normalize(item.get("text", "")) == target:
                return str(item["id"])
        return None

    # ---------------- публикация ----------------
    async def publish(self, payload: PublishInput, idempotency_key: str) -> PublishResult:
        # токен сообщества из хранилища приоритетнее значения по умолчанию
        token = payload.token or self.community_token
        if _dry() or not token:
            return PublishResult(external_id=f"dry_{idempotency_key[:10]}",
                                 external_url="https://vk.com/wall-1_1")

        owner_id = str(payload.account_external_id)     # отрицательный id сообщества
        if payload.reconcile:
            # прошлая попытка оборвалась: сначала смотрим, не вышел ли пост уже
            found = await self.find_published(owner_id, payload.text)
            if found:
                log.info("vk: пост уже в канале, повтор не нужен, post_id=%s", found)
                return PublishResult(external_id=found, external_url=_url(owner_id, found))
        attachments = await self._attachments(owner_id, payload)
        params: dict[str, Any] = {
            "owner_id": owner_id,
            "from_group": 1,
            "message": payload.text,
            "guid": idempotency_key,                    # защита от дублей на стороне VK
        }
        if attachments:
            params["attachments"] = ",".join(a for a in attachments if a)

        try:
            data = await self._call("wall.post", params, token=token)
        except NormalizedError as exc:
            if exc.retryable:                            # могло уже опубликоваться
                found = await self.find_published(owner_id, payload.text)
                if found:
                    log.info("vk: публикация восстановлена reconcile-ом, post_id=%s", found)
                    return PublishResult(external_id=found, external_url=_url(owner_id, found))
            raise

        post_id = str(data["post_id"])
        return PublishResult(external_id=post_id, external_url=_url(owner_id, post_id))

    async def health_check(self, account_external_id: str) -> dict:
        if _dry() or not self.community_token:
            return {"ok": True, "delivery_risk": "low", "mode": "dry_run"}
        try:
            await self._call("groups.getById", {"group_id": account_external_id.replace("-", "")})
            return {"ok": True, "delivery_risk": "low", "mode": "live"}
        except NormalizedError as exc:
            return {"ok": False, "delivery_risk": "high", "error": str(exc), "kind": exc.kind}

    def rate_budget(self) -> dict:
        return {"per_minute": 20, "per_day": None}


def _normalize(text: str) -> str:
    return json.dumps(" ".join(text.split()), ensure_ascii=False)


def _url(owner_id: str, post_id: str) -> str:
    owner = str(owner_id).replace("-", "")
    return f"https://vk.com/wall-{owner}_{post_id}"
