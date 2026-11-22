"""HTTP-контракт ядра (v1 + v2). См. 10-ARCHITECTURE/openapi.yaml.

Изменения v2 (зоны роста MVP):
* RBAC проверяется на сервере: каждая запись и публикация требует права роли;
* автоматизация ОРД: ``POST /ord/erid`` — erid в один клик;
* короткие ссылки: ``POST /links`` + публичный ``GET /l/{code}`` с логом клика;
* онбординг: ``GET /onboarding/quickstart``, ``POST /channels/{ch}/connect`` (MAX — песочница).
"""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse

from . import (accounts, compliance, generator, links, media, metrics, onboarding, ord,
               quota, ratelimit, rbac, scheduler, store)
from .adapters import NormalizedError, get_adapter
from .schemas import (
    ActorOut, AnalyticsSummary, ChannelAccount, ComplianceReport, ConnectOut, GenerateIn,
    GenerateOut, LinkIn, LinkOut, LinkStatsOut, OrdProviderOut, OrdRequestIn, OrdSubmissionOut,
    Publication, PublicationIn, Signal, SignalIn,
)

router = APIRouter(prefix="/api/v1")

# Публичный роутер без префикса: короткие ссылки и health-check для мониторинга
public_router = APIRouter()


# ============================ system / RBAC ============================
@router.get("/me", response_model=ActorOut, tags=["rbac"])
def me(actor: rbac.Actor = Depends(rbac.current_actor)) -> dict:
    """Кто я и что мне можно. UI использует это, чтобы не показывать запрещённые кнопки."""
    return {
        "id": actor.id, "role": actor.role,
        "role_title": rbac.ROLE_TITLES.get(actor.role, actor.role),
        "workspace_id": actor.workspace_id,
        "permissions": sorted(actor.permissions),
        "is_platform_admin": actor.is_platform_admin,
        "source": actor.source,
    }


@router.get("/team/roles", tags=["rbac"])
def roles(actor: rbac.Actor = Depends(rbac.require("workspace:read"))) -> dict:
    return rbac.describe()


@router.get("/audit", tags=["rbac"])
def audit_tail(limit: int = Query(50, ge=1, le=500),
               actor: rbac.Actor = Depends(rbac.require("workspace:manage"))) -> dict:
    return store.audit_tail(limit)


# ============================== channels ==============================
@router.get("/channels", response_model=list[ChannelAccount], tags=["channels"])
def list_channels(actor: rbac.Actor = Depends(rbac.require("channel:read"))) -> list[dict]:
    out = []
    for account in store.list_channel_accounts():
        item = dict(account)
        conn = onboarding.state_of(account["channel"])
        if conn:
            item["connection_state"] = conn["state"]
            item["sandbox"] = conn.get("sandbox", False)
        out.append(item)
    return out


@router.post("/channels/{channel_id}/token", tags=["channels"])
def save_channel_token(channel_id: str, payload: dict,
                       actor: rbac.Actor = Depends(rbac.require("channel:manage"))) -> dict:
    """Сохранить токен канала.

    Токен шифруется AES-256-GCM (:mod:`orbita.crypto`) и в БД попадает только шифротекстом.
    В ответе — отпечаток (последние 4 символа + hash8), сам секрет не возвращается никогда.
    """
    token = str((payload or {}).get("token", "")).strip()
    if len(token) < 12:
        raise HTTPException(422, detail={"kind": "weak_token",
                                         "message": "Токен короче 12 символов — похоже на ошибку ввода."})
    account = store.get_channel_account(channel_id)
    if not account:
        raise HTTPException(404, "канал не найден")
    external_id = str((payload or {}).get("external_id") or "").strip() or None
    saved = store.set_channel_token(channel_id, token, channel=account["channel"],
                                    external_id=external_id,
                                    workspace_id=getattr(actor, "workspace_id", None))
    store.audit(actor.role, "POST", f"/channels/{channel_id}/token", 200,
                {"channel": account["channel"], "token_hint": saved["token_hint"]})
    return {"id": channel_id, "channel": account["channel"],
            "token_hint": saved["token_hint"], "external_id": saved.get("external_id"),
            "encrypted": True}


