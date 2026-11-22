"""HTTP-контракт v3: карта каналов, семь функций, контуры SaaS.

Разделение намеренное: ``api.py`` — ядро публикаций, ``api_v3.py`` — расширения,
которые не должны усложнять основной цикл. Все пишущие эндпоинты закрыты RBAC.
"""
from __future__ import annotations

import os

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi import Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from fastapi import BackgroundTasks
from fastapi.responses import PlainTextResponse

from . import (abtests, apikeys, article_ab, article_i18n, article_links, articles, assets, auth, automations, bio, billing,
               catalog, edo,
               events_calendar, knowledge, listening, media, media_compliance, media_lab,
               media_raster,
               media_recycle, media_reel, media_report, media_locale, media_voice, pii,
               publictools, quota, rbac,
               ratelimit, security, seo, store, superadmin, tools)
from .workers import sync as sync_workers
from .schemas import ChannelKind

router = APIRouter(prefix="/api/v1")
public = APIRouter()

Read = Depends(rbac.require("publication:read"))
Write = Depends(rbac.require("workspace:manage"))
Gen = Depends(rbac.require("variant:generate"))


# ============================ карта каналов ============================
@router.get("/channels/catalog", tags=["channels"])
def channels_catalog(actor: rbac.Actor = Depends(rbac.require("channel:read"))) -> dict:
    """Все площадки и способ доставки в каждую. 29 каналов, 8 режимов интеграции."""
    return {"summary": catalog.summary(), "items": [catalog.describe(c.kind) for c in catalog.CATALOG]}


@router.get("/channels/kinds/{kind}/spec", tags=["channels"])
def channel_spec(kind: ChannelKind, actor: rbac.Actor = Depends(rbac.require("channel:read"))) -> dict:
    return catalog.describe(kind)


# ============================== инструменты ==============================
class ThreadIn(BaseModel):
    text: str = Field(min_length=40)
    channel: ChannelKind = "x"
    hook: str | None = None
    cta: str | None = None
    numbering: bool = True


@router.post("/tools/thread-split", tags=["tools"])
def thread_split(payload: ThreadIn, actor: rbac.Actor = Gen) -> dict:
    """Smart Thread Splitter: логичная нарезка лонгрида в тред с хуком и CTA."""
    return tools.split_thread(payload.text, payload.channel, hook=payload.hook,
                              cta=payload.cta, with_numbering=payload.numbering)


@router.get("/tools/headlines", tags=["tools"])
def headlines(title: str = Query(min_length=5), actor: rbac.Actor = Gen) -> dict:
    return {"variants": tools.headline_variants(title)}


# ============================ link-in-bio ============================
class BioIn(BaseModel):
    slug: str = Field(pattern=r"^[a-z0-9-]{3,30}$")
    title: str
    bio: str = ""


class BioBlockIn(BaseModel):
    kind: str = "link"
    label: str
    url: str
    badge: str | None = None
    channel: ChannelKind | None = None


@router.get("/bio/pages", tags=["bio"])
def bio_pages(actor: rbac.Actor = Read) -> list[dict]:
    return list(bio.PAGES.values())


@router.get("/bio/pages/{slug}", tags=["bio"])
def bio_page(slug: str, actor: rbac.Actor = Read) -> dict:
    page = bio.get(slug)
    if not page:
        raise HTTPException(404, "страница не найдена")
    return page


@router.post("/bio/pages", status_code=201, tags=["bio"])
def bio_create(payload: BioIn, actor: rbac.Actor = Write) -> dict:
    if bio.get(payload.slug):
        raise HTTPException(409, "slug занят")
    return bio.create(payload.slug, payload.title, payload.bio)


@router.post("/bio/pages/{slug}/blocks", status_code=201, tags=["bio"])
def bio_add_block(slug: str, payload: BioBlockIn, actor: rbac.Actor = Write) -> dict:
    if not bio.get(slug):
        raise HTTPException(404, "страница не найдена")
    return bio.add_block(slug, payload.kind, payload.label, payload.url,
                         badge=payload.badge, channel=payload.channel)


@router.post("/bio/pages/{slug}/sync", tags=["bio"])
def bio_sync(slug: str, posts: list[dict], actor: rbac.Actor = Write) -> dict:
    if not bio.get(slug):
        raise HTTPException(404, "страница не найдена")
    return bio.sync_posts(slug, posts)


@public.post("/webhooks/max", tags=["inbox"])
async def max_webhook(request: Request) -> dict:
    """Вебхук MAX: событие кладётся в очередь воркера Инбокса и разбирается синком.

    Подпись проверяется, если задан ``MAX_WEBHOOK_SECRET`` (заголовок ``X-Max-Signature``,
    HMAC-SHA256 по байтам тела). Без секрета ручка работает только в демо-режиме.
    """
    import hashlib
    import hmac
    import os

    raw = await request.body()
    secret = os.getenv("MAX_WEBHOOK_SECRET", "").strip()
    if secret:
        signature = request.headers.get("x-max-signature", "")
        expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise HTTPException(401, {"type": "signature", "message": "подпись вебхука MAX неверна"})
    elif not auth.demo_mode():
        raise HTTPException(503, {"type": "not_configured",
                                  "message": "MAX_WEBHOOK_SECRET не задан"})
    import json as _json

    try:
        update = _json.loads(raw or b"{}")
    except ValueError:
        raise HTTPException(400, {"type": "validation", "message": "тело вебхука — не JSON"})
    return sync_workers.push_max_update(update)


# ============================ SEO-контур ============================
@public.get("/robots.txt", response_class=PlainTextResponse, include_in_schema=True, tags=["seo"])
def robots(host: str | None = None) -> PlainTextResponse:
    """robots.txt маркетингового контура. Приложение и служебные зоны закрыты."""
    return PlainTextResponse(seo.robots_txt(host or seo.SITE), media_type="text/plain")


@public.get("/app-robots.txt", response_class=PlainTextResponse, tags=["seo"])
def app_robots() -> PlainTextResponse:
    """Контент для `app.posiz.ru/robots.txt`: приложение не индексируется целиком."""
    return PlainTextResponse(seo.app_robots_txt(), media_type="text/plain")


@public.get("/sitemap.xml", response_class=PlainTextResponse, include_in_schema=True, tags=["seo"])
def sitemap() -> PlainTextResponse:
    """sitemap маркетинга: интеграции, связки, решения, утилиты и блог (без /l/ и /@)."""
    return PlainTextResponse(seo.sitemap_xml(), media_type="application/xml")


@public.get("/seo/inventory", tags=["seo"])
def seo_inventory() -> dict:
    return {"inventory": seo.inventory(), "plans": [
        {"code": code, "name": name, "price_rub": price, "features": desc}
        for code, name, price, desc in seo.PLANS],
        "jsonld": {"software_application": seo.software_application(),
                   "faq_example": seo.faq_page([
                       ("Нужен ли erid для поста с ценой?", "Да: цена или скидка делают пост рекламой.")])}}


@public.get("/seo/schema/{kind}", tags=["seo"])
def seo_schema(kind: str, url: str | None = None) -> dict:
    """JSON-LD для страницы: software | faq | howto | article | breadcrumbs (пример)."""
    try:
        if kind == "software":
            return seo.software_application(url or seo.SITE)
        if kind == "faq":
            return seo.faq_page([("Сколько стоит Posiz?", "От 0 ₽ (Free) до 12 900 ₽ (Агентство).")])
        if kind == "howto":
            return seo.how_to("Подключить канал MAX", [
                ("Верифицируйте ИП/ООО", "Через Госуслуги, 1–48 часов"),
                ("Создайте бота", "Через @MasterBot в MAX"),
                ("Вставьте токен", "В Posiz: Каналы → Подключить → MAX"),
            ], url=url or f"{seo.SITE}/integrations/max")
        if kind == "article":
            return seo.article("Маркировка рекламы в MAX", author="Юрист Posiz",
                               section="Законы и маркировка", published="2026-11-17",
                               url=url or f"{seo.SITE}/blog/compliance")
        if kind == "breadcrumbs":
            return seo.breadcrumbs([("Главная", seo.SITE), ("Блог", f"{seo.SITE}/blog")])
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    raise HTTPException(404, f"тип разметки {kind!r} неизвестен")


# ====================== бесплатные SEO-утилиты ======================
class EridCheckIn(BaseModel):
    text: str = Field(min_length=3, max_length=4000)
    advertiser: str = ""
    inn: str | None = None
    erid: str | None = None


class RiskIn(BaseModel):
    channels: list[str] = Field(min_length=1, max_length=12)
    audience: int = Field(default=100_000, ge=100, le=100_000_000)


class UtmIn(BaseModel):
    target_url: str = Field(min_length=4)
    channel: str = "vk"
    campaign: str = "post"
    content: str | None = None
    term: str | None = None
    with_link_in_text: bool = True


class PublicThreadIn(BaseModel):
    text: str = Field(min_length=10, max_length=20000)
    channel: str = "x"
    limit: int | None = None


@public.get("/tools/", tags=["tools"])
def public_tools_index() -> dict:
    """Каталог бесплатных утилит (используется на posiz.ru/tools)."""
    return publictools.index()


@public.post("/tools/erid-checker", tags=["tools"])
def tool_erid(payload: EridCheckIn,
              _rl: None = Depends(ratelimit.rate_limit("public_tool"))) -> dict:
    """Проверка текста на признаки рекламы и готовый блок маркировки (38-ФЗ)."""
    return publictools.erid_check(payload.text, payload.advertiser, payload.inn, payload.erid)


@public.post("/tools/risk-calculator", tags=["tools"])
def tool_risk(payload: RiskIn,
              _rl: None = Depends(ratelimit.rate_limit("public_tool"))) -> dict:
    """Сколько охвата теряется при блокировке/замедлении канала."""
    return publictools.risk_calculator(payload.channels, payload.audience)


@public.post("/tools/utm-builder", tags=["tools"])
def tool_utm(payload: UtmIn,
             _rl: None = Depends(ratelimit.rate_limit("public_tool"))) -> dict:
    """UTM-ссылка и смета доставки по каналам."""
    return publictools.utm_builder(payload.target_url, payload.channel, payload.campaign,
                                   payload.content, payload.term, payload.with_link_in_text)


@public.post("/tools/thread-splitter", tags=["tools"])
def tool_threads(payload: PublicThreadIn,
                 _rl: None = Depends(ratelimit.rate_limit("public_tool"))) -> dict:
    """Нарезка лонгрида в тред под лимиты X / Threads / Bluesky."""
    return publictools.thread_splitter(payload.text, payload.channel, payload.limit)


@public.get("/feed/{workspace}.xml", response_class=PlainTextResponse, tags=["bio"])
def public_feed(workspace: str) -> PlainTextResponse:
    """RSS для Дзена: полный текст в ``<yandex:full-text>``, до 50 постов."""
    from .adapters.special import RssAdapter

    items = store.recent_publications(limit=20)
    xml = RssAdapter("dzen", workspace=workspace).render(items)
    return PlainTextResponse(xml, media_type="application/rss+xml")


