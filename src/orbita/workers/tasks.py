"""Задачи воркера. Вызываются ARQ (Redis) или sweep-таймером без Redis.

Принципы (см. ADR 0017 и :mod:`orbita.publishing`):

  * **единственный вход** — ``claim_publication``: он атомарно переводит публикацию
    в ``publishing`` и выдаёт аренду. Мимо него не идёт ни ARQ-задача, ни cron, ни
    ручное «опубликовать сейчас», поэтому два процесса не возьмут один пост;
  * **аренда вместо «вечного publishing»**: если воркер упал, ``reclaim_expired()``
    вернёт публикацию в ``retry_wait`` — зависших задач нет;
  * **разные решения по классу ошибки**: transient → ``retry_wait`` с backoff,
    auth → ``needs_reauth`` (нужна переавторизация канала), fatal → ``failed``;
  * **reconciliation**: если ``external_id`` уже записан, повтор не выполняется —
    внешний пост не дублируется;
  * **токен конкретного канала** берётся из хранилища аккаунта, а не из переменных
    окружения процесса; без токена адаптер работает в dry-run и это видно в ответе;
  * **маркировка применяется к тексту публикации**, а не остаётся в отчёте проверки.
"""
from __future__ import annotations

import asyncio
import logging
import os

from .. import accounts, channel_limits, compliance, media, publishing, store
from ..adapters import NormalizedError, get_adapter
from ..adapters.base import PublishInput

log = logging.getLogger("orbita.worker")


async def _renew_lease(pid: str, stop: asyncio.Event,
                       interval: float = publishing.DEFAULT_LEASE_SECONDS / 3) -> None:
    """Продлевать аренду, пока идёт публикация.

    Раньше `job_timeout` совпадал со сроком аренды: задача, работавшая чуть дольше
    (медленная загрузка медиа, ретрай внутри адаптера), теряла аренду во время работы,
    и пост мог быть взят вторым воркером. Теперь аренда продлевается, пока задача жива.
    """
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=max(5.0, interval))
        except asyncio.TimeoutError:
            store.pub_update(pid, lease_until=publishing.lease_until(), force=True)


def _final_text(variant: dict, pub: dict) -> tuple[str, bool]:
    """Текст, который реально уходит в канал.

    Если публикация — реклама (``is_ad``) или у неё есть ``erid``, блок маркировки
    добавляется в конец текста здесь, а не «где-то в отчёте». Возвращает
    ``(текст, применена_ли_маркировка)``.
    """
    body = (variant or {}).get("body") or ""
    if not (pub.get("is_ad") or pub.get("erid")):
        return body, False
    account = store.get_channel_account(pub.get("channel_account_id")) or {}
    legal = accounts.legal_entity(pub.get("workspace_id") or "")
    ctx = compliance.VariantContext(
        channel=pub.get("channel") or "",
        body=body,
        erid=pub.get("erid"),
        audience_size=int(account.get("audience_size") or 0),
        channel_in_blogger_registry=None,           # реестр не подтверждён — правило даст warn
        # реквизиты рекламодателя берутся из профиля бренда, а не из константы модуля:
        # иначе реклама клиента уходила бы с чужим юрлицом
        advertiser_name=legal.get("legal_name"),
        advertiser_site=legal.get("site_url"),
    )
    return compliance.apply_marking(body, ctx), True


def _apply(pid: str, decision: publishing.Decision) -> str:
    """Записать решение машины состояний в хранилище, соблюдая разрешённые переходы."""
    current = (store.get_publication(pid) or {}).get("status", "")
    target = decision.status
    if current and not publishing.can_transition(current, target):
        log.error("недопустимый переход %s → %s для %s: пишу failed", current, target, pid)
        store.mark_failed(pid, decision.error or "недопустимый переход", "failed")
        return "failed"
    if target == "retry_wait":
        store.mark_retry(pid, decision.error or "повтор", decision.next_attempt_at or publishing.now())
    elif target == "needs_reauth":
        store.mark_failed(pid, decision.error or "нужна переавторизация канала", "needs_reauth")
    else:
        store.mark_failed(pid, decision.error or "ошибка публикации", target)
    return target


