"""SEO-контур Posiz: robots, sitemap, JSON-LD и карта URL маркетингового сайта.

Задача модуля — сделать стратегию продвижения исполняемой, а не «текстом в документе»:

* ``robots_txt`` — одна точка правды: что индексируется (маркетинг), что закрыто
  (приложение, короткие ссылки, Link-in-Bio клиентов);
* ``sitemap_xml`` — собирается из каталога URL (интеграции, связки, решения, утилиты, блог),
  поэтому новые страницы попадают в карту автоматически, а не «руками в редакторе»;
* JSON-LD-билдеры: ``SoftwareApplication`` (с тарифами), ``FAQPage``, ``HowTo``, ``Article``,
  ``BreadcrumbList``. Разметка нужна и для расширенных сниппетов, и для попадания в ответы
  нейропоиска (Яндекс Нейро, AI Overviews): структурированные данные + блок «Коротко».

Сам маркетинговый сайт — отдельный Next.js-контур (`posiz.ru`), вне этого репозитория.
Здесь живёт контракт: какие URL существуют, как они помечены и какие данные отдаёт API.
"""
from __future__ import annotations

import html
import json
from datetime import date
from typing import Any, Iterable
from xml.sax.saxutils import escape as xml_escape

from . import catalog

SITE = "https://posiz.ru"
APP = "https://app.posiz.ru"
API = "https://api.posiz.ru"
SITE_NAME = "Posiz"
LEGAL_NAME = "ООО «Позиц»"

# тарифы: (код, название, ₽/мес, краткое описание)
PLANS: list[tuple[str, str, int, str]] = [
    ("free", "Free", 0, "1 бренд, 2 канала, 10 публикаций в месяц"),
    ("start", "Старт", 990, "4 канала, 60 публикаций, Brand Brain 200 страниц"),
    ("business", "Бизнес", 2490, "8 каналов, 300 публикаций, инбокс и комплаенс"),
    ("pro", "Про", 4990, "20 каналов, 1 500 публикаций, API и MCP, 3 бренда"),
    ("agency", "Агентство", 12900, "60 каналов, white-label, отчёты клиентам"),
]

# ниши для /solutions/<niche>
NICHES: list[tuple[str, str]] = [
    ("lawyers", "Юристы и адвокаты"),
    ("clinics", "Медицинские клиники"),
    ("accounting", "Бухгалтерские компании"),
    ("ecommerce", "E-commerce и маркетплейсы"),
    ("schools", "Онлайн-школы"),
    ("agencies", "SMM-агентства"),
    ("realestate", "Недвижимость и застройщики"),
    ("fitness", "Фитнес и студии"),
    ("restaurants", "Рестораны и доставка"),
    ("manufacturing", "Производство и B2B"),
    ("it", "IT и SaaS"),
    ("beauty", "Салоны красоты"),
    ("auto", "Автосервисы и автосалоны"),
    ("education", "Репетиторы и курсы"),
    ("construction", "Строительство и ремонт"),
    ("logistics", "Логистика и грузоперевозки"),
    ("finance", "Финансовые консультанты"),
    ("hr", "HR и рекрутинг"),
    ("travel", "Туризм"),
    ("pets", "Зообизнес"),
    ("photo", "Фотографы и ивенты"),
    ("handmade", "Handmade и крафт"),
    ("nonprofit", "НКО и фонды"),
    ("agro", "Агробизнес"),
]

# утилиты /tools/<tool>
TOOLS: list[tuple[str, str, str]] = [
    ("erid-checker", "Проверка текста на признаки рекламы", "erid и 38-ФЗ: нужна ли маркировка"),
    ("risk-calculator", "Калькулятор риска охвата", "сколько аудитории теряется при блокировке канала"),
    ("thread-splitter", "Нарезка лонгрида в тред", "X, Threads, Bluesky с нумерацией"),
    ("utm-builder", "Генератор UTM и смета доставки", "ссылки с UTM и стоимость публикации"),
]