@public.get("/@{slug}", response_class=HTMLResponse, tags=["bio"])
def public_bio_short(slug: str):
    """Публичный адрес Link-in-Bio в брендовом виде: ``posiz.ru/@<slug>``."""
    page = bio.get(slug)
    if not page:
        raise HTTPException(404, "страница не найдена")
    return HTMLResponse(bio.render_html(page))


@public.get("/bio/{slug}", response_class=HTMLResponse, tags=["bio"])
def bio_public(slug: str, _rl: None = Depends(ratelimit.rate_limit("bio_page"))):
    """Публичная страница «одна ссылка в шапке профиля». Без авторизации, но с лимитом."""
    page = bio.get(slug)
    if not page:
        raise HTTPException(404, "страница не найдена")
    bio.track(slug, "view")
    return HTMLResponse(bio.render_html(page))


# ===================== комментарий → личка =====================
class RuleIn(BaseModel):
    id: str | None = None
    keyword: str = Field(min_length=2)
    channels: list[ChannelKind] = Field(default_factory=lambda: ["vk"])
    magnet: str = ""
    link_campaign: str = "comment"
    dm_template: str | None = None


class CommentIn(BaseModel):
    author: str = "гость"
    text: str


@router.get("/automations", tags=["automations"])
def automations_list(actor: rbac.Actor = Read) -> dict:
    return {"rules": automations.list_rules(), "events": automations.list_events(20)}


@router.post("/automations", status_code=201, tags=["automations"])
def automations_upsert(payload: RuleIn, actor: rbac.Actor = Write) -> dict:
    return automations.upsert_rule(payload.model_dump())


@public.post("/webhooks/comments/{channel}", tags=["automations"])
async def comment_webhook(channel: ChannelKind, payload: CommentIn, request: Request,
                          x_webhook_signature: str | None = Header(default=None,
                                                                  alias="X-Webhook-Signature"),
                          _rl: None = Depends(ratelimit.rate_limit("comment_webhook"))):
    """Воркер инбокса шлёт сюда комментарии; правило отвечает в личку с UTM-ссылкой.

    Подпись обязательна (``X-Webhook-Signature`` либо платформенный секрет), иначе любой
    внешний POST запускает автоответы от имени клиента и жжёт квоту.
    """
    raw = await request.body()
    security.verify_channel_webhook(channel, raw_body=raw, headers=dict(request.headers))
    sent = automations.handle_comment(channel, payload.author, payload.text)
    return {"channel": channel, "matched": len(sent), "sent": sent}


# ============================ мониторинг ============================
class KeywordIn(BaseModel):
    phrase: str = Field(min_length=3)
    channels: list[ChannelKind] = Field(default_factory=lambda: ["telegram"])


@router.get("/listening/keywords", tags=["listening"])
def keywords(actor: rbac.Actor = Read) -> list[dict]:
    return listening.list_keywords()


@router.post("/listening/keywords", status_code=201, tags=["listening"])
def add_keyword(payload: KeywordIn, actor: rbac.Actor = Write) -> dict:
    return listening.add_keyword(payload.phrase, payload.channels)


@router.post("/listening/scan", tags=["listening"])
def listening_scan(actor: rbac.Actor = Write) -> dict:
    found = listening.scan()
    return {"found": len(found), "mentions": found}


@router.get("/listening/mentions", tags=["listening"])
def mentions(priority: str | None = Query(default=None, pattern="^(high|medium|low)$"),
             actor: rbac.Actor = Read) -> list[dict]:
    return listening.list_mentions(priority)


@router.post("/listening/mentions/{mention_id}/reply", tags=["listening"])
def mention_reply(mention_id: str, actor: rbac.Actor = Read) -> dict:
    return listening.reply_draft(mention_id)


# ==================== календарь инфоповодов ====================
@router.get("/calendar/events", tags=["calendar"])
def calendar_events(start: str | None = None, end: str | None = None,
                    actor: rbac.Actor = Read) -> list[dict]:
    return events_calendar.between(start, end)


@router.get("/calendar/upcoming", tags=["calendar"])
def calendar_upcoming(limit: int = Query(5, ge=1, le=20), actor: rbac.Actor = Read) -> list[dict]:
    return events_calendar.upcoming(limit)


# ============================== медиа-банк ==============================
class AssetIn(BaseModel):
    name: str
    folder: str
    kind: str = "jpg"
    size_mb: float = 0.5
    tags: list[str] = Field(default_factory=list)


@router.get("/assets", tags=["assets"])
def assets_list(folder: str | None = None, tag: str | None = None, actor: rbac.Actor = Read) -> dict:
    return {"folders": assets.folders(), "storage": assets.storage_usage(),
            "items": assets.list_assets(folder, tag)}


@router.post("/assets", status_code=201, tags=["assets"])
def asset_add(payload: AssetIn, actor: rbac.Actor = Write) -> dict:
    return assets.add_asset(payload.name, payload.folder, payload.kind, payload.size_mb,
                            tags=payload.tags)



# ============================== медиа: карточки ==============================
class MediaPostIn(BaseModel):
    """Пост, из которого собирается карточка. Вёрстку задаёт шаблон, не клиент."""

    title: str = Field(min_length=3, max_length=300)
    text: str | None = Field(default=None, max_length=2000)
    eyebrow: str | None = Field(default=None, max_length=40)
    note: str | None = Field(default=None, max_length=200)
    items: list[str] | None = Field(default=None, max_length=media.MAX_ITEMS)
    author: str | None = Field(default=None, max_length=80)
    footer: str | None = Field(default=None, max_length=80)
    url: str | None = Field(default=None, max_length=120)


class MediaBrandIn(BaseModel):
    """Brand kit: цвета, тема, гарнитура и масштаб типографики — данные клиента, не код."""

    name: str | None = Field(default=None, max_length=60)
    accent: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    bg: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    text: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    muted: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    #: dark | light — светлая тема нужна магазинам и клиникам чаще «тёмного космоса»
    theme: str | None = Field(default=None, max_length=12)
    #: sans | serif | mono — системные стеки, без загрузки веб-шрифтов
    font: str | None = Field(default=None, max_length=12)
    #: множитель типографики 0,8…1,3: «крупнее» — поле спеки, а не новый шаблон
    scale: float | None = Field(default=None, ge=media.MIN_BRAND_SCALE,
                                le=media.MAX_BRAND_SCALE)


class MediaComplianceIn(BaseModel):
    """Маркировка: пакет правил рынка + поля, которые этот пакет требует.

    ``pack`` — данные, а не режим рендерера: ``ru-ad`` (erid + рекламодатель + ИНН),
    ``ru-info`` (реклама запрещена), ``eu-disclosure``, ``us-ftc``, ``generic``.
    Неизвестный пакет — это ошибка, а не тихий откат на умолчание.
    """

    pack: str | None = Field(default=None, max_length=40)
    ad: bool = False
    erid: str | None = Field(default=None, max_length=64)
    advertiser: str | None = Field(default=None, max_length=120)
    inn: str | None = Field(default=None, pattern=r"^\d{10}$|^\d{12}$")
    min_age: int | None = None
    #: дисклеймер сцены/кадра («не является консультацией») — тоже плашка, тоже из пакета
    disclaimer: str | None = Field(default=None,
                                   max_length=media_compliance.MAX_DISCLAIMER_CHARS)
    #: язык пометки: у пакета может быть несколько формулировок (Реклама / Ad / Anzeige)
    lang: str | None = Field(default=None, max_length=8)


class RecycleIn(BaseModel):
    """Топ-пост → ролик. Без ``publication_id`` берётся лучший по данным."""

    publication_id: str | None = Field(default=None, max_length=64)
    channel: str | None = Field(default=None, max_length=40)
    days: int = Field(default=media_recycle.DEFAULT_DAYS, ge=1, le=3650)
    #: порог показов: ниже него вовлечённость — шум, а не сигнал
    min_impressions: int | None = Field(default=None, ge=1)
    format: str = Field(default="9x16", max_length=8)
    fps: int = 30
    locale: str | None = Field(default=None, max_length=8)
    brand: MediaBrandIn | None = None
    render: bool = True


class ReportIn(BaseModel):
    """Видео-отчёт агентства: цифры периода из аналитики, а не из головы."""

    days: int = Field(default=media_report.DEFAULT_PERIOD_DAYS, ge=1, le=3650)
    channel: str | None = Field(default=None, max_length=40)
    locale: str | None = Field(default=None, max_length=8)
    format: str = Field(default="9x16", max_length=8)
    fps: int = 30
    brand: MediaBrandIn | None = None
    render: bool = True


class HookLabIn(BaseModel):
    """Лаборатория хука: один пост → N вариантов первых двух секунд."""

    post: MediaPostIn
    brand: MediaBrandIn | None = None
    compliance: MediaComplianceIn | None = None
    fps: int = 30
    format: str = Field(default="9x16", max_length=8)
    #: вариантов: 2–5; больше — уже не хватает трафика на сравнение
    count: int = Field(default=media_lab.DEFAULT_VARIANTS, ge=media_lab.MIN_VARIANTS,
                       le=media_lab.MAX_VARIANTS)


class HookMetricIn(BaseModel):
    """Показатели варианта — приращениями: статистика приходит несколько раз за жизнь поста."""

    variant_id: str = Field(max_length=32)
    views: int = Field(ge=1)
    completions: int | None = Field(default=None, ge=0)
    avg_watch_seconds: float | None = Field(default=None, ge=0)
    channel: str | None = Field(default=None, max_length=40)


class HookDecideIn(BaseModel):
    """Порог показов на вариант: ниже него разница в досмотре — шум."""

    min_views: int | None = Field(default=None, ge=1)


class CardsIn(BaseModel):
    post: MediaPostIn
    formats: list[str] = Field(default_factory=lambda: ["1x1", "9x16"], max_length=3)
    template: str = "card-hero"
    brand: MediaBrandIn | None = None
    compliance: MediaComplianceIn | None = None


class SpecIn(BaseModel):
    spec: dict