async def publish_publication(ctx, publication_id: str, worker_id: str | None = None) -> dict:
    """Опубликовать материал в канал: захват → сверка → публикация → фиксация результата."""
    pub = store.get_publication(publication_id)
    if pub is None:
        return {"skipped": "not_found", "publication_id": publication_id}

    # reconciliation: внешний пост уже создан — повторять нельзя
    if pub.get("status") == "published" or pub.get("external_id"):
        return {"skipped": "already_published", "publication_id": publication_id,
                "external_id": pub.get("external_id")}

    if pub.get("status") in {"draft", "needs_approval", "blocked_by_compliance", "cancelled",
                             "failed", "needs_reauth"}:
        return {"skipped": f"not_queued:{pub['status']}", "publication_id": publication_id}

    worker = worker_id or (ctx.get("worker_id") if isinstance(ctx, dict) else None) or "worker"
    if not store.claim_publication(publication_id, worker_id=worker):
        # задачу уже взял другой воркер либо ещё не наступил срок повтора
        return {"skipped": "not_claimable", "publication_id": publication_id,
                "status": (store.get_publication(publication_id) or {}).get("status")}

    attempt = int((store.get_publication(publication_id) or {}).get("attempt_count") or 1)
    variant = store.get_variant(pub.get("variant_id"), workspace_id=pub.get("workspace_id"))
    if variant is None:
        return {"status": _apply(publication_id, publishing.Decision(
            status="failed", error="вариант публикации не найден в хранилище")),
            "kind": "missing_variant", "publication_id": publication_id}

    # Лимит частоты площадки: ведро на (workspace, аккаунт, канал). Проверяется ДО
    # обращения к API, но после аренды — публикация остаётся за воркером, а отправка
    # откладывается без списания попытки: «площадка просит подождать» ≠ «ошибка».
    budget = channel_limits.take(pub.get("channel"), workspace_id=pub.get("workspace_id"),
                                 account_id=pub.get("channel_account_id"))
    if not budget.allowed:
        from .. import metrics
        metrics.inc("publish_deferred_channel_limit")
        store.defer_publication(publication_id, retry_in=budget.retry_after)
        log.info("publish deferred %s: лимит канала, повтор через %s с",
                 publication_id, budget.retry_after)
        return {"status": "deferred", "kind": "channel_rate_limited",
                "retry_after": budget.retry_after, "publication_id": publication_id}

    account = store.get_channel_account(pub.get("channel_account_id")) or {}
    token = store.get_channel_token(pub.get("channel_account_id"))
    text, marked = _final_text(variant, pub)
    adapter = get_adapter(pub["channel"], token=token)

    # Вложения: id артефактов → подписанные ссылки. Именно здесь, а не при создании
    # публикации: ссылка живёт час, а пост может ждать слота три дня. Если ссылку
    # собрать нельзя (не задан MEDIA_URL_SECRET, артефакт удалён по TTL), публикация
    # НЕ уходит без картинки молча — это отдельный класс ошибки, а не «transient».
    try:
        media_urls = [media.public_url(mid, media.best_format(mid) or "png")
                      for mid in (pub.get("media_ids") or [])]
        missing = [mid for mid in (pub.get("media_ids") or []) if media.asset(mid) is None]
        if missing:
            raise ValueError(f"артефакты удалены или не найдены: {missing[:3]}")
    except (ValueError, KeyError) as exc:
        log.error("publish %s: вложения недоступны (%s)", publication_id, exc)
        return {"status": _apply(publication_id, publishing.Decision(
            status="failed", error=f"media_unavailable: {exc}")),
                "kind": "media_unavailable", "publication_id": publication_id}

    # Журнал попыток: намерение фиксируется ДО обращения к площадке. Незакрытая прошлая
    # попытка означает «возможно, уже опубликовано» — тогда адаптер идёт в режиме сверки.
    previous = store.attempt_last(publication_id)
    uncertain = bool(previous and previous.get("finished_at") is None)
    store.attempt_begin(publication_id, attempt, worker, pub["idempotency_key"],
                        workspace_id=pub.get("workspace_id"))
    stop = asyncio.Event()
    renewer = asyncio.create_task(_renew_lease(publication_id, stop))

    try:
        result = await adapter.publish(
            PublishInput(account_external_id=str(account.get("external_id") or "1"),
                         text=text, media_urls=media_urls,
                         link_url=variant.get("link_url"),
                         link_in_reply=variant.get("link_in_reply", False),
                         token=token, reconcile=uncertain),
            idempotency_key=pub["idempotency_key"],
        )
    except NormalizedError as exc:
        decision = publishing.after_error(exc.kind, str(exc), attempt)
        store.attempt_finish(publication_id, decision.status, error=str(exc))
        log.warning("publish failed %s: %s → %s", publication_id, exc.kind, decision.status)
        return {"status": _apply(publication_id, decision), "kind": exc.kind,
                "attempt": attempt, "retry_at": decision.next_attempt_at,
                "publication_id": publication_id}
    except Exception as exc:                            # noqa: BLE001 — сеть, таймауты, чужие SDK
        decision = publishing.after_error("transient", f"{exc.__class__.__name__}: {exc}", attempt)
        # попытку намеренно НЕ закрываем: неизвестно, приняла ли площадка запрос.
        # Следующая попытка увидит незакрытую запись и пойдёт со сверкой (reconcile).
        log.exception("publish crashed %s", publication_id)
        return {"status": _apply(publication_id, decision), "kind": "transient",
                "attempt": attempt, "publication_id": publication_id, "uncertain": True}
    finally:
        stop.set()
        renewer.cancel()

    store.mark_published(publication_id, result.external_id, result.external_url or "")
    store.attempt_finish(publication_id, "published", external_id=result.external_id)
    return {"status": "published", "external_id": result.external_id,
            "marked": marked, "token_used": bool(token), "attempt": attempt,
            "publication_id": publication_id}


