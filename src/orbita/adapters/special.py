"""Адаптеры каналов без «обычного» постинга: RSS, headless, блоги, вебхуки, рассылки, карточки.

Смысл: продукт не притворяется, что у площадки есть API. Каждый адаптер честно возвращает
свой способ доставки, а UI показывает пользователю именно его.
"""
from __future__ import annotations

from .. import feed
from ..config import dry_run as _dry, settings
from ..workers import headless
from .base import ChannelAdapter, NormalizedError, PublishInput, PublishResult


class RssAdapter(ChannelAdapter):
    """Дзен: постинга нет, есть автоимпорт ленты. Публикация = валидная запись в RSS.

    Полный текст уходит в ``<yandex:full-text>`` — без него Дзен обрезает анонс и
    публикация теряет смысл. Лента отдаётся по ``GET /feed/{workspace}.xml``.
    """

    capabilities = {"publish": False, "rss": True, "full_text_tag": "yandex:full-text"}

    def __init__(self, channel: str, workspace: str = "dogovor"):
        self.channel = channel
        self.workspace = workspace

    def render(self, items: list[dict], *, title: str | None = None) -> str:
        """Собрать ленту и проверить её требованиями Дзена."""
        base = f"https://{settings.link_domain}/feed/{self.workspace}.xml"
        xml = feed.build(
            title=title or f"{self.workspace}: договоры без юриста",
            link=f"https://{settings.link_domain}/{self.workspace}",
            description="Разборы договоров аренды, подряда и поставки для малого бизнеса",
            items=items, logo=f"https://{settings.link_domain}/logo.png",
        )
        report = feed.validate(xml)
        if not report["ok"]:
            raise NormalizedError("policy", f"лента не прошла валидацию: {report['problems']}")
        return xml

    async def publish(self, payload: PublishInput, idempotency_key: str) -> PublishResult:
        item = {"title": (payload.text.split("\n")[0] or "Пост")[:120],
                "body": payload.text, "url": payload.link_url or f"https://{settings.link_domain}/",
                "guid": idempotency_key, "published_at": None}
        self.render([item])
        return PublishResult(
            external_id=f"rss_{idempotency_key[:8]}",
            external_url=f"https://{settings.link_domain}/feed/{self.workspace}.xml",
        )


class HeadlessAdapter(ChannelAdapter):
    """TenChat / Rutube: официального постинга нет — headless-воркер или черновик + напоминание."""

    capabilities = {"publish": False, "headless": True, "draft_fallback": True}

    def __init__(self, channel: str):
        self.channel = channel

    async def publish(self, payload: PublishInput, idempotency_key: str) -> PublishResult:
        job = headless.Job(channel=self.channel, account=payload.account_external_id,
                           text=payload.text, media=list(payload.media_urls),
                           idempotency_key=idempotency_key)
        result = await headless.run(job)
        if result["status"] == "published":
            return PublishResult(external_id=str(result.get("external_id") or job.job_id),
                                 external_url=result.get("url"))
        if _dry():
            return PublishResult(external_id=f"draft_{self.channel}_{idempotency_key[:6]}",
                                 external_url=None)
        raise NormalizedError(
            "policy",
            f"{self.channel}: {result.get('reason', 'headless недоступен')}. "
            "Пост сохранён черновиком, пользователю отправлено напоминание с готовым текстом.",
        )


class BlogAdapter(ChannelAdapter):
    """VC.ru / Habr: Markdown-черновик и отложенная публикация от имени компании."""

    capabilities = {"publish": False, "draft": True, "moderation": True}

    def __init__(self, channel: str):
        self.channel = channel

    async def publish(self, payload: PublishInput, idempotency_key: str) -> PublishResult:
        if _dry():
            return PublishResult(external_id=f"blog_{idempotency_key[:8]}",
                                 external_url=f"https://{self.channel}.example/draft/{idempotency_key[:8]}")
        raise NormalizedError("policy", f"{self.channel}: черновик создан, публикация — после ревью редакции")


class WebhookAdapter(ChannelAdapter):
    """Discord и подобные: публикация вебхуком в канал сообщества."""

    capabilities = {"publish": True, "embeds": True, "media": ["image", "embed"]}

    def __init__(self, channel: str):
        self.channel = channel

    async def publish(self, payload: PublishInput, idempotency_key: str) -> PublishResult:
        if _dry():
            return PublishResult(external_id=f"wh_{idempotency_key[:8]}",
                                 external_url="https://discord.example/channels/demo")
        raise NormalizedError("auth", "вебхук Discord не настроен: добавьте URL вебхука канала")


class EmailAdapter(ChannelAdapter):
    """Собственный канал: рассылка дайджеста и Web Push — нельзя замедлить или заблокировать."""

    capabilities = {"publish": True, "digest": True, "unsubscribe_required": True}

    def __init__(self, channel: str):
        self.channel = channel

    async def publish(self, payload: PublishInput, idempotency_key: str) -> PublishResult:
        if _dry():
            return PublishResult(external_id=f"msg_{idempotency_key[:8]}",
                                 external_url="https://dogovor.expert/digest/preview")
        raise NormalizedError("auth", f"{self.channel}: провайдер рассылки не подключён")


class LocalAdapter(ChannelAdapter):
    """Яндекс Бизнес / 2ГИС / GBP: карточка организации, новости и отзывы."""

    capabilities = {"publish": True, "reviews": True, "media": ["image"]}

    def __init__(self, channel: str):
        self.channel = channel

    async def publish(self, payload: PublishInput, idempotency_key: str) -> PublishResult:
        if _dry():
            return PublishResult(external_id=f"card_{idempotency_key[:8]}", external_url=None)
        raise NormalizedError("auth", f"{self.channel}: карточка не привязана к аккаунту")


MODE_TO_CLASS: dict[str, type[ChannelAdapter]] = {
    "rss": RssAdapter,
    "headless": HeadlessAdapter,
    "blog": BlogAdapter,
    "webhook": WebhookAdapter,
    "email": EmailAdapter,
    "local": LocalAdapter,
    "manual": HeadlessAdapter,
}