class VoiceIn(BaseModel):
    """Озвучка ролика: провайдер, голос и **подача**.

    `performance` — это не «промпт», а профиль данных: паузы, темп и (у провайдера) настройки
    выразительности. `cinematic-baritone` — глубокая подача с драматическими паузами:
    стабильность 40 %, выразительность 20 %, темп 0,95 и паузы ×1,45.
    """

    provider: str | None = None
    voice: str | None = None
    rate: float = Field(default=1.0, ge=0.5, le=2.0)
    #: текст диктора; без него собирается из сцен (хук + титры + финал)
    text: str | None = Field(default=None, max_length=3000)
    #: для провайдера ``file`` — путь к готовому аудио (демо, ручная озвучка диктором)
    path: str | None = None
    #: нейтральный / cinematic-baritone / documentary / energetic
    performance: str | None = Field(default=None, max_length=32)
    #: настройки ElevenLabs (0…1); вне диапазона — ошибка, а не «подрежем молча»
    stability: float | None = Field(default=None, ge=0.0, le=1.0)
    style: float | None = Field(default=None, ge=0.0, le=1.0)
    #: модель синтеза: по умолчанию eleven_multilingual_v2 (Turbo/Flash теряют подачу)
    model: str | None = Field(default=None, max_length=40)
    #: метки ударения: {"Дагестана": "Дагест+ана"} — в кадр не попадают
    stress: dict[str, str] | None = None
    pitch: float | None = Field(default=None, ge=0.5, le=2.0)


class ReelIn(BaseModel):
    """Ролик: либо готовый пост (собрать сцены самим), либо спека целиком (перерендер)."""

    post: MediaPostIn | None = None
    spec: dict | None = None
    fps: int = 30
    #: формат ролика: 9x16 (Shorts/клипы), 4x5 (лента), 1x1 (посты), 16x9 (YouTube)
    format: str = Field(default="9x16", max_length=8)
    brand: MediaBrandIn | None = None
    compliance: MediaComplianceIn | None = None
    voice: VoiceIn | None = None
    #: язык ролика: от него зависят темп чтения титров и предельная длина слова
    locale: str | None = Field(default=None, max_length=8)
    #: подача диктора: паузы и темп влияют на длительность сцен, а не только на звук
    performance: str | None = Field(default=None, max_length=32)
    #: ``False`` — только смета и проверки, без рендера кадров (быстро и бесплатно)
    render: bool = True


@router.get("/media/templates", tags=["media"])
def media_templates(actor: rbac.Actor = Read) -> dict:
    """Библиотека шаблонов и brand kit: модель выбирает из списка, а не придумывает вёрстку."""
    return {"items": media.templates(), "limits": media.limits(),
            "reels": {"scenes": sorted(media_reel.SCENES),
                      "formats": [fmt for fmt in media_reel.REEL_FORMATS if fmt in media.FORMATS],
                      "fps": list(media_reel.FPS_CHOICES),
                      "limits": media_reel.limits(),
                      "locales": media_locale.catalog(),
                      "performances": media_voice.performance_catalog(),
                      "default_performance": media_voice.DEFAULT_PERFORMANCE},
            "brand": {"themes": sorted(media.THEMES), "fonts": dict(media.FONTS),
                      "scale": [media.MIN_BRAND_SCALE, media.MAX_BRAND_SCALE]}}


@router.get("/media/voice", tags=["media"])
def media_voice_catalog(actor: rbac.Actor = Read) -> dict:
    """Витрина озвучки: провайдеры, цены, подачи, модели и — отдельно — self-hosted движки.

    Self-hosted (`Fish Speech`, `F5-TTS`, `Piper` через свой сервер) в основной список не
    попадает: клиенту сервиса он не предлагается, а тому, кто разворачивает модель у себя,
    нужен именно отдельный раздел, а не «бесплатная озвучка» в общем прайсе.
    """
    state = media_voice.available()
    return {**state, "settings_ranges": media_voice.SETTINGS_RANGES,
            "performances": media_voice.performance_catalog(),
            "default_performance": media_voice.DEFAULT_PERFORMANCE,
            "stress_supported": {code: bool(row.get("stress"))
                                 for code, row in media_voice.PROVIDERS.items()},
            "expressiveness": {code: row.get("expressiveness", 1)
                               for code, row in media_voice.PROVIDERS.items()}}


@router.get("/media/summary", tags=["media"])
def media_summary(actor: rbac.Actor = Read) -> dict:
    return {**media.summary(), "reels": media_reel.summary(),
            "hooks": media_lab.summary(),
            "recycle": media_recycle.summary(workspace_id=actor.workspace_id),
            "reports": media_report.summary(workspace_id=actor.workspace_id),
            "voice": media_voice.available(),
            "compliance": {"default_pack": media_compliance.DEFAULT_PACK,
                           "packs": [row["code"] for row in media_compliance.packs()]}}


@router.get("/media/compliance/packs", tags=["media"])
def media_compliance_packs(actor: rbac.Actor = Read) -> dict:
    """Пакеты правил маркировки: что требуется на каждом рынке и чем это рисуется.

    Пакет — данные: новый рынок добавляется через ``MEDIA_COMPLIANCE_PACKS`` (JSON),
    без релиза рендерера.
    """
    return {"default": media_compliance.DEFAULT_PACK,
            "items": media_compliance.packs(),
            "limits": {"max_disclaimer_chars": media_compliance.MAX_DISCLAIMER_CHARS}}


@router.post("/media/specs/validate", tags=["media"])
def media_validate(payload: SpecIn, actor: rbac.Actor = Gen) -> dict:
    """Сухая проверка спеки: без записи и без рендера. Так отлаживают шаблоны."""
    return media.check(payload.spec)


@router.post("/media/cards", status_code=201, tags=["media"])
def media_cards(payload: CardsIn, actor: rbac.Actor = Gen) -> dict:
    """Карточки и обложки из поста: 1:1, 9:16, 16:9 — с проверками до рендера.

    Ответ 201 даже при отказе отдельного формата: причина в ``checks.issues``
    конкретного элемента. Полный отказ (422) — когда не собрался ни один формат.
    """
    brand = payload.brand.model_dump(exclude_none=True) if payload.brand else None
    compliance = payload.compliance.model_dump(exclude_none=True) if payload.compliance else None
    result = media.cards_from_post(payload.post.model_dump(exclude_none=True),
                                   payload.formats, template=payload.template,
                                   brand=brand, compliance=compliance)
    if not result["ok"]:
        raise HTTPException(status_code=422, detail={"message": "ни один формат не собрался",
                                                     "items": result["items"]})
    return result


@router.get("/media/jobs", tags=["media"])
def media_jobs(limit: int = Query(default=50, ge=1, le=200),
               actor: rbac.Actor = Read) -> dict:
    return {"items": media.jobs(limit), "summary": media.summary()}


@router.get("/media/jobs/{job_id}", tags=["media"])
def media_job(job_id: str, actor: rbac.Actor = Read) -> dict:
    item = media.job(job_id)
    if not item:
        raise HTTPException(status_code=404, detail="задача не найдена")
    found = media.spec(item["spec_id"])
    return {"job": item, "spec": (found or {}).get("body")}


@router.post("/media/jobs/{job_id}/cancel", tags=["media"])
def media_job_cancel(job_id: str, actor: rbac.Actor = Write) -> dict:
    item = media.cancel(job_id)
    if not item:
        raise HTTPException(status_code=404, detail="задача не найдена")
    return item


@router.get("/media/assets/{asset_id}", tags=["media"])
def media_asset(asset_id: str, fmt: str | None = Query(default=None, alias="format"),
                actor: rbac.Actor = Read) -> Response:
    """Артефакт карточки: SVG или PNG.

    Формат задаётся `?format=png` или расширением в пути (`/media/assets/x.png`) — второе
    нужно, потому что Telegram и VK скачивают картинку по ссылке и смотрят на расширение.
    PNG отдаётся, только если растеризатор доступен; иначе 409 с подсказкой, а не «тихо SVG
    под видом PNG».
    """
    suffix = ""
    if "." in asset_id:
        asset_id, _, suffix = asset_id.rpartition(".")
    item = media.asset(asset_id)
    if not item:
        raise HTTPException(status_code=404, detail="артефакт не найден")
    wanted = (fmt or suffix or "svg").lower()
    if wanted not in {"svg", "png"}:
        raise HTTPException(status_code=400, detail="формат — svg или png")
    if wanted == "png" and not item.get("png_key"):
        state = media_raster.available()
        raise HTTPException(status_code=409, detail={
            "issue": "raster_unavailable",
            "message": "растеризатор недоступен: доступен только SVG",
            "raster": state})
    content = media.asset_content(asset_id, wanted)
    if not content:
        raise HTTPException(status_code=404, detail="артефакт не найден в хранилище")
    data, mime = content
    return Response(content=data, media_type=mime, headers={
        "Cache-Control": "private, max-age=3600",
        "Content-Disposition": f'inline; filename="{asset_id}.{wanted}"'})


@router.get("/media/public/{token}", tags=["media"])
def media_public(token: str) -> Response:
    """Публичная ссылка на карточку — **без авторизации**, но по подписанному токену.

    Зачем без авторизации: ссылку открывает не наш клиент, а сервер канала (Telegram
    тянет картинку сам) или человек в превью поста. Токен = id + формат + срок + HMAC,
    живёт час и не даёт доступа ни к чему, кроме одного артефакта.
    """
    resolved = media.resolve_token(token)
    if not resolved:
        raise HTTPException(status_code=403, detail="ссылка недействительна или истекла")
    asset_id, fmt = resolved
    content = media.asset_content(asset_id, fmt)
    if not content:
        raise HTTPException(status_code=404, detail="артефакт не найден")
    data, mime = content
    return Response(content=data, media_type=mime,
                    headers={"Cache-Control": "public, max-age=3600"})


@router.post("/media/purge", tags=["media"])
def media_purge(ttl_days: int | None = Query(default=None, ge=1, le=365),
                actor: rbac.Actor = Write) -> dict:
    """Ручной запуск уборки (воркер делает то же по cron): удаляет артефакты старше TTL."""
    days = ttl_days or int(os.getenv("MEDIA_TTL_DAYS", "14"))
    return media.purge(days)

@router.get("/media/recycle/candidates", tags=["media"])
def media_recycle_candidates(days: int = Query(default=media_recycle.DEFAULT_DAYS, ge=1, le=3650),
                             channel: str | None = Query(default=None, max_length=40),
                             limit: int = Query(default=media_recycle.DEFAULT_LIMIT, ge=1, le=50),
                             min_impressions: int | None = Query(default=None, ge=1),
                             actor: rbac.Actor = Read) -> dict:
    """Кандидаты на recycle: топ-посты по вовлечённости и все отказы с причинами."""
    return media_recycle.candidates(days=days, channel=channel, limit=limit,
                                    min_impressions=min_impressions,
                                    workspace_id=actor.workspace_id)