@router.post("/channels/{channel_id}/health", tags=["channels"])
async def channel_health(channel_id: str,
                         actor: rbac.Actor = Depends(rbac.require("channel:read"))) -> dict:
    account = store.get_channel_account(channel_id)
    if not account:
        raise HTTPException(404, "канал не найден")
    adapter = get_adapter(account["channel"])
    return {"channel": account["channel"], "adapter": type(adapter).__name__,
            "capabilities": adapter.capabilities, "budget": adapter.rate_budget()}


@router.get("/channels/{channel}/connect-url", tags=["channels"])
def connect_url(channel: str,
                actor: rbac.Actor = Depends(rbac.require("channel:read"))) -> dict:
    modes = {
        "max": ("bot_verification", "Создайте бота через @MasterBot после верификации ИП/ООО "
                                   "(dev.max.ru). До верификации канал работает в песочнице."),
        "vk": ("oauth", "Создайте приложение VK ID, получите токен сообщества с правами wall."),
        "telegram": ("bot_token", "Создайте бота у @BotFather и выдайте ему права админа канала."),
        "x": ("oauth", "OAuth 2.0 PKCE; учитывайте pay-per-use ($0,015/пост, $0,20 со ссылкой)."),
    }
    mode, instructions = modes.get(channel, ("manual", "Для этого канала доступен ручной режим публикации."))
    return {"channel": channel, "mode": mode, "instructions": instructions,
            "url": f"/oauth/{channel}/start" if mode == "oauth" else None,
            "sandbox": channel == "max"}


@router.post("/channels/{channel}/connect", response_model=ConnectOut, tags=["channels"])
def connect_channel(channel: str,
                    actor: rbac.Actor = Depends(rbac.require("channel:manage"))) -> dict:
    """Подключение канала. MAX возвращает pending_verification + sandbox вместо ошибки."""
    state = onboarding.connect(channel)
    store.audit(actor.role, "POST", f"/channels/{channel}/connect", 200,
                {"state": state["state"]})
    return state


# ============================== onboarding ==============================
@router.get("/onboarding/quickstart", tags=["onboarding"])
def quickstart(actor: rbac.Actor = Depends(rbac.require("channel:read"))) -> dict:
    return onboarding.quickstart()


@router.post("/onboarding/{channel}/verify", response_model=ConnectOut, tags=["onboarding"])
def verify_channel(channel: str,
                   actor: rbac.Actor = Depends(rbac.require("workspace:manage"))) -> dict:
    """Демо: завершает модерацию MAX (в проде вызывается вебхуком модерации)."""
    return onboarding.verify(channel)


# =============================== ОРД ===============================
@router.get("/ord/providers", response_model=list[OrdProviderOut], tags=["compliance"])
def ord_providers(actor: rbac.Actor = Depends(rbac.require("ad:request_erid"))) -> list[dict]:
    return ord.describe_providers()


@router.post("/ord/erid", response_model=OrdSubmissionOut, status_code=201, tags=["compliance"])
def request_erid(payload: OrdRequestIn,
                 actor: rbac.Actor = Depends(rbac.require("ad:request_erid"))) -> dict:
    """Получение erid в один клик из модального окна комплаенса."""
    creative = ord.OrdCreative(
        channel=payload.channel, advertiser_name=payload.advertiser_name,
        advertiser_inn=payload.advertiser_inn, text=payload.text,
        variant_id=payload.variant_id, external_url=payload.external_url,
    )
    provider_name = payload.provider or ord.default_provider()
    try:
        provider = ord.get_provider(provider_name)
        result = provider.submit(creative)
    except ord.OrdNotConfigured as exc:
        # Честная деградация: не настроен боевой ОРД — работаем в песочнице, но говорим об этом
        result = ord.PROVIDERS["mock"].submit(creative)
        result.note = f"{exc} Выдан erid песочницы."
    except ord.OrdProviderError as exc:
        raise HTTPException(502, detail={"kind": "ord_error", "message": str(exc),
                                        "retryable": True, "provider": provider_name})
    row = store.save_ord_submission(result)
    store.audit(actor.role, "POST", "/ord/erid", 201,
                {"erid": row["erid"], "provider": row["provider"]})
    return row