# рубрики блога и материалы (по контент-плану, 21 статья)
BLOG: dict[str, tuple[str, list[tuple[str, str]]]] = {
    "compliance": ("Законы и маркировка", [
        ("samoreklama-max-vk-telegram-2026", "Самореклама в MAX, VK и Telegram в 2026: когда нужен erid"),
        ("reestr-bloggerov-rkn-10000", "Реестр блогеров РКН 10 000+: пошаговая инструкция"),
        ("shtrafy-reklama-ogranichennye-resursy", "Штрафы за рекламу на ограниченных ресурсах"),
        ("sbor-3-procenta-erir", "Сбор 3% с интернет-рекламы: как учитывать данные в ЕРИР"),
        ("socseti-yurista-kliniki-zakon", "Как юристу и клинике вести соцсети без нарушений"),
        ("markirovka-storis-video-erid", "Маркировка кружков, сторис и видео: где размещать erid"),
    ]),
    "channels": ("Гайды по площадкам", [
        ("verifikaciya-max-gosuslugi", "Верификация ИП/ООО в MAX через Госуслуги за 24 часа"),
        ("pereezd-iz-whatsapp-telegram", "Куда переводить подписчиков из WhatsApp и Telegram"),
        ("avtoimport-dzen-rss", "Автоимпорт статей в Дзен через RSS без потери форматирования"),
        ("x-ssylka-v-otvete", "Почему ссылка в X стоит в 13 раз дороже и как вынести её в ответ"),
        ("tenchat-rutube-api", "Правда об API TenChat и Rutube: публикация без ручного копирования"),
        ("vk-api-krossposting", "VK API: кросспостинг без ошибок доступа и банов"),
    ]),
    "cases": ("Кейсы с цифрами", [
        ("dogovor-expert-6-mesyacev", "Как dogovor.expert заменил SMM-отдел роем агентов: отчёт за 6 месяцев"),
        ("odin-infopovod-5-postov", "Один инфоповод → 5 постов за 4 минуты"),
        ("x-api-minus-90", "Как сократить расходы на API X на 90%"),
        ("max-vs-telegram-lidy", "Сколько лидов приносит дистрибуция в MAX против Telegram"),
        ("sebestoimost-kampanii-2-rublya", "Оптимизация токенов LLM: кампания за 2 рубля"),
    ]),
    "playbooks": ("Плейбуки AI-маркетинга", [
        ("obuchenie-brand-brain", "Как обучить Brand Brain на своих статьях без галлюцинаций"),
        ("kontent-plan-na-mesyac", "Готовый контент-план на месяц под изменения в законах"),
        ("anatomiya-lid-magnita", "Анатомия лид-магнита: договор или чек-лист в вирусном посте"),
        ("prompt-tone-of-voice", "Промпт для Tone of Voice: как составить системный промпт"),
    ]),
}

# источники для связок кросс-постинга
SYNC_SOURCES = ["telegram", "vk", "max", "dzen", "ok", "instagram", "whatsapp",
                "x", "youtube", "rutube", "tenchat", "threads"]
SYNC_TARGETS = ["max", "vk", "telegram", "dzen", "ok"]

# что нельзя индексировать (302-редиректы и пользовательский контент)
NOINDEX_PREFIXES = ("/l/", "/@", "/bio/")


# ---------------------------------------------------------------- robots / sitemap
def robots_txt(host: str = SITE) -> str:
    """robots.txt маркетингового контура: открыто всё, кроме служебных зон."""
    return (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /api/\n"
        "Disallow: /l/\n"
        "Disallow: /@\n"
        "Disallow: /auth/\n"
        f"Crawl-delay: 1\n\n"
        f"# Приложение закрыто целиком: {APP}/robots.txt → Disallow: /\n"
        f"Sitemap: {host}/sitemap.xml\n"
    )


def app_robots_txt() -> str:
    return "User-agent: *\nDisallow: /\n"