@router.post("/media/recycle", status_code=201, tags=["media"])
def media_recycle_create(payload: RecycleIn, actor: rbac.Actor = Gen) -> dict:
    """Топ-пост → ролик в один вызов: пост берётся из аналитики, а не из списка «последние».

    Отказ тоже ответ: ``409`` с кодом ``no_candidates``/``below_threshold`` значит, что данных
    для выбора не хватает — и это видно клиенту, а не превращается в «странный» ролик.
    """
    result = media_recycle.recycle(
        publication_id=payload.publication_id, channel=payload.channel, days=payload.days,
        fmt=payload.format, fps=payload.fps, locale=payload.locale,
        brand=payload.brand.model_dump(exclude_none=True) if payload.brand else None,
        min_impressions=payload.min_impressions, render=payload.render,
        workspace_id=actor.workspace_id)
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail={
            "kind": result.get("code"), "message": result.get("reason"),
            "skipped": result.get("skipped", [])[:5], "counts": result.get("counts")})
    submission = result.get("submission") or {}
    job = (submission.get("reel") or {})
    if job.get("status") == "rejected":
        raise HTTPException(status_code=422, detail={
            "kind": "reel_rejected", "issues": submission["checks"]["issues"]})
    return {**result, "reel_id": job.get("id"), "status": job.get("status"),
            "summary": media_recycle.summary(workspace_id=actor.workspace_id)}


@router.post("/media/reports", status_code=201, tags=["media"])
def media_report_create(payload: ReportIn, actor: rbac.Actor = Gen) -> dict:
    """Видео-отчёт за период: те же цифры, что в кабинете, плюс ссылка на источник каждой."""
    result = media_report.build(
        days=payload.days, channel=payload.channel, locale=payload.locale,
        fmt=payload.format, fps=payload.fps,
        brand=payload.brand.model_dump(exclude_none=True) if payload.brand else None,
        render=payload.render, workspace_id=actor.workspace_id)
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail={
            "kind": result.get("code"), "message": result.get("reason"),
            "numbers": result.get("numbers")})
    submission = result.get("submission") or {}
    job = (submission.get("reel") or {})
    if job.get("status") == "rejected":
        raise HTTPException(status_code=422, detail={
            "kind": "reel_rejected", "issues": submission["checks"]["issues"]})
    return {**result, "reel_id": job.get("id"), "status": job.get("status")}


@router.get("/media/reels", tags=["media"])
def media_reels(limit: int = Query(default=50, ge=1, le=200),
                actor: rbac.Actor = Read) -> dict:
    """Задачи роликов и сводка: сколько готово, сколько осталось сторибордами."""
    return {"items": media_reel.reels(limit), "summary": media_reel.summary()}


@router.post("/media/reels", status_code=201, tags=["media"])
def media_reel_create(payload: ReelIn, actor: rbac.Actor = Gen) -> dict:
    """Пост → ролик 9:16: сцены, кадры, MP4.

    Рендер синхронный, потому что ролик в 15 секунд — это ~40 секунд CPU на узле с
    растеризатором. Когда сцены начнёт писать модель (этап D), этот же вызов уедет в
    очередь: контракт ответа уже содержит задачу, а не «готовый файл».
    """
    # подачу можно задать и на уровне ролика, и внутри voice: решает одна точка, иначе
    # спека и синтез разойдутся (ролик с паузами, а голос без них)
    performance = payload.performance or (payload.voice.performance if payload.voice else None)
    if payload.spec is not None:
        spec = dict(payload.spec)
    elif payload.post is not None:
        spec = media_reel.build_reel_spec(
            payload.post.model_dump(exclude_none=True), fps=payload.fps, fmt=payload.format,
            locale=payload.locale, performance=performance,
            brand=payload.brand.model_dump(exclude_none=True) if payload.brand else None,
            compliance=(payload.compliance.model_dump(exclude_none=True)
                        if payload.compliance else None))
    else:
        raise HTTPException(status_code=422, detail={
            "kind": "reel_source_required",
            "message": "нужен post (собрать сцены) или spec (перерендер)",
        })

    voice_payload = payload.voice.model_dump(exclude_none=True) if payload.voice else None
    result = media_reel.submit(spec, render=payload.render,
                               workspace_id=actor.workspace_id, voice=voice_payload)
    job = result["reel"]
    if job.get("status") == "rejected":
        raise HTTPException(status_code=422, detail={
            "kind": "reel_rejected", "issues": result["checks"]["issues"],
            "hint": "исправьте сцены и пришлите спеку снова",
        })
    return {**result, "reel_id": job["id"], "voice_state": media_voice.available(),
            "summary": media_reel.summary()}


@router.get("/media/reels/{reel_id}", tags=["media"], response_model=None)
def media_reel_get(reel_id: str, actor: rbac.Actor = Read):
    """Задача ролика, а с расширением `.mp4`/`.png`/`.mp3`/`.srt`/`.txt` — сам файл.

    Один маршрут, а не три: Starlette сравнивает пути по порядку регистрации, и
    `/media/reels/{id}` перехватывает `/media/reels/{id}.mp4` (расширение попадает в id).
    Разбор расширения внутри — единственный способ не зависеть от порядка.
    """
    suffix = ""
    if "." in reel_id:
        reel_id, _, suffix = reel_id.rpartition(".")
    if suffix in {"mp4", "png", "mp3", "srt", "txt"}:
        if media_reel.reel(reel_id) is None:
            raise HTTPException(status_code=404, detail="ролик не найден")
        content = media_reel.content(reel_id, suffix)
        if not content:
            kinds = {"mp4": "video_unavailable", "png": "poster_unavailable",
                     "mp3": "audio_unavailable", "srt": "subtitles_unavailable",
                     "txt": "compliance_text_unavailable"}
            raise HTTPException(status_code=409, detail={
                "kind": kinds[suffix],
                "reason": (media_reel.reel(reel_id) or {}).get("video_error")
                or "файл не собран",
                "defects": (media_reel.reel(reel_id) or {}).get("video_defects") or [],
                "hint": "проверьте MEDIA_FFMPEG_BIN и растеризатор: GET /media/summary",
            })
        data, mime = content
        return Response(content=data, media_type=mime, headers={
            "Cache-Control": "private, max-age=3600",
            "Content-Disposition": f'inline; filename="{reel_id}.{suffix}"'})

    item = media_reel.reel(reel_id)
    if not item:
        raise HTTPException(status_code=404, detail="ролик не найден")
    spec = media_reel.spec_of(reel_id)
    return {"reel": item, "spec": spec,
            "timeline": media_reel.timeline(spec) if spec else [],
            "storyboard": [{"t": shot["t"]} for shot in media_reel.storyboard(spec)]
            if spec else []}


@router.get("/media/reels/{reel_id}/frame", tags=["media"])
def media_reel_frame(reel_id: str, t: float = Query(default=0.0, ge=0),
                     actor: rbac.Actor = Read) -> Response:
    """Кадр на любой секунде: 900 PNG не храним — кадр считается из спеки.

    Стоит миллисекунды, поэтому им пользуется и превью в интерфейсе, и проверка
    «а что на 12-й секунде» без пересборки ролика.
    """
    svg = media_reel.frame_svg(reel_id, t)
    if not svg:
        raise HTTPException(status_code=404, detail="ролик или спека не найдены")
    return Response(content=svg, media_type="image/svg+xml",
                    headers={"Cache-Control": "private, max-age=60"})


# ============================== Хук-лаборатория (этап F) ==============================
@router.post("/media/hooks", status_code=201, tags=["media"])
def media_hook_create(payload: HookLabIn, actor: rbac.Actor = Gen) -> dict:
    """Пост → лаборатория хука: 2–5 вариантов первых двух секунд.

    Меняется только первый кадр: остальные сцены общие, иначе метрика досмотра покажет
    разницу роликов, а не силу хука. ``built`` может быть меньше ``requested`` — в ответе
    видно, какие стратегии не дали материала и почему.
    """
    lab = media_lab.build_lab(
        payload.post.model_dump(exclude_none=True),
        brand=payload.brand.model_dump(exclude_none=True) if payload.brand else None,
        compliance=(payload.compliance.model_dump(exclude_none=True)
                    if payload.compliance else None),
        fmt=payload.format, fps=payload.fps, count=payload.count,
        workspace_id=actor.workspace_id)
    return {**lab, "lab_id": lab["id"], "table": media_lab.table(lab["id"])}


@router.get("/media/hooks", tags=["media"])
def media_hook_list(limit: int = Query(default=50, ge=1, le=200),
                    actor: rbac.Actor = Read) -> dict:
    return {"items": media_lab.labs(limit), "summary": media_lab.summary()}


@router.get("/media/hooks/{lab_id}", tags=["media"])
def media_hook_get(lab_id: str, min_views: int | None = Query(default=None, ge=1),
                   actor: rbac.Actor = Read) -> dict:
    """Лаборатория и таблица вариантов: метрика по каждому и решение (или причина его отсутствия)."""
    row = media_lab.lab(lab_id)
    if not row:
        raise HTTPException(status_code=404, detail="лаборатория не найдена")
    return {**row, "table": media_lab.table(lab_id, min_views=min_views)}


@router.post("/media/hooks/{lab_id}/metrics", tags=["media"])
def media_hook_metrics(lab_id: str, payload: HookMetricIn, actor: rbac.Actor = Gen) -> dict:
    """Записать показатели варианта (приращениями).

    В проде это делает синхронизация аналитики каналов; здесь — тот же контракт, чтобы
    лабораторию можно было проверить и в песочнице.
    """
    result = media_lab.record_metrics(lab_id, payload.variant_id, views=payload.views,
                                      completions=payload.completions,
                                      avg_watch_seconds=payload.avg_watch_seconds,
                                      channel=payload.channel)
    if not result["ok"]:
        status = 404 if result["code"] == "lab_not_found" else 409
        raise HTTPException(status_code=status, detail={
            "kind": result["code"], "reason": result["reason"]})
    return {**result, "table": media_lab.table(lab_id)}


@router.post("/media/hooks/{lab_id}/decide", tags=["media"], response_model=None)
def media_hook_decide(lab_id: str, payload: HookDecideIn | None = None,
                      actor: rbac.Actor = Gen):
    """Зафиксировать победителя. Без данных — ``409``, а не «первый по списку»."""
    result = media_lab.decide(lab_id, min_views=payload.min_views if payload else None)
    if result.get("ok"):
        return result
    if result.get("code") == "lab_not_found":
        raise HTTPException(status_code=404, detail="лаборатория не найдена")
    raise HTTPException(status_code=409, detail={
        "kind": "hook_not_decided", "code": result.get("code"),
        "reason": result.get("reason"),
        "hint": "нужны показы минимум по двум вариантам: POST /media/hooks/{id}/metrics",
        "table": result.get("table"),
    })


@router.post("/media/hooks/{lab_id}/reel", tags=["media"], response_model=None)
def media_hook_reel(lab_id: str, render: bool = Query(default=True),
                    actor: rbac.Actor = Gen):
    """Ролик победителя: спека из лаборатории уходит в тот же конвейер, что обычный ролик."""
    spec = media_lab.winner_spec(lab_id)
    if not spec:
        raise HTTPException(status_code=409, detail={
            "kind": "hook_not_decided",
            "reason": "победитель не выбран: решения нет, собирать нечего",
        })
    result = media_reel.submit(spec, render=render, workspace_id=actor.workspace_id)
    return {**result, "reel_id": result["reel"]["id"], "lab_id": lab_id}