async def publish_due(ctx) -> dict:
    """Cron-задача ARQ: добрать всё, что созрело (страховка к отложенным задачам)."""
    from ..scheduler import sweep_due

    summary = await sweep_due()
    if summary["due"]:
        log.info("sweep: due=%s claimed=%s reclaimed=%s",
                 summary["due"], summary["claimed"], summary.get("reclaimed", 0))
    return summary


async def purge_media(ctx) -> dict:
    """Cron: удалить медиа-артефакты старше TTL (по умолчанию 14 дней).

    Зачем это вообще нужно: спул/S3 растёт на каждый рендер (черновики, отменённые
    публикации, A/B-варианты), а артефакт после публикации всё равно не нужен —
    в канале лежит уже загруженный файл. Уборка идёт ночью и только по артефактам,
    у которых истёк срок; документы публикаций она не трогает.
    """
    ttl_days = int(os.getenv("MEDIA_TTL_DAYS", "14"))
    report = media.purge(ttl_days)
    if report.get("deleted"):
        log.info("media purge: удалено %s файлов, освобождено %s Б",
                 report["deleted"], report.get("freed_bytes"))
    return report


async def reclaim_stuck(ctx) -> dict:
    """Cron: вернуть в работу публикации с истёкшей арендой (упавшие воркеры)."""
    rows = store.reclaim_expired()
    if rows:
        log.warning("reclaim: возвращено в очередь %s публикаций", len(rows))
    return {"reclaimed": len(rows),
            "publications": [{"id": r.get("id"), "status": r.get("status")} for r in rows]}


async def sync_inbox(ctx, channel_account_id: str | None = None,
                     vk_owner: str | None = None, tg_chat: str | None = None) -> dict:
    """Синк Инбокса: VK-комментарии/сообщения, Telegram getUpdates, очередь вебхуков MAX.

    Дедупликация — по ``external id`` события (``store.upsert_inbox``), PII маскируется
    на входе. Возвращает фактический отчёт, а не ``{"synced": 0}``.
    """
    from . import sync as sync_workers

    log.info("inbox sync: %s", channel_account_id or "все подключённые каналы")
    report = await asyncio.to_thread(sync_workers.run_once, vk_owner=vk_owner, tg_chat=tg_chat)
    return {"synced": report["inbox"]["stored"], "pii_masked": report["inbox"]["pii_masked"],
            "channels": report["channels"]}


async def collect_metrics(ctx) -> dict:
    """Снять охваты/реакции/переходы и записать снимок в аналитику."""
    from . import sync as sync_workers

    report = await asyncio.to_thread(sync_workers.run_once)
    collected = sum(1 for ch in report["channels"].values() if ch.get("ok"))
    return {"collected": collected, "channels": report["channels"],
            "metrics": store.channel_metrics_tail()}