def sitemap_entries() -> list[dict[str, Any]]:
    """Каталог URL sitemap: маркетинг + pSEO-кластеры + блог. Без /l/ и /@."""
    today = date.today().isoformat()
    entries: list[dict[str, Any]] = [
        {"loc": f"{SITE}/", "changefreq": "weekly", "priority": "1.0"},
        {"loc": f"{SITE}/pricing", "changefreq": "monthly", "priority": "0.9"},
        # хабы — без завершающего слэша: канонический URL должен отвечать 200,
        # а не 308 (Next.js с trailingSlash: false редиректит «/integrations/» → «/integrations»)
        {"loc": f"{SITE}/integrations", "changefreq": "weekly", "priority": "0.8"},
        {"loc": f"{SITE}/sync", "changefreq": "weekly", "priority": "0.7"},
        {"loc": f"{SITE}/solutions", "changefreq": "weekly", "priority": "0.7"},
        {"loc": f"{SITE}/tools", "changefreq": "weekly", "priority": "0.7"},
        {"loc": f"{SITE}/blog", "changefreq": "daily", "priority": "0.8"},
    ]
    for item in catalog.CATALOG:
        entries.append({"loc": f"{SITE}/integrations/{item.kind}",
                        "changefreq": "monthly", "priority": "0.8"})
    for source in SYNC_SOURCES:
        for target in SYNC_TARGETS:
            if source == target:
                continue
            entries.append({"loc": f"{SITE}/sync/{source}-to-{target}",
                            "changefreq": "monthly", "priority": "0.7"})
    for slug, _title in NICHES:
        entries.append({"loc": f"{SITE}/solutions/{slug}",
                        "changefreq": "monthly", "priority": "0.7"})
    for slug, _title, _hint in TOOLS:
        entries.append({"loc": f"{SITE}/tools/{slug}",
                        "changefreq": "monthly", "priority": "0.6"})
    for category, (_title, articles) in BLOG.items():
        entries.append({"loc": f"{SITE}/blog/{category}",
                        "changefreq": "weekly", "priority": "0.7"})
        for slug, _headline in articles:
            entries.append({"loc": f"{SITE}/blog/{category}/{slug}",
                            "changefreq": "monthly", "priority": "0.6"})
    for entry in entries:
        entry.setdefault("lastmod", today)
    return entries


def sitemap_xml(entries: Iterable[dict[str, Any]] | None = None) -> str:
    rows = []
    for entry in entries or sitemap_entries():
        rows.append(
            "  <url>"
            f"<loc>{xml_escape(entry['loc'])}</loc>"
            f"<lastmod>{entry['lastmod']}</lastmod>"
            f"<changefreq>{entry['changefreq']}</changefreq>"
            f"<priority>{entry['priority']}</priority>"
            "</url>"
        )
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            + "\n".join(rows) + "\n</urlset>\n")


def inventory() -> dict[str, Any]:
    """Сколько страниц даёт каждый кластер — для отчёта и для теста плана."""
    entries = sitemap_entries()
    counts = {"integrations": len(catalog.CATALOG),
              "sync": len(SYNC_SOURCES) * len(SYNC_TARGETS) - len(
                  set(SYNC_SOURCES) & set(SYNC_TARGETS)),
              "solutions": len(NICHES), "tools": len(TOOLS),
              "blog_categories": len(BLOG),
              "blog_articles": sum(len(a) for _t, a in BLOG.values())}
    counts["total"] = len(entries)
    return counts


# ---------------------------------------------------------------- JSON-LD
def _script(payload: dict[str, Any]) -> str:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f'<script type="application/ld+json">{body}</script>'