# ============================== A/B-тесты ==============================
class AbIn(BaseModel):
    publication_id: str
    headline_a: str
    headline_b: str
    channel: ChannelKind = "telegram"
    check_after_minutes: int = 120


# ============================ статьи ============================
class ArticleIn(BaseModel):
    """Бриф статьи: тема, ключевые слова, разделы с текстом и данные под визуалы."""

    #: тема и SEO-заголовок: заголовок можно задать отдельно, если он длиннее темы
    topic: str = Field(min_length=3, max_length=200)
    title: str | None = Field(default=None, max_length=200)
    seo_title: str | None = Field(default=None, max_length=90)
    keywords: list[str] = Field(default_factory=list, max_length=20)
    #: разделы: ``heading`` + ``text`` (его пишет модель) + данные (``rows``, ``steps``,
    #: ``material``, ``image_prompt``) — по ним и выбирается визуал
    outline: list[dict] = Field(default_factory=list, max_length=20)
    sections: int = Field(default=5, ge=3, le=9)
    locale: str | None = Field(default=None, max_length=8)
    #: тип schema.org для разметки: Article по умолчанию, HowTo/FAQPage — когда уместно
    article_schema: str | None = Field(default=None, alias="schema", max_length=32)
    lead: str | None = Field(default=None, max_length=600)
    summary: list[str] = Field(default_factory=list, max_length=10)
    faq: list[dict] = Field(default_factory=list, max_length=20)
    #: материалы клиента: графики и цифры строятся только по ним, а не по памяти модели
    materials: list[dict] = Field(default_factory=list, max_length=50)
    brand: dict | None = None
    #: готовый план (после правки человеком) — тогда планировщик не пересобирает структуру
    plan: dict | None = None
    #: пакет правил: политика темы (объём, обязательные блоки, запрещённые обещания, оговорка)
    rule_pack: str | None = Field(default=None, max_length=32)
    #: шаблон публикации: токены вёрстки, а не отдельный рендерер
    template: str | None = Field(default=None, max_length=32)
    #: откуда брать иллюстрации: brand-card (своя графика), ai-generate, stock
    image_provider: str | None = Field(default=None, max_length=32)
    #: ``True`` — статья с ошибками проверок не сохраняется (422 с перечнем замечаний)
    strict: bool = True


class ArticlePlanIn(BaseModel):
    topic: str = Field(min_length=3, max_length=200)
    rule_pack: str | None = Field(default=None, max_length=32)
    template: str | None = Field(default=None, max_length=32)
    image_provider: str | None = Field(default=None, max_length=32)
    title: str | None = None
    keywords: list[str] = Field(default_factory=list, max_length=20)
    outline: list[dict] = Field(default_factory=list, max_length=20)
    sections: int = Field(default=5, ge=3, le=9)
    locale: str | None = Field(default=None, max_length=8)
    materials: list[dict] = Field(default_factory=list, max_length=50)


class ArticleLinksIn(BaseModel):
    """Сколько ссылок ставить и с какой близости: порог — параметр, а не «на глаз»."""

    limit: int | None = Field(default=None, ge=1, le=10)
    min_score: float | None = Field(default=None, ge=0.0, le=1.0)


class ArticleRefreshIn(BaseModel):
    """Материалы для обновления вечнозелёной статьи и момент, на который считаем давность."""

    materials: list[dict] = Field(default_factory=list, max_length=50)
    now: str | None = Field(default=None, max_length=40)
    reason: str | None = Field(default=None, max_length=300)


@router.get("/articles/summary", tags=["articles"])
def articles_summary(actor: rbac.Actor = Read) -> dict:
    """Что умеет движок статей: блоки, правила выбора визуала, провайдеры иллюстраций."""
    return {**articles.summary(), "spec_version": articles.SPEC_VERSION,
            "limits": {**articles.summary()["limits"],
                       "sections": [3, 9], "shingle": articles.SHINGLE},
            "statuses": ["draft", "ready"]}


@router.get("/articles/packs", tags=["articles"])
def articles_packs(actor: rbac.Actor = Read) -> dict:
    """Пакеты правил и шаблоны публикации: чем тема отличается, кроме текста.

    Пакет — данные: «темы с высокой ценой ошибки» добавляют оговорку, требуют источники у
    сравнений и запрещают обещания результата; шаблон меняет токены вёрстки. Новый рынок или
    отрасль подключается файлом (`ARTICLE_RULE_PACKS`, `ARTICLE_TEMPLATES`), без релиза.
    """
    return {"rule_packs": articles.rule_packs(), "templates": articles.templates(),
            "default_pack": articles.DEFAULT_PACK,
            "default_template": articles.DEFAULT_TEMPLATE,
            "policy_keys": ["min_words", "max_words", "max_duplicate_ratio",
                            "required_blocks", "require_sources", "banned_phrases",
                            "disclaimer"],
            "env": {"rule_packs": "ARTICLE_RULE_PACKS", "templates": "ARTICLE_TEMPLATES"}}


@router.get("/articles/images", tags=["articles"])
def articles_images(actor: rbac.Actor = Read) -> dict:
    """Провайдеры иллюстраций и кэш: что доступно, чего не хватает, какая лицензия у файлов.

    Доступность считается из окружения, а не объявлена в коде: без ключа провайдер честно
    недоступен, статья при этом соберётся со своей графикой и назовёт причину отката.
    """
    return {"providers": articles.image_catalog(),
            "cache": articles.image_cache_stats(),
            "recent": articles.image_cache()[:10],
            "env": {"keys": ["MEDIA_IMAGE_PROVIDER_KEY", "MEDIA_STOCK_KEY"],
                    "urls": ["MEDIA_IMAGE_PROVIDER_URL", "MEDIA_STOCK_URL"]},
            "default": "brand-card"}


class ArticleTranslationIn(BaseModel):
    """Перевод статьи: `translations` — то, что дал переводчик, а не то, что мы придумали."""

    locale: str = Field(min_length=2, max_length=8)
    #: ``False`` — сохранить частичный перевод как черновик, ``True`` — отказать (422)
    strict: bool = True
    translations: dict = Field(default_factory=dict)
    #: адреса переводов для `hreflang`: язык → URL
    urls: dict[str, str] = Field(default_factory=dict)


class ArticleAbIn(BaseModel):
    """Сколько вариантов заголовка сравнивать; конкретные формулы можно задать явно."""

    count: int = Field(default=3, ge=2, le=4)
    kinds: list[str] = Field(default_factory=list, max_length=4)
    #: второй тест на той же статье: ретраем он не считается
    salt: str | None = Field(default=None, max_length=40)


class ArticleAbMetricsIn(BaseModel):
    """Накопленные показы и клики варианта: сумма, а не последнее значение."""

    code: str = Field(min_length=1, max_length=16)
    impressions: int = Field(ge=0, le=10_000_000)
    clicks: int = Field(ge=0, le=10_000_000)


@router.get("/articles/locales", tags=["articles"])
def articles_locales(actor: rbac.Actor = Read) -> dict:
    """Языки статьи: темп чтения, лимиты заголовка и подписи интерфейса.

    Коды берутся из реестра `media_locale`, но числа — свои: темп чтения с экрана (титры ролика)
    и темп чтения статьи различаются, и «5 минут» по темпу титров превращается в 8.
    """
    return {"locales": article_i18n.catalog(),
            "default": media_locale.DEFAULT_LOCALE,
            "ui_keys": sorted(article_i18n.UI_STRINGS["ru"]),
            "note": "темп чтения статьи — слова в минуту; темп ролика — слова в секунду",
            "env": {"translator": "ARTICLE_TRANSLATOR"}}


@router.get("/articles/abtests", tags=["articles"])
def articles_abtests_list(limit: int = Query(default=50, ge=1, le=200),
                          actor: rbac.Actor = Read) -> dict:
    """Тесты заголовков: у каждого — отпечаток тела и состояние, а не только название."""
    rows = article_ab.tests(limit)
    return {"items": [article_ab.report(row["id"]) for row in rows],
            "kinds": [{"code": row["code"], "hypothesis": row["hypothesis"]}
                      for row in article_ab.HEADLINE_KINDS],
            "lead_kinds": [row["code"] for row in article_ab.LEAD_KINDS],
            "limits": {"min_impressions": article_ab._env_int(
                           "ARTICLE_AB_MIN_IMPRESSIONS", article_ab.DEFAULT_MIN_IMPRESSIONS),
                       "alpha": article_ab._env_float("ARTICLE_AB_ALPHA",
                                                      article_ab.DEFAULT_ALPHA),
                       "max_title": article_ab.MAX_TITLE},
            "env": {"min_impressions": "ARTICLE_AB_MIN_IMPRESSIONS",
                    "alpha": "ARTICLE_AB_ALPHA"}}


@router.post("/articles/abtests/{test_id}/assign", tags=["articles"])
def articles_abtest_assign(test_id: str, visitor: str = Query(min_length=1, max_length=120),
                           actor: rbac.Actor = Read) -> dict:
    """Какой заголовок видит этот читатель. Один и тот же посетитель — один и тот же вариант."""
    result = article_ab.assign(test_id, visitor)
    if not result.get("ok"):
        code = result.get("code")
        status = 404 if code == "test_not_found" else 422
        raise HTTPException(status_code=status, detail={"kind": code,
                                                        "message": result.get("reason")})
    return result


@router.post("/articles/abtests/{test_id}/metrics", tags=["articles"])
def articles_abtest_metrics(test_id: str, payload: ArticleAbMetricsIn,
                            actor: rbac.Actor = Gen) -> dict:
    """Записать показы и клики варианта: суммы, а не перезапись — метрики приходят порциями."""
    result = article_ab.record(test_id, payload.code, impressions=payload.impressions,
                               clicks=payload.clicks)
    if not result.get("ok"):
        code = result.get("code")
        status = 404 if code == "test_not_found" else 409
        raise HTTPException(status_code=status, detail={"kind": code,
                                                        "message": result.get("reason"),
                                                        "known": result.get("known")})
    return result


@router.post("/articles/abtests/{test_id}/evaluate", tags=["articles"])
def articles_abtest_evaluate(test_id: str, actor: rbac.Actor = Read) -> dict:
    """Решение по данным: мало показов, шум или победитель — с числом, а не «кажется, лучше»."""
    result = article_ab.evaluate(test_id)
    if not result.get("ok"):
        if result.get("code") == "test_not_found":
            raise HTTPException(status_code=404, detail={"kind": "test_not_found"})
        raise HTTPException(status_code=409, detail={
            "kind": result.get("code"), "message": result.get("reason"),
            "needed": result.get("needed"), "ctr": result.get("ctr"),
            "p_value": result.get("p_value"), "uplift": result.get("uplift")})
    return result