@router.get("/ord/submissions", response_model=list[OrdSubmissionOut], tags=["compliance"])
def ord_submissions(actor: rbac.Actor = Depends(rbac.require("ad:approve"))) -> list[dict]:
    return store.list_ord_submissions()


# ========================= короткие ссылки =========================
@router.post("/links", response_model=LinkOut, status_code=201, tags=["links"])
def create_link(payload: LinkIn,
                actor: rbac.Actor = Depends(rbac.require("link:create"))) -> dict:
    link = links.build(payload.target_url, payload.channel, payload.campaign,
                       variant_id=payload.variant_id)
    return store.save_link(link)


@router.get("/links", response_model=list[LinkOut], tags=["links"])
def list_links(actor: rbac.Actor = Depends(rbac.require("link:read"))) -> list[dict]:
    return store.list_links()


@router.get("/links/{code}/stats", response_model=LinkStatsOut, tags=["links"])
def link_stats(code: str, actor: rbac.Actor = Depends(rbac.require("link:read"))) -> dict:
    link = store.get_link(code)
    if not link:
        raise HTTPException(404, "ссылка не найдена")
    return links.stats(link, store.clicks_for(code))


@public_router.get("/l/{code}", include_in_schema=True, tags=["links"])
def follow_link(code: str, request: Request,
                _rl: None = Depends(ratelimit.rate_limit("link_redirect"))):
    """Публичный редирект. 302 — иначе браузер закэширует и клики перестанут считаться.

    Ограничен по IP (60 запросов в минуту): без лимита счётчики кликов накручиваются
    скриптом, а аналитика клиента врёт.
    """
    link = store.get_link(code)
    if not link:
        raise HTTPException(404, "ссылка не найдена или истекла")
    event = links.record_click(
        code, referer=request.headers.get("referer"),
        user_agent=request.headers.get("user-agent"),
        ip=request.client.host if request.client else None,
    )
    store.add_click(event)
    return RedirectResponse(link["target_url"], status_code=302)


# ============================== signals ==============================
@router.post("/signals", response_model=Signal, status_code=201, tags=["signals"])
def create_signal(payload: SignalIn,
                  actor: rbac.Actor = Depends(rbac.require("signal:create"))) -> dict:
    # сигнал принадлежит workspace актора: раньше он всегда писался в ws_de,
    # и данные всех клиентов смешивались в одном «демо»-пространстве
    return store.create_signal(payload.kind, payload.title, payload.source_url,
                               payload.raw_text, payload.goal,
                               workspace_id=actor.workspace_id)


@router.get("/signals", tags=["signals"])
def list_signals(limit: int = 50,
                 actor: rbac.Actor = Depends(rbac.require("signal:read"))) -> dict:
    """Инфоповоды своего рабочего пространства (не больше 500 за раз)."""
    rows = store.list_signals(workspace_id=actor.workspace_id, limit=min(max(limit, 1), 500))
    return {"items": rows, "count": len(rows), "workspace_id": actor.workspace_id}


@router.get("/signals/{signal_id}", tags=["signals"])
def get_signal(signal_id: str,
               actor: rbac.Actor = Depends(rbac.require("signal:read"))) -> dict:
    """Инфоповод. Чужой workspace = 404: не подтверждаем существование чужих данных."""
    signal = store.get_signal(signal_id, workspace_id=actor.workspace_id)
    if signal is None:
        raise HTTPException(404, {"kind": "not_found", "signal_id": signal_id})
    return signal


@router.post("/signals/{signal_id}/generate", response_model=GenerateOut, tags=["signals"])
def generate(signal_id: str, payload: GenerateIn | None = None,
             actor: rbac.Actor = Depends(rbac.require("variant:generate"))) -> dict:
    signal = store.get_signal(signal_id, workspace_id=actor.workspace_id)
    if signal is None:
        raise HTTPException(404, "инфоповод не найден")
    channels = (payload.channels if payload else None) or ["max", "vk", "x"]
    link = signal.get("source_url")
    # Квота генераций списывается ДО вызова модели: иначе лимит «есть», но не ограничивает.
    try:
        quota.consume("generations", len(channels), actor.workspace_id)
    except quota.QuotaExceeded as exc:
        metrics.inc("quota_exceeded_generations")
        raise HTTPException(402, detail={"kind": "quota_exceeded", "key": exc.key,
                                         "used": exc.used, "limit": exc.limit,
                                         "hint": "докупите пакет: POST /billing/packs/generations_300"})
    return generator.run(signal_id, list(channels), link_url=link,
                         workspace_id=actor.workspace_id)


