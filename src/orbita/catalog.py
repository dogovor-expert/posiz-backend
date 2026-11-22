"""Карта каналов: 29 площадок и способ интеграции с каждой.

``api_mode`` отвечает на вопрос «как технически публиковать», а не «есть ли кнопка»:

* ``api``      — официальный API с публикацией (MAX, VK, Telegram, X, Pinterest, Discord, VK Видео);
* ``rss``      — публикации нет, есть автоимпорт ленты (Дзен): генерируем RSS с ``yandex:full-text``;
* ``headless`` — официального постинга нет (TenChat): Playwright-воркер в изолированном контейнере
                 либо режим «черновик + напоминание», включённый по умолчанию;
* ``blog``     — блог-платформа: Markdown-черновик + отложенная публикация/синдикация (VC.ru, Habr);
* ``webhook``  — каналы сообществ (Discord), публикация вебхуком;
* ``local``    — карточка организации (Яндекс Бизнес, 2ГИС): новости и отзывы;
* ``email``    — собственный канал: рассылка (UniSender/Sendsay/Mailgun) и Web Push;
* ``manual``   — ручной режим (Twitch/Kick — только уведомления, нет постинга).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

P0 = "p0"   # РФ-ядро: без этого продукт не продаётся
P1 = "p1"   # расширение охвата
P2 = "p2"   # нишевые/комьюнити


@dataclass(frozen=True)
class ChannelSpec:
    kind: str
    label: str
    group: str                 # rf | global | owned | community | local
    api_mode: str
    priority: str = P1
    publish: bool = True
    read: bool = True
    media: tuple[str, ...] = ()
    limit: str = ""
    connect: str = "oauth"     # oauth | token | webhook | rss | manual
    risk: str = "low"          # low | medium | high | blocked
    note: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)


CATALOG: tuple[ChannelSpec, ...] = (
    # ---------- ядро РФ ----------
    ChannelSpec("max", "MAX", "rf", "api", P0, True, True, ("image", "video", "document"),
                "4000", "token", "low", "Bot API platform-api.max.ru, ИП/ООО + верификация 1–48 ч",
                ("messenger", "p0")),
    ChannelSpec("vk", "ВКонтакте", "rf", "api", P0, True, True, ("image", "video", "carousel"),
                "4000", "oauth", "low", "community token пишет, service key читает", ("social", "p0")),
    ChannelSpec("telegram", "Telegram", "rf", "api", P0, True, True, ("image", "video", "document", "album"),
                "4096", "token", "high", "РКН: ограничения с 10.02.2026, доставка нестабильна",
                ("messenger", "p0")),
    ChannelSpec("ok", "Одноклассники", "rf", "api", P0, True, True, ("image",), "3000", "oauth", "medium",
                "только посты и фото, строгая модерация", ("social",)),
    # ---------- расширение охвата РФ ----------
    ChannelSpec("dzen", "Дзен", "rf", "rss", P0, False, False, ("image",), "—", "rss", "low",
                "постинга по API нет: автоимпорт RSS с yandex:full-text", ("seo", "longread")),
    ChannelSpec("tenchat", "TenChat", "rf", "headless", P1, False, False, ("image",), "—", "manual", "medium",
                "деловая соцсеть, алгоритм «Зевс»; headless-воркер или черновик + напоминание", ("b2b",)),
    ChannelSpec("vk_video", "VK Видео", "rf", "api", P1, True, True, ("video", "clip"), "—", "oauth", "low",
                "video.save + resumable upload через VK API", ("video",)),
    ChannelSpec("rutube", "Rutube", "rf", "headless", P1, False, False, ("video", "clip"), "—", "manual", "medium",
                "партнёрский API или сессионный uploader; клипы и длинные разборы", ("video",)),
    ChannelSpec("vcru", "VC.ru", "rf", "blog", P1, False, True, ("image",), "—", "manual", "low",
                "B2B-доверие: Markdown-черновик + публикация в блог компании", ("b2b", "longread")),
    ChannelSpec("habr", "Habr", "rf", "blog", P2, False, True, ("image",), "—", "manual", "medium",
                "строгая модерация, черновик + публикация от имени компании", ("b2b", "longread")),
    ChannelSpec("yandex_business", "Яндекс Бизнес", "local", "local", P1, True, True, ("image",), "—", "oauth",
                "low", "новости и акции в карточку, отзывы в Social Inbox", ("local", "reputation")),
    ChannelSpec("twogis", "2ГИС", "local", "local", P1, True, True, ("image",), "—", "oauth", "low",
                "карточка организации, отзывы, акции", ("local", "reputation")),
    # ---------- глобальные ----------
    ChannelSpec("x", "X (Twitter)", "global", "api", P1, True, True, ("image",), "280", "oauth", "medium",
                "pay-per-use: $0,015/пост, $0,20 со ссылкой в тексте", ("social",)),
    ChannelSpec("pinterest", "Pinterest", "global", "api", P1, True, True, ("image",), "500", "oauth", "low",
                "Pinterest API v5: пины 1000×1500 со ссылкой на шаблон — вечный поисковый трафик",
                ("seo", "visual")),
    ChannelSpec("linkedin", "LinkedIn", "global", "api", P2, True, True, ("image", "document"), "3000", "oauth",
                "medium", "доступ к posting API только для партнёров", ("b2b",)),
    ChannelSpec("threads", "Threads", "global", "api", P2, True, True, ("image",), "500", "oauth", "medium",
                "Meta: 250 постов/24ч, App Review обязателен", ("social",)),
    ChannelSpec("instagram", "Instagram", "global", "api", P2, True, True, ("image", "video", "carousel"),
                "2200", "oauth", "high", "100 постов/24ч, реклама в РФ запрещена (72-ФЗ)", ("social", "visual")),
    ChannelSpec("facebook", "Facebook", "global", "api", P2, True, True, ("image", "video"), "63206", "oauth",
                "high", "25 постов/сутки, реклама в РФ запрещена", ("social",)),
    ChannelSpec("youtube", "YouTube", "global", "api", P2, True, True, ("video", "short"), "5000", "oauth",
                "medium", "ограниченный ресурс, переходный период до 31.12.2026", ("video",)),
    ChannelSpec("bluesky", "Bluesky", "global", "api", P2, True, True, ("image",), "300", "token", "low", "",
                ("social",)),
    ChannelSpec("mastodon", "Mastodon", "global", "api", P2, True, True, ("image",), "500", "token", "low", "",
                ("social",)),
    ChannelSpec("gbp", "Google Business Profile", "local", "local", P2, True, True, ("image",), "1500", "oauth",
                "medium", "локальные посты и отзывы; недоступно для РФ-адресов", ("local",)),
    # ---------- комьюнити ----------
    ChannelSpec("discord", "Discord", "community", "webhook", P1, True, True, ("image", "embed"), "2000",
                "webhook", "low", "Embed-карточки в каналы: анонсы, статусы, релизы", ("community",)),
    ChannelSpec("twitch", "Twitch", "community", "manual", P2, True, False, (), "500", "oauth", "medium",
                "только анонсы и chat-бот, постинга нет", ("community", "stream")),
    ChannelSpec("kick", "Kick", "community", "manual", P2, False, False, (), "—", "manual", "medium",
                "публичного постинга нет: уведомления сообщества", ("community", "stream")),
    # ---------- собственные каналы (нельзя заблокировать) ----------
    ChannelSpec("email", "Email-рассылка", "owned", "email", P0, True, True, ("image", "html"), "—", "token",
                "low", "UniSender / Sendsay / Mailgun: дайджест лучших постов недели", ("owned", "digest")),
    ChannelSpec("webpush", "Web Push", "owned", "email", P1, True, True, ("image",), "—", "token", "low",
                "мгновенные push-уведомления подписчикам сайта", ("owned",)),
    ChannelSpec("vk_messenger", "VK Мессенджер", "rf", "api", P2, True, True, (), "4000", "oauth", "low", "",
                ("messenger",)),
    ChannelSpec("whatsapp", "WhatsApp", "global", "api", P2, True, True, ("image", "document"), "4096", "token",
                "blocked", "WABA только для зарубежных рынков; в РФ ограничен с 11–12.02.2026", ("messenger",)),
)

BY_KIND: dict[str, ChannelSpec] = {c.kind: c for c in CATALOG}


def describe(kind: str) -> dict:
    spec = BY_KIND.get(kind)
    if not spec:
        return {"kind": kind, "api_mode": "unknown", "publish": False,
                "note": "канал не описан в карте адаптеров"}
    data = asdict(spec)
    data["delivery"] = {"api": "прямая публикация", "rss": "автоимпорт ленты",
                        "headless": "черновик + напоминание / headless-воркер",
                        "blog": "Markdown-черновик + публикация",
                        "webhook": "вебхук сообщества", "local": "карточка организации",
                        "email": "рассылка", "manual": "ручной режим"}.get(spec.api_mode, "—")
    return data


def summary() -> dict:
    by_mode: dict[str, int] = {}
    for c in CATALOG:
        by_mode[c.api_mode] = by_mode.get(c.api_mode, 0) + 1
    return {
        "total": len(CATALOG),
        "by_mode": by_mode,
        "by_group": {g: sum(1 for c in CATALOG if c.group == g) for g in {c.group for c in CATALOG}},
        "direct_publish": sum(1 for c in CATALOG if c.publish and c.api_mode in {"api", "webhook", "email", "local"}),
        "no_public_api": [c.kind for c in CATALOG if c.api_mode in {"rss", "headless", "blog", "manual"}],
    }