@router.post("/articles/abtests/{test_id}/apply", tags=["articles"])
def articles_abtest_apply(test_id: str, actor: rbac.Actor = Gen) -> dict:
    """Применить победителя: меняются заголовок и вводка, тело статьи остаётся прежним."""
    found = article_ab.test(test_id)
    if not found:
        raise HTTPException(status_code=404, detail={"kind": "test_not_found"})
    article_id = found.get("article_id")
    item = articles.article(article_id) if article_id else None
    if not item:
        raise HTTPException(status_code=404, detail={"kind": "article_not_found",
                                                     "message": "статья теста не найдена"})
    result = article_ab.apply(test_id, item["spec"])
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail={"kind": result.get("code"),
                                                     "message": result.get("reason")})
    doc = articles.replace(article_id, result["spec"], reason="A/B заголовка по данным")
    return {**{key: result[key] for key in ("winner", "title", "lead", "decision",
                                            "body_hash", "reason")},
            "article_id": article_id, "version": doc["version"], "status": doc["status"],
            "urls": {"self": f"/api/v1/articles/{article_id}",
                     "preview": f"/api/v1/articles/{article_id}/preview"}}


@router.get("/articles/evergreen", tags=["articles"])
def articles_evergreen(limit: int = Query(default=50, ge=1, le=200),
                       now: str | None = Query(default=None, max_length=40),
                       actor: rbac.Actor = Read) -> dict:
    """Вечнозелёные статьи и давность их ревью: что пора обновлять, а что ещё свежее.

    Отчёт считается из даты ревью и срока из окружения (`ARTICLE_REVIEW_DAYS`), а не из
    «когда-нибудь обновим». Статья без метки в отчёте видна отдельно: срок ей не назначен.
    """
    report = article_links.evergreen_report(articles.articles(limit), now=now)
    return {**report, "index": {"entries": len(article_links.index_rows())},
            "env": {"min_score": "ARTICLE_LINK_MIN_SCORE", "max_links": "ARTICLE_MAX_LINKS",
                    "review_days": "ARTICLE_REVIEW_DAYS"}}


@router.get("/articles/plan", tags=["articles"])
def articles_plan_quick(topic: str = Query(min_length=3, max_length=200),
                        sections: int = Query(default=5, ge=3, le=9),
                        locale: str | None = Query(default=None, max_length=8),
                        keywords: list[str] = Query(default_factory=list),
                        actor: rbac.Actor = Read) -> dict:
    """Структура статьи по одной теме: заголовки, визуалы и причины их выбора."""
    result = articles.plan({"topic": topic, "sections": sections, "locale": locale,
                            "keywords": keywords})
    if not result.get("ok"):
        raise HTTPException(status_code=422, detail={"kind": result.get("code"),
                                                     "message": result.get("reason")})
    return result


@router.post("/articles/plan", tags=["articles"])
def articles_plan_full(payload: ArticlePlanIn, actor: rbac.Actor = Read) -> dict:
    """План по брифу: то же, что соберёт статья, но без текста и без рендера."""
    brief = payload.model_dump(exclude_none=True)
    materials = brief.pop("materials", [])
    result = articles.plan(brief, sections=payload.sections, locale=payload.locale)
    if not result.get("ok"):
        raise HTTPException(status_code=422, detail={"kind": result.get("code"),
                                                     "message": result.get("reason")})
    numbers = articles.material_numbers(materials)
    return {**result, "materials": sorted(numbers),
            "unused_materials": sorted(set(numbers) - {
                str((section.get("data") or {}).get("material") or "").strip()
                for section in result["sections"]})}


@router.post("/articles", status_code=201, tags=["articles"])
def articles_create(payload: ArticleIn, actor: rbac.Actor = Gen) -> dict:
    """Бриф → готовая публикация: спека, проверки, HTML.

    Здесь нет «черновика текста»: на выходе — статья, которую можно ставить на сайт.
    Визуалы выбираются по данным разделов и правилам ``VISUAL_RULES``; каждая цифра
    ссылается на материал, у каждого визуала есть ``alt`` и ``reason``.
    """
    brief = payload.model_dump(exclude_none=True, by_alias=True)
    materials = brief.pop("materials", [])
    brand = brief.pop("brand", None)
    plan_data = brief.pop("plan", None)
    strict = bool(brief.pop("strict", True))
    result = articles.compose(brief, materials=materials, plan_data=plan_data, brand=brand,
                              workspace_id=actor.workspace_id)
    if not result.get("ok"):
        raise HTTPException(status_code=422, detail={"kind": result.get("code"),
                                                     "message": result.get("reason")})
    checks = result["checks"]
    if strict and not checks["ok"]:
        raise HTTPException(status_code=422, detail={
            "kind": "article_rejected", "issues": checks["issues"],
            "stats": checks["stats"],
            "hint": "передайте strict=false, чтобы сохранить черновик с замечаниями"})
    published = articles.publish(result["spec"], workspace_id=actor.workspace_id)
    return {**result, "reused": published["reused"],
            "article_id": published["article"]["id"],
            "status": published["article"]["status"],
            "urls": {"self": f"/api/v1/articles/{published['article']['id']}",
                     "preview": f"/api/v1/articles/{published['article']['id']}/preview"}}


@router.get("/articles", tags=["articles"])
def articles_list(limit: int = Query(default=50, ge=1, le=200),
                  actor: rbac.Actor = Read) -> dict:
    """Сохранённые статьи: заголовок, статус, объём и число визуалов (без спек)."""
    rows = articles.articles(limit)
    return {"items": [{"id": row["id"], "title": row.get("title"), "slug": row.get("slug"),
                       "status": row.get("status"), "created_at": row.get("created_at"),
                       "stats": (row.get("spec") or {}).get("stats"),
                       "issues": len(row.get("issues") or [])} for row in rows],
            "summary": articles.summary()}


@router.get("/articles/{article_id}", tags=["articles"], response_model=None)
def articles_get(article_id: str, actor: rbac.Actor = Read):
    """Статья по id; с расширением ``.html`` — сразу готовая страница."""
    suffix = ""
    if "." in article_id:
        article_id, _, suffix = article_id.rpartition(".")
    item = articles.article(article_id)
    if not item:
        raise HTTPException(status_code=404, detail="статья не найдена")
    if suffix in {"html", "htm"}:
        return HTMLResponse(articles.render_html(item["spec"]))
    return {"article": {key: value for key, value in item.items() if key != "spec"},
            "spec": item["spec"], "html_bytes": len(articles.render_html(item["spec"]))}


@router.get("/articles/{article_id}/preview", tags=["articles"])
def articles_preview(article_id: str, standalone: bool = Query(default=True),
                     actor: rbac.Actor = Read) -> HTMLResponse:
    """Превью статьи: тот же HTML, что уйдёт на сайт (со стилями и разметкой schema.org)."""
    item = articles.article(article_id)
    if not item:
        raise HTTPException(status_code=404, detail="статья не найдена")
    return HTMLResponse(articles.render_html(item["spec"], standalone=standalone))


@router.get("/articles/{article_id}/links", tags=["articles"])
def articles_links_preview(article_id: str, limit: int | None = Query(default=None, ge=1, le=10),
                           min_score: float | None = Query(default=None, ge=0.0, le=1.0),
                           actor: rbac.Actor = Read) -> dict:
    """Кандидаты на перелинковку: близость по смыслу, а не по тегам. Ничего не сохраняет.

    Порог и лимит берутся из параметров или окружения. Если ближайшие статьи ниже порога или
    уже связаны, ответ это называет (`no_candidates`, `already_linked`), а не отдаёт пустой
    список без объяснения.
    """
    item = articles.article(article_id)
    if not item:
        raise HTTPException(status_code=404, detail="статья не найдена")
    article_links.index_all(articles.articles(200))
    plan_links = article_links.link_plan(item["spec"], limit=limit, min_score=min_score)
    return {**plan_links, "article_id": article_id, "slug": item.get("slug"),
            "already": len(article_links._existing_urls(item["spec"]))}


@router.post("/articles/{article_id}/links", tags=["articles"])
def articles_links_apply(article_id: str, payload: ArticleLinksIn | None = None,
                         actor: rbac.Actor = Gen) -> dict:
    """Поставить «Читайте также»: блок встаёт перед призывом, версия статьи растёт на одну."""
    item = articles.article(article_id)
    if not item:
        raise HTTPException(status_code=404, detail="статья не найдена")
    body = payload or ArticleLinksIn()
    article_links.index_all(articles.articles(200))
    plan_links = article_links.link_plan(item["spec"], limit=body.limit,
                                         min_score=body.min_score)
    if not plan_links.get("ok"):
        raise HTTPException(status_code=409, detail={
            "kind": plan_links.get("code"), "message": plan_links.get("reason"),
            "threshold": plan_links.get("threshold"),
            "best_score": plan_links.get("best_score")})
    linked = article_links.apply_links(item["spec"], plan_links["links"])
    if not linked.get("ok"):
        raise HTTPException(status_code=409, detail={"kind": linked.get("code"),
                                                     "message": linked.get("reason")})
    doc = articles.replace(article_id, linked["spec"], reason="перелинковка по смыслу")
    return {"ok": True, "article_id": article_id, "version": doc["version"],
            "added": linked["added"], "links": plan_links["links"],
            "stats": article_links.link_stats(linked["spec"]),
            "status": doc["status"],
            "urls": {"self": f"/api/v1/articles/{article_id}",
                     "preview": f"/api/v1/articles/{article_id}/preview"}}


@router.post("/articles/{article_id}/abtests", status_code=201, tags=["articles"])
def articles_abtest_create(article_id: str, payload: ArticleAbIn | None = None,
                           actor: rbac.Actor = Gen) -> dict:
    """Завести A/B заголовка и вводки на этой статье: тело у вариантов одно и то же.

    Варианты собираются по реестру формул; тот, что ломает правила (длина, ключевое слово,
    запрещённое обещание пака), в тест не попадает и назван в `skipped`.
    """
    item = articles.article(article_id)
    if not item:
        raise HTTPException(status_code=404, detail="статья не найдена")
    body = payload or ArticleAbIn()
    result = article_ab.create(item["spec"], count=body.count, article_id=article_id,
                               kinds=body.kinds or None, salt=body.salt)
    if not result.get("ok"):
        raise HTTPException(status_code=422, detail={"kind": result.get("code"),
                                                     "message": result.get("reason"),
                                                     "skipped": result.get("skipped")})
    return {"ok": True, "article_id": article_id, "reused": result["reused"],
            **article_ab.report(result["test"]["id"])}