@router.post("/variants/{variant_id}/compliance-check", response_model=ComplianceReport,
             tags=["compliance"])
def compliance_check(variant_id: str, erid: str | None = Query(default=None),
                     actor: rbac.Actor = Depends(rbac.require("publication:read"))) -> dict:
    variant = store.get_variant(variant_id)
    if not variant:
        raise HTTPException(404, "вариант не найден")
    account = next((a for a in store.list_channel_accounts() if a["channel"] == variant["channel"]), None)
    legal = accounts.legal_entity(actor.workspace_id)
    ctx = compliance.VariantContext(
        channel=variant["channel"], body=variant["body"], erid=erid,
        audience_size=(account or {}).get("audience_size", 0),
        advertiser_name=legal.get("legal_name"), advertiser_site=legal.get("site_url"),
    )
    return compliance.check(ctx).model_dump()


# ============================ publications ============================
@router.post("/publications", response_model=Publication, status_code=201, tags=["publications"])
def create_publication(payload: PublicationIn, background: BackgroundTasks,
                       actor: rbac.Actor = Depends(rbac.require("publication:create"))) -> dict:
    variant = store.get_variant(payload.variant_id)
    account = store.get_channel_account(payload.channel_account_id)
    if not variant:
        raise HTTPException(404, "вариант не найден")
    if not account:
        raise HTTPException(404, "канал не найден")

    try:
        quota.consume("publications", 1, actor.workspace_id)
    except quota.QuotaExceeded as exc:
        metrics.inc("quota_exceeded_publications")
        raise HTTPException(402, detail={"kind": "quota_exceeded", "key": exc.key,
                                         "used": exc.used, "limit": exc.limit,
                                         "hint": "докупите пакет: POST /billing/packs/publications_200"})

    # реклама без erid не создаётся вообще — это не «предупреждение в UI»
    if payload.is_ad and not payload.erid:
        raise HTTPException(409, detail={
            "kind": "ad_marking_required",
            "message": "Реклама без erid не создаётся",
            "hint": "Получите erid: POST /api/v1/ord/erid",
        })

    # Вложения проверяем ДО создания публикации: ссылка на несуществующий артефакт
    # иначе всплыла бы в момент отправки, то есть после согласования и в очереди.
    media_ids = list(dict.fromkeys(payload.media_ids or []))      # дубли схлопываем, порядок храним
    unknown = [mid for mid in media_ids if media.asset(mid) is None]
    if unknown:
        raise HTTPException(409, detail={
            "kind": "media_asset_unknown",
            "message": "артефакт не найден — карточка не отрендерена или удалена по TTL",
            "asset_ids": unknown[:5],
            "hint": "POST /api/v1/media/cards, затем передайте asset_id из ответа",
        })

    legal = accounts.legal_entity(actor.workspace_id)
    report = compliance.check(compliance.VariantContext(
        channel=variant["channel"], body=variant["body"], erid=payload.erid,
        audience_size=account.get("audience_size", 0),
        advertiser_name=legal.get("legal_name"), advertiser_site=legal.get("site_url"),
    ), is_ad=True if payload.is_ad else None)
    if any(r.severity == "block" for r in report.reasons):
        # 409 + отчёт: клиент видит, какое правило сработало
        raise HTTPException(409, detail={"compliance": report.model_dump()})

    pub = store.create_publication(payload.variant_id, payload.channel_account_id,
                                   payload.scheduled_at, payload.erid,
                                   is_ad=payload.is_ad,
                                   require_approval=payload.require_approval,
                                   media_ids=media_ids)
    store.audit(actor.role, "POST", "/publications", 201,
                {"publication_id": pub["id"], "is_ad": payload.is_ad,
                 "media": len(media_ids),
                 "erid": payload.erid, "status": pub["status"],
                 "erid_source": "ord" if payload.erid and store.get_ord_submission(payload.erid)
                 else ("manual" if payload.erid else None)})

    # Отложенная публикация ставится в очередь СРАЗУ: ARQ с _defer_by, если есть Redis,
    # иначе её доберёт sweep воркера. Без этого scheduled_at был просто полем в карточке.
    if pub["status"] == "queued":
        background.add_task(scheduler.schedule_publication, pub["id"], payload.scheduled_at)
    return pub


