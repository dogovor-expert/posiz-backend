from __future__ import annotations

from ..catalog import BY_KIND
from ..config import settings
from .base import ChannelAdapter, NormalizedError, PublishInput, PublishResult
from .max import MaxAdapter
from .special import MODE_TO_CLASS
from .telegram import TelegramAdapter
from .vk import VKAdapter


class StubAdapter(ChannelAdapter):
    """Канал без публичного API (или ручной режим): публикация только по подтверждению человеком."""

    capabilities = {"publish": False, "manual": True}

    def __init__(self, channel: str):
        self.channel = channel

    async def publish(self, payload: PublishInput, idempotency_key: str) -> PublishResult:
        """Ручной режим: в dry-run имитируем публикацию, в проде требуем подтверждения человеком."""
        if settings.dry_run:
            return PublishResult(external_id=f"manual_{idempotency_key[:8]}", external_url=None)
        raise NormalizedError("policy", "канал в ручном режиме: подтвердите публикацию в интерфейсе")


ADAPTERS: dict[str, ChannelAdapter] = {
    "telegram": TelegramAdapter(),
    "vk": VKAdapter(),
    "max": MaxAdapter(),
    "ok": StubAdapter("ok"),
    "x": StubAdapter("x"),
    "linkedin": StubAdapter("linkedin"),
    "threads": StubAdapter("threads"),
    "instagram": StubAdapter("instagram"),
    "bluesky": StubAdapter("bluesky"),
}


def _build_from_catalog() -> None:
    """Каналы из карты адаптеров получают свой адаптер по режиму доставки.

    api → StubAdapter (в проде — реальный клиент), rss/headless/blog/webhook/email/local/manual → special.
    """
    for spec in BY_KIND.values():
        if spec.kind in ADAPTERS:
            continue
        cls = MODE_TO_CLASS.get(spec.api_mode)
        ADAPTERS[spec.kind] = cls(spec.kind) if cls else StubAdapter(spec.kind)


_build_from_catalog()


def get_adapter(channel: str, token: str | None = None) -> ChannelAdapter:
    """Адаптер канала. ``token`` — расшифрованный токен конкретного аккаунта.

    Синглтоны в ``ADAPTERS`` не мутируются: если токен передан, возвращается отдельный
    экземпляр с этим токеном. Иначе два аккаунта одного канала в параллельных задачах
    перетирали бы токены друг друга (гонка, из-за которой пост уходил в чужой канал).
    """
    base = ADAPTERS.get(channel) or StubAdapter(channel)
    if not token:
        return base
    if isinstance(base, MaxAdapter):
        return MaxAdapter(token=token)
    if isinstance(base, TelegramAdapter):
        return TelegramAdapter(token=token)
    if isinstance(base, VKAdapter):
        return VKAdapter(community_token=token)
    return base
