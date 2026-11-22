"""Контракт адаптера канала."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Literal

ErrorKind = Literal["rate_limit", "auth", "policy", "transient", "fatal"]


@dataclass
class PublishInput:
    """Вход публикации.

    ``token`` — токен **конкретного аккаунта канала** из хранилища (расшифрован на
    стороне ядра). Раньше адаптеры читали только переменные окружения процесса,
    поэтому подключённый клиентом канал публиковал «не туда» или не публиковал вовсе.
    """

    account_external_id: str
    text: str
    media_urls: list[str] = field(default_factory=list)
    link_url: str | None = None
    link_in_reply: bool = False
    buttons: list[dict] = field(default_factory=list)
    token: str | None = None
    #: ``True`` — прошлая попытка осталась незакрытой: сначала проверь, не опубликовано ли
    #: уже (поиск по тексту/guid), и только потом отправляй.
    reconcile: bool = False


@dataclass
class PublishResult:
    external_id: str
    external_url: str | None = None
    delivered_at: str | None = None


class NormalizedError(Exception):
    """Ошибка канала, приведённая к 5 классам с разной политикой ретраев."""

    def __init__(self, kind: ErrorKind, message: str, retry_after: int | None = None, raw: dict | None = None):
        super().__init__(message)
        self.kind = kind
        self.retry_after = retry_after
        self.raw = raw or {}

    @property
    def retryable(self) -> bool:
        return self.kind in {"rate_limit", "transient"}


class ChannelAdapter(ABC):
    """Реализация — на канал. Ядро не знает специфики платформ."""

    channel: str
    capabilities: dict = {}

    @abstractmethod
    async def publish(self, payload: PublishInput, idempotency_key: str) -> PublishResult: ...

    async def health_check(self, account_external_id: str) -> dict:
        return {"ok": True, "delivery_risk": "low"}

    def rate_budget(self) -> dict:
        return {"per_minute": 20, "per_day": None}