@router.get("/publications/{publication_id}", response_model=Publication, tags=["publications"])
def get_publication(publication_id: str,
                    actor: rbac.Actor = Depends(rbac.require("publication:read"))) -> dict:
    pub = store.get_publication(publication_id)
    if not pub:
        raise HTTPException(404, "публикация не найдена")
    return pub


@router.post("/publications/{publication_id}/approve", response_model=Publication,
             tags=["publications"])
def approve_publication(publication_id: str, background: BackgroundTasks,
                        note: str | None = Query(default=None),
                        actor: rbac.Actor = Depends(rbac.require("publication:approve"))) -> dict:
    """Подпись под размещением. Рекламу может одобрить только носитель ad:approve + erid обязателен."""
    pub = store.get_publication(publication_id)
    if not pub:
        raise HTTPException(404, "публикация не найдена")

    if pub.get("is_ad"):
        if not rbac.can(actor.role, "ad:approve"):
            raise HTTPException(403, detail={
                "kind": "forbidden", "permission": "ad:approve", "role": actor.role,
                "allowed_roles": rbac.roles_with("ad:approve"),
                "message": "Рекламное размещение утверждает юрист/комплаенс или владелец.",
            })
        if not pub.get("erid"):
            raise HTTPException(409, detail={"kind": "ad_marking_required",
                                            "hint": "POST /api/v1/ord/erid"})
        submission = store.get_ord_submission(pub["erid"])
        if submission and submission["status"] != "registered":
            raise HTTPException(409, detail={"kind": "erid_not_registered",
                                            "status": submission["status"]})
    approved = store.approve_publication(publication_id, actor.role, note)
    store.audit(actor.role, "POST", f"/publications/{publication_id}/approve", 200,
                {"is_ad": pub.get("is_ad"), "erid": pub.get("erid")})
    if approved.get("status") == "queued":
        background.add_task(scheduler.schedule_publication, publication_id,
                            approved["scheduled_at"])
    return approved


@router.post("/publications/{publication_id}/publish-now", tags=["publications"])
async def publish_now(publication_id: str,
                      actor: rbac.Actor = Depends(rbac.require("publication:publish"))) -> dict:
    """Синхронный прогон публикации (в проде — задача в очереди, здесь — для отладки)."""
    pub = store.get_publication(publication_id)
    if not pub:
        raise HTTPException(404, "публикация не найдена")
    if pub["status"] != "queued":
        raise HTTPException(409, detail={
            "kind": "approval_required", "status": pub["status"],
            "message": ("Реклама и режим «сначала согласование» публикуются только после "
                        "подписи роли Юрист/Комплаенс или Владелец."),
            "approve_endpoint": f"/api/v1/publications/{publication_id}/approve",
        })
    # Ручная публикация идёт ТЕМ ЖЕ путём, что ARQ-задача и cron: захват аренды,
    # токен из хранилища, финальный текст с маркировкой, машина состояний.
    # Раньше здесь был отдельный вызов адаптера — он мог отправить второй пост
    # параллельно с воркером и не применял маркировку рекламы.
    from .workers.tasks import publish_publication

    result = await publish_publication({"worker_id": f"api:{actor.role}"}, publication_id)
    if result.get("skipped"):
        raise HTTPException(409, detail={"kind": "not_claimable", **result})
    if result.get("status") != "published":
        raise HTTPException(502, detail={"kind": "publish_failed", **result})
    store.audit(actor.role, "POST", f"/publications/{publication_id}/publish-now", 200,
                {"external_id": result.get("external_id")})
    return store.get_publication(publication_id) or result



# ============================== analytics ==============================
@router.get("/analytics/summary", response_model=AnalyticsSummary, tags=["analytics"])
def analytics_summary(actor: rbac.Actor = Depends(rbac.require("analytics:read"))) -> dict:
    summary = store.analytics_summary()
    summary["link_clicks"] = store.link_clicks_total()
    summary["links_total"] = len(store.list_links())
    return summary