@router.get("/articles/{article_id}/translations", tags=["articles"])
def articles_translations_list(article_id: str, actor: rbac.Actor = Read) -> dict:
    """Переводы статьи и группа `hreflang`: у каждой версии должен быть адрес и ссылка на себя."""
    item = articles.article(article_id)
    if not item:
        raise HTTPException(status_code=404, detail="статья не найдена")
    rows = [row for row in articles.articles(200)
            if (row.get("spec") or {}).get("translation_of") == article_id]
    urls: dict[str, str] = {}
    source_locale = str((item["spec"].get("locale") or "ru")).lower()
    source_url = (item["spec"].get("seo") or {}).get("canonical") or f"/blog/{item.get('slug')}/"
    urls[source_locale] = source_url
    for row in rows:
        code = str((row["spec"].get("locale") or "")).lower()
        if code:
            urls[code] = (row["spec"].get("seo") or {}).get("canonical") or f"/blog/{row.get('slug')}/"
    group = article_i18n.hreflang(item["spec"], urls) if len(urls) > 1 else {
        "ok": False, "code": "single_locale", "rows": [],
        "reason": "переводов пока нет: hreflang-группа из одной версии не нужна"}
    return {"article_id": article_id, "source_locale": source_locale,
            "translations": [{"id": row["id"], "locale": (row["spec"] or {}).get("locale"),
                              "title": row.get("title"), "slug": row.get("slug"),
                              "complete": ((row["spec"] or {}).get("translation") or {}).get(
                                  "complete"),
                              "minutes": ((row["spec"] or {}).get("reading") or {}).get("minutes")}
                             for row in rows],
            "hreflang": group, "urls": urls}


@router.post("/articles/{article_id}/translations", status_code=201, tags=["articles"])
def articles_translate(article_id: str, payload: ArticleTranslationIn,
                       actor: rbac.Actor = Gen) -> dict:
    """Перевести статью данными: «хром» и `hreflang` переводит код, тексты приходят переводом.

    Непереведённое называется: строгий режим отказывает (`translation_missing`) и перечисляет
    поля, нестрогий — сохраняет черновик с `complete: false`. Полурусская английская версия —
    худший вид локализации, поэтому по умолчанию отказ.
    """
    item = articles.article(article_id)
    if not item:
        raise HTTPException(status_code=404, detail="статья не найдена")
    result = article_i18n.translate(item["spec"], payload.locale,
                                    translations=payload.translations,
                                    strict=payload.strict, article_id=article_id)
    if not result.get("ok"):
        status = 422 if result.get("code") in {"translation_missing", "locale_unknown",
                                              "already_localized"} else 409
        raise HTTPException(status_code=status, detail={
            "kind": result.get("code"), "message": result.get("reason"),
            "missing": result.get("missing") or [], "issues": result.get("issues") or []})
    spec = result["spec"]
    if payload.urls:
        # адрес версии берётся из набора hreflang: canonical и alternate не должны расходиться
        given = str(payload.urls.get(spec["locale"]) or "").strip()
        if given:
            spec.setdefault("seo", {})["canonical"] = given
        group = article_i18n.hreflang(spec, payload.urls, default=payload.urls.get("default"))
        if group.get("ok"):
            spec["hreflang"] = group["rows"]
    published = articles.publish(spec, workspace_id=actor.workspace_id)
    return {"ok": True, "article_id": published["article"]["id"], "reused": published["reused"],
            "locale": spec["locale"], "translation_of": article_id,
            "complete": spec["translation"]["complete"],
            "missing": result["missing"], "issues": result["issues"],
            "applied": len(result["applied"]),
            "reading": spec["reading"], "status": published["article"]["status"],
            "hreflang": spec.get("hreflang") or [],
            "urls": {"self": f"/api/v1/articles/{published['article']['id']}",
                     "preview": f"/api/v1/articles/{published['article']['id']}/preview"}}


@router.post("/articles/{article_id}/refresh", tags=["articles"])
def articles_refresh(article_id: str, payload: ArticleRefreshIn, actor: rbac.Actor = Gen) -> dict:
    """Обновить цифры вечнозелёной статьи по материалам клиента.

    Меняются только визуалы, у которых есть источник и ряд в присланных материалах. Нет
    материала — число остаётся прежним и попадает в `skipped`; нет изменений — `no_changes`
    и версия не растёт.
    """
    item = articles.article(article_id)
    if not item:
        raise HTTPException(status_code=404, detail="статья не найдена")
    result = article_links.refresh(item["spec"], payload.materials, now=payload.now,
                                   reason=payload.reason)
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail={
            "kind": result.get("code"), "message": result.get("reason"),
            "skipped": result.get("skipped") or []})
    doc = articles.replace(article_id, result["spec"], reason="обновление вечнозелёной статьи")
    return {"ok": True, "article_id": article_id, "version": doc["version"],
            "changes": result["changes"], "skipped": result["skipped"],
            "updated_at": result["updated_at"], "status": doc["status"],
            "freshness": article_links.freshness(result["spec"], now=payload.now)}


@router.get("/abtests", tags=["abtests"])
def ab_list(actor: rbac.Actor = Read) -> list[dict]:
    return abtests.list_tests()


@router.post("/abtests", status_code=201, tags=["abtests"])
def ab_create(payload: AbIn, actor: rbac.Actor = Write) -> dict:
    return abtests.create(payload.publication_id, payload.headline_a, payload.headline_b,
                          payload.channel, payload.check_after_minutes)


@router.post("/abtests/{test_id}/evaluate", tags=["abtests"])
def ab_evaluate(test_id: str, force: bool = False, actor: rbac.Actor = Read) -> dict:
    result = abtests.evaluate(test_id, force)
    if "error" in result:
        raise HTTPException(404, result["error"])
    return result


@router.post("/abtests/{test_id}/apply", tags=["abtests"])
def ab_apply(test_id: str, actor: rbac.Actor = Write) -> dict:
    result = abtests.apply(test_id)
    if "error" in result:
        raise HTTPException(409, result["error"])
    return result


# ============================== квоты ==============================
@router.get("/quota", tags=["billing"])
def quota_get(actor: rbac.Actor = Read) -> dict:
    return {"quota": quota.get(actor.workspace_id), "catalog": quota.catalog()}


@router.post("/quota/consume", tags=["billing"])
def quota_consume(key: str = Query(pattern="^(generations|publications)$"), amount: int = 1,
                  actor: rbac.Actor = Write) -> dict:
    try:
        return quota.consume(key, amount, actor.workspace_id)
    except quota.QuotaExceeded as exc:
        raise HTTPException(402, detail={"kind": "quota_exceeded", "key": exc.key,
                                        "used": exc.used, "limit": exc.limit,
                                        "hint": "докупите пакет: POST /billing/packs/generations_300"})


# ============================== биллинг ==============================
class InvoiceIn(BaseModel):
    amount_rub: float
    plan: str = "business"
    payer: str = "ООО «Договор Эксперт»"
    inn: str | None = None


@router.get("/billing/subscription", tags=["billing"])
def billing_subscription(actor: rbac.Actor = Read) -> dict:
    return billing.subscription()


@router.post("/billing/invoices", status_code=201, tags=["billing"])
def billing_invoice(payload: InvoiceIn, actor: rbac.Actor = Write) -> dict:
    return billing.create_invoice(payload.amount_rub, payload.plan, payload.payer, payload.inn)


@router.post("/billing/webhook", tags=["billing"])
async def billing_webhook(request: Request, payment_id: str, event: str = "payment.succeeded",
                    x_signature: str | None = Header(default=None, alias="X-Signature")) -> dict:
    """Вебхук ЮKassa: активирует тариф, формирует чек 54-ФЗ и УПД для юрлица.

    Ранее эндпоинт принимал любой POST — поддельное уведомление включало платный тариф.
    Теперь: HMAC-SHA256 тела (``YOOKASSA_WEBHOOK_SECRET``) и/или IP из подсетей ЮKassa.
    """
    raw = await request.body()
    client_ip = request.client.host if request.client else None
    security.verify_yookassa({"payment_id": payment_id, "event": event}, raw_body=raw,
                             signature=x_signature, client_ip=client_ip)
    result = billing.webhook(payment_id, event)
    if "error" in result:
        raise HTTPException(404, result["error"])
    return result


@router.get("/billing/payments", tags=["billing"])
def billing_payments(actor: rbac.Actor = Read) -> list[dict]:
    return billing.list_payments()


@router.post("/billing/autopay", tags=["billing"])
def billing_autopay(enabled: bool, actor: rbac.Actor = Write) -> dict:
    return billing.set_autopay(enabled)


@router.post("/billing/packs/{code}", tags=["billing"])
def billing_pack(code: str, actor: rbac.Actor = Write) -> dict:
    result = quota.buy_pack(code)
    if "error" in result:
        raise HTTPException(404, result["error"])
    return result


# ============================== auth ==============================
class MagicIn(BaseModel):
    email: str


@router.post("/auth/magic-link", tags=["auth"])
def auth_magic(payload: MagicIn,
               _rl: None = Depends(ratelimit.rate_limit("auth_login"))) -> dict:
    return auth.request_magic(payload.email)


@router.post("/auth/verify", tags=["auth"])
def auth_verify(token: str,
                _rl: None = Depends(ratelimit.rate_limit("auth_verify"))) -> dict:
    result = auth.verify_magic(token)
    if "error" in result:
        raise HTTPException(400, result["error"])
    return result


@router.get("/auth/oauth/{provider}", tags=["auth"])
def auth_oauth(provider: str) -> dict:
    result = auth.oauth_url(provider)
    if "error" in result:
        raise HTTPException(404, result["error"])
    return result


@router.get("/auth/onboarding", tags=["auth"])
def auth_onboarding() -> dict:
    return {"steps": auth.onboarding_steps(), "trial": auth.trial_policy()}


class RefreshIn(BaseModel):
    refresh_token: str


@router.post("/auth/refresh", tags=["auth"])
def auth_refresh(payload: RefreshIn,
                 _rl: None = Depends(ratelimit.rate_limit("auth_verify"))) -> dict:
    """Ротация refresh-сессии. Повторное использование старого токена гасит цепочку."""
    result = auth.refresh(payload.refresh_token)
    if "error" in result:
        raise HTTPException(401, {"type": "auth", "message": result["error"],
                                  "code": result.get("code")})
    return result


@router.post("/auth/logout", tags=["auth"])
def auth_logout(payload: RefreshIn) -> dict:
    return auth.logout(payload.refresh_token)


class OAuthCallbackIn(BaseModel):
    code: str
    # state обязателен: одноразовое значение из /auth/oauth/{provider}, живёт 10 минут
    state: str = ""
    redirect_uri: str = auth.REDIRECT_DEFAULT


@router.post("/auth/oauth/{provider}/callback", tags=["auth"])
def auth_oauth_callback(provider: str, payload: OAuthCallbackIn) -> dict:
    """Обмен ``code`` на токены провайдера и выдачу наших access/refresh (с проверкой state)."""
    result = auth.exchange_code(provider, payload.code, redirect_uri=payload.redirect_uri,
                                state=payload.state)
    if "error" in result:
        raise HTTPException(400, {"type": "oauth", **result})
    return result