def software_application(url: str = f"{SITE}/", description: str | None = None) -> dict[str, Any]:
    """SoftwareApplication с тарифами: расширенный сниппет «цена от» в выдаче."""
    return {
        "@context": "https://schema.org",
        "@type": "SoftwareApplication",
        "name": SITE_NAME,
        "applicationCategory": "BusinessApplication",
        "operatingSystem": "Web",
        "url": url,
        "description": description or (
            "AI-хаб управления соцсетями и мессенджерами: Brand Brain, рой агентов, "
            "публикация в 29 каналов, комплаенс РФ и маркировка erid."
        ),
        "inLanguage": "ru-RU",
        "publisher": {"@type": "Organization", "name": LEGAL_NAME, "url": SITE},
        "offers": {
            "@type": "OfferCatalog",
            "name": "Тарифы Posiz",
            "itemListElement": [
                {
                    "@type": "Offer",
                    "name": plan,
                    "price": price,
                    "priceCurrency": "RUB",
                    "url": f"{SITE}/pricing#{code}",
                    "description": desc,
                    "availability": "https://schema.org/InStock",
                }
                for code, plan, price, desc in PLANS
            ],
        },
        "featureList": [
            "публикация в 29 каналах (MAX, VK, Telegram, Дзен, TenChat и др.)",
            "Brand Brain и гибридный поиск по знаниям бренда",
            "маркировка рекламы: erid через ОРД API",
            "единый инбокс с маскированием персональных данных",
        ],
    }


def faq_page(items: list[tuple[str, str]] | list[dict[str, str]]) -> dict[str, Any]:
    def pair(item):
        if isinstance(item, dict):
            return item["q"], item["a"]
        return item

    return {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [
            {"@type": "Question", "name": q,
             "acceptedAnswer": {"@type": "Answer", "text": a}}
            for q, a in (pair(i) for i in items)
        ],
    }


def how_to(name: str, steps: list[tuple[str, str]], *, total_time: str = "PT15M",
           url: str | None = None) -> dict[str, Any]:
    payload = {
        "@context": "https://schema.org",
        "@type": "HowTo",
        "name": name,
        "totalTime": total_time,
        "step": [
            {"@type": "HowToStep", "position": i, "name": title, "text": text}
            for i, (title, text) in enumerate(steps, start=1)
        ],
    }
    if url:
        payload["url"] = url
    return payload


def article(headline: str, *, author: str, section: str, published: str,
            updated: str | None = None, url: str, description: str = "") -> dict[str, Any]:
    payload = {
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": headline[:110],
        "inLanguage": "ru-RU",
        "author": {"@type": "Person", "name": author},
        "publisher": {"@type": "Organization", "name": LEGAL_NAME, "url": SITE},
        "articleSection": section,
        "datePublished": published,
        "dateModified": updated or published,
        "mainEntityOfPage": {"@type": "WebPage", "@id": url},
    }
    if description:
        payload["description"] = description
    return payload


def breadcrumbs(trail: list[tuple[str, str]]) -> dict[str, Any]:
    return {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": i, "name": name, "item": url}
            for i, (name, url) in enumerate(trail, start=1)
        ],
    }


def head_tags(kind: str, **kwargs) -> str:
    """Готовый набор ``<script type="application/ld+json">`` для Next.js `head`."""
    if kind == "software":
        payload = software_application(**kwargs)
    elif kind == "faq":
        payload = faq_page(**kwargs)
    elif kind == "howto":
        payload = how_to(**kwargs)
    elif kind == "article":
        payload = article(**kwargs)
    elif kind == "breadcrumbs":
        payload = breadcrumbs(**kwargs)
    else:
        raise ValueError(f"неизвестный тип разметки: {kind}")
    return _script(payload)


def tldr(points: list[str]) -> str:
    """Блок «Коротко» для GEO: нейропоиск цитирует список выводов охотнее абзаца."""
    rows = "".join(f"<li>{html.escape(p)}</li>" for p in points)
    return ('<aside class="tldr"><b>Коротко</b><ul>' + rows + "</ul></aside>")


def tool_meta() -> list[dict[str, str]]:
    return [{"slug": slug, "url": f"{SITE}/tools/{slug}", "title": title, "hint": hint}
            for slug, title, hint in TOOLS]