class TwoFactorSetupIn(BaseModel):
    email: str | None = None


class TwoFactorCodeIn(BaseModel):
    code: str = Field(min_length=4, max_length=32)


class TwoFactorVerifyIn(BaseModel):
    challenge: str = Field(min_length=8)
    code: str = Field(min_length=4, max_length=32)


@router.post("/auth/2fa/setup", tags=["auth"])
def auth_2fa_setup(actor: rbac.Actor = Depends(rbac.current_actor)) -> dict:
    """Начать включение 2FA для текущего пользователя (TOTP + 10 backup-кодов)."""
    return auth.setup_2fa(actor.id, getattr(actor, "email", None) or actor.id)


@router.post("/auth/2fa/activate", tags=["auth"])
def auth_2fa_activate(payload: TwoFactorCodeIn,
                      actor: rbac.Actor = Depends(rbac.current_actor)) -> dict:
    """Подтвердить включение кодом из приложения — без этого 2FA не считается включённой."""
    result = auth.activate_2fa(actor.id, payload.code)
    if "error" in result:
        raise HTTPException(400, detail=result)
    return result


@router.post("/auth/2fa/verify", tags=["auth"])
def auth_2fa_verify(payload: TwoFactorVerifyIn,
                    _rl: None = Depends(ratelimit.rate_limit("auth_2fa"))) -> dict:
    """Второй фактор при входе: обменивает challenge на access/refresh."""
    result = auth.verify_second_factor(payload.challenge, payload.code)
    if "error" in result:
        raise HTTPException(401, detail=result)
    return result


@router.get("/auth/status", tags=["auth"])
def auth_status() -> dict:
    """Состояние входа: где живут одноразовые состояния и чем доставляется ссылка.

    `state_storage` важен для эксплуатации: `memory` означает, что при нескольких репликах
    API вход через OAuth и второй фактор работать не будут — нужен Redis.
    """
    status = auth.transport_status()
    status["state_storage"] = auth.state_backend()
    status["two_factor"] = {"supported": ["totp", "backup_code"],
                            "required_when_enabled": True,
                            "routes": ["/api/v1/auth/2fa/setup", "/api/v1/auth/2fa/activate",
                                       "/api/v1/auth/2fa/verify"]}
    return status


# ============================== API-ключи ==============================
class ApiKeyIn(BaseModel):
    name: str = Field(min_length=2, max_length=60)
    scopes: list[str] = Field(default_factory=lambda: ["read", "publish"])


@router.get("/keys", tags=["keys"])
def keys_list(actor: rbac.Actor = Depends(rbac.require("workspace:manage"))) -> dict:
    """Список ключей: только hint, полное значение показывается один раз при выпуске."""
    return {"describe": apikeys.describe(), "items": apikeys.list_keys(actor.workspace_id)}


@router.post("/keys", status_code=201, tags=["keys"])
def keys_create(payload: ApiKeyIn,
                actor: rbac.Actor = Depends(rbac.require("workspace:manage"))) -> dict:
    try:
        created = apikeys.issue(payload.name, payload.scopes, workspace_id=actor.workspace_id)
    except ValueError as exc:
        raise HTTPException(400, {"type": "validation", "message": str(exc)})
    return {**created, "warning": "сохраните ключ: полное значение больше не показывается"}


@router.post("/keys/{key_id}/revoke", tags=["keys"])
def keys_revoke(key_id: str,
                actor: rbac.Actor = Depends(rbac.require("workspace:manage"))) -> dict:
    return apikeys.revoke(key_id)


@router.post("/keys/verify", tags=["keys"])
def keys_verify(key: str = Query(..., min_length=8), scope: str | None = None,
                _rl: None = Depends(ratelimit.rate_limit("key_verify"))) -> dict:
    """Проверка ключа для интеграций (SDK, MCP).

    Публичная по необходимости — ключ и есть предъявитель права, — но с лимитом частоты:
    иначе маршрут превращается в оракул для перебора. Ответ урезан до того, что нужно
    клиенту: без внутренних ``id``/``name`` ключа. Пространство арендатора возвращается
    явно: получив ключ, интеграция обязана работать в нём, а не в «ws_de» по умолчанию.
    """
    result = apikeys.verify(key, scope)
    return {k: result[k] for k in ("ok", "code", "reason", "scopes", "workspace_id")
            if k in result}


# ============================ Brand Brain / RAG ============================
class KnowledgeSourceIn(BaseModel):
    kind: str = "site"
    location: str


class KnowledgeCardIn(BaseModel):
    kind: str = "product"
    title: str
    body: str
    source_id: str | None = None
    facts: list[str] = Field(default_factory=list)


@router.get("/knowledge", tags=["knowledge"])
def knowledge_stats(actor: rbac.Actor = Read) -> dict:
    return {"stats": knowledge.stats(), "sources": knowledge.list_sources()}


@router.post("/knowledge/sources", status_code=201, tags=["knowledge"])
def knowledge_add_source(payload: KnowledgeSourceIn, actor: rbac.Actor = Write) -> dict:
    return knowledge.add_source(payload.kind, payload.location)


@router.post("/knowledge/cards", status_code=201, tags=["knowledge"])
def knowledge_add_card(payload: KnowledgeCardIn, actor: rbac.Actor = Write) -> dict:
    return knowledge.add_card(payload.source_id, payload.kind, payload.title, payload.body,
                              facts=payload.facts)


@router.post("/knowledge/sources/{source_id}/index", tags=["knowledge"])
def knowledge_index(source_id: str, actor: rbac.Actor = Write) -> dict:
    try:
        return knowledge.index_source(source_id)
    except KeyError:
        raise HTTPException(404, "источник не найден")


@router.get("/knowledge/search", tags=["knowledge"])
def knowledge_search(q: str = Query(..., min_length=2), top_k: int = Query(6, ge=1, le=20),
                     actor: rbac.Actor = Read) -> dict:
    """Гибридный поиск (BM25 + вектор) — то же, что видит генератор."""
    hits = knowledge.search(q, top_k=top_k)
    ctx = knowledge.context_block(q, top_k=top_k)
    return {"query": q, "hits": hits, "citations": ctx["citations"],
            "backend": "bm25+cosine(1024), pgvector-ready"}


@router.get("/knowledge/cards", tags=["knowledge"])
def knowledge_cards(kind: str | None = None, actor: rbac.Actor = Read) -> list[dict]:
    return knowledge.list_cards(kind=kind)


# ============================== Инбокс ==============================
@router.get("/inbox", tags=["inbox"])
def inbox(limit: int = Query(30, ge=1, le=200), masked: bool = True,
          actor: rbac.Actor = Read) -> dict:
    """Входящие сообщения: по умолчанию отдаются маскированными (PII не уходит в UI-логи)."""
    items = store.inbox_tail(limit, masked=masked)
    return {"items": items, "count": len(items), "masked": masked,
            "pii_policy": "маскирование на входе, оригинал — только с правом inbox:read"}


@router.post("/inbox/sync", tags=["inbox"])
def inbox_sync(vk_owner: str | None = None, tg_chat: str | None = None,
               actor: rbac.Actor = Write) -> dict:
    """Ручной запуск синка (в проде — воркер по расписанию)."""
    return sync_workers.run_once(vk_owner=vk_owner, tg_chat=tg_chat)


@router.get("/inbox/pii-check", tags=["inbox"])
def inbox_pii_check(text: str = Query(..., min_length=1), actor: rbac.Actor = Read) -> dict:
    """Что именно будет замаскировано — без самих значений."""
    masked, _ = pii.mask(text)
    return {"report": pii.audit_report(text), "masked": masked}


# ============================== ЭДО / УПД ==============================
@router.get("/billing/upd/sample", tags=["billing"])
def upd_sample(amount_rub: int = Query(4900, ge=1),
               actor: rbac.Actor = Depends(rbac.require("billing:manage"))):
    """Скачать пример УПД (XML 5.03) — то, что уйдёт в Диадок/СБИС."""
    upd = edo.sample_upd(amount_kop=amount_rub * 100)
    xml = edo.build_xml(upd)
    # имя файла — ASCII, кириллица дополнительно в filename* (RFC 5987)
    ascii_name = f"upd_{upd.issued_at.isoformat()}_{upd.digest}.xml"
    from urllib.parse import quote

    return PlainTextResponse(xml, media_type="application/xml",
                             headers={"Content-Disposition":
                                      f"attachment; filename=\"{ascii_name}\"; "
                                      f"filename*=UTF-8''{quote(f'УПД_{upd.number}.xml')}"})


@router.get("/billing/edo/status", tags=["billing"])
def edo_status(actor: rbac.Actor = Depends(rbac.require("billing:manage"))) -> dict:
    return edo.status()


# ============================== суперадмин ==============================
Admin = Depends(rbac.require_platform_admin("platform:admin"))


class ImpersonateIn(BaseModel):
    tenant_id: str
    reason: str


@router.get("/admin/metrics", tags=["admin"])
def admin_metrics(actor: rbac.Actor = Admin) -> dict:
    return superadmin.metrics()


@router.get("/admin/finops", tags=["admin"])
def admin_finops(actor: rbac.Actor = Admin) -> dict:
    return superadmin.finops()


@router.get("/admin/tenants", tags=["admin"])
def admin_tenants(actor: rbac.Actor = Admin) -> list[dict]:
    return superadmin.tenants()


@router.post("/admin/impersonate", tags=["admin"])
def admin_impersonate(payload: ImpersonateIn,
                      actor: rbac.Actor = Depends(rbac.require_platform_admin("platform:impersonate"))) -> dict:
    result = superadmin.impersonate(payload.tenant_id, payload.reason, actor.role)
    if "error" in result:
        raise HTTPException(400, result["error"])
    return result


@router.get("/admin/ops", tags=["admin"])
def admin_ops(actor: rbac.Actor = Depends(rbac.require_platform_admin("platform:ops"))) -> dict:
    return superadmin.OPS


@router.post("/admin/ops/pause", tags=["admin"])
def admin_pause(paused: bool,
                actor: rbac.Actor = Depends(rbac.require_platform_admin("platform:ops"))) -> dict:
    return superadmin.toggle_pause(paused)


@router.get("/admin/rules", tags=["admin"])
def admin_rules(actor: rbac.Actor = Admin) -> list[dict]:
    return superadmin.rules()


class RuleAdminIn(BaseModel):
    code: str
    title: str | None = None
    severity: str = "warn"
    version: int = 202612
    active: bool = True


@router.post("/admin/rules", tags=["admin"])
def admin_rule_upsert(payload: RuleAdminIn, actor: rbac.Actor = Admin) -> dict:
    return superadmin.upsert_rule(payload.model_dump())
