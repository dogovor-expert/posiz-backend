"""Планировщик отложенных публикаций: ARQ + Redis, с честным фолбэком.

Проблема, которую решает модуль: ``scheduled_at`` в карточке поста — это обещание клиенту.
Ранее воркер умел публиковать «сейчас», но никто не будил его в нужную минуту, поэтому
отложенный пост не выходил сам по себе.

Как устроено:

1. ``schedule_publication(pub_id, when)`` кладёт задачу в ARQ с ``_defer_by`` (Redis),
   если Redis доступен. Тогда публикация проснётся точно по времени.
2. ``sweep_due()`` — страховка и режим без Redis: выбирает из БД/памяти публикации
   со статусом ``queued`` и ``scheduled_at <= now``, атомарно забирает каждую
   (``claim_publication``: ``UPDATE … WHERE status='queued'``) и публикует. Два воркера
   не возьмут один пост: проигравший получит ``False`` и пропустит.
3. Если Redis нет вообще — ``run.py`` крутит ``sweep_due`` по таймеру. Это медленнее
   (гранулярность = интервал), но отложенные публикации выходят, а не «висят вечно».
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from . import store
from .config import settings

log = logging.getLogger("orbita.scheduler")

_pool = None
_pool_checked = False


def _now() -> datetime:
    return datetime.now(timezone.utc)


def defer_seconds(when: datetime) -> float:
    """Сколько секунд ждать до ``when`` (не меньше нуля)."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - _now()).total_seconds())


async def get_pool():
    """Пул ARQ (Redis). ``None``, если Redis не настроен или недоступен."""
    global _pool, _pool_checked
    if _pool_checked:
        return _pool
    _pool_checked = True
    if not settings.redis_url:
        log.warning("REDIS_URL не задан: отложенные публикации идут через sweep-таймер")
        return None
    try:
        from urllib.parse import urlparse

        from arq import create_pool
        from arq.connections import RedisSettings

        parsed = urlparse(settings.redis_url)
        _pool = await create_pool(RedisSettings(
            host=parsed.hostname or "localhost", port=parsed.port or 6379,
            database=int((parsed.path or "/0").strip("/") or 0),
        ))
        log.info("arq pool: %s", settings.redis_url)
    except Exception as exc:                            # noqa: BLE001 — Redis опционален
        log.warning("ARQ недоступен (%s): отложенные публикации через sweep", exc)
        _pool = None
    return _pool


def reset_pool() -> None:
    global _pool, _pool_checked
    _pool, _pool_checked = None, False


async def schedule_publication(publication_id: str, when: datetime) -> dict:
    """Запланировать публикацию: ARQ с задержкой или пометка «сделает sweep»."""
    delay = defer_seconds(when)
    pool = await get_pool()
    if pool is not None:
        try:
            job = await pool.enqueue_job("publish_publication", publication_id, _defer_by=delay)
            return {"queued": "arq", "publication_id": publication_id,
                    "delay_seconds": round(delay, 1),
                    "job_id": getattr(job, "job_id", None)}
        except Exception as exc:                        # noqa: BLE001
            log.warning("ARQ enqueue failed (%s): fallback на sweep", exc)
    return {"queued": "sweep", "publication_id": publication_id, "delay_seconds": round(delay, 1)}


async def sweep_due(limit: int = 50, publish: bool = True) -> dict:
    """Забрать созревшие публикации и опубликовать. Возвращает сводку."""
    due = store.due_publications(_now(), limit)
    claimed: list[str] = []
    published: list[dict] = []

    reclaimed = store.reclaim_expired()                 # вернуть то, что бросил упавший воркер
    for pub in due:
        pid = pub["id"]
        # Захват делает сама задача публикации: sweep не должен забирать пост дважды —
        # иначе задача увидит status=publishing и откажется работать.
        if not publish:
            if not store.claim_publication(pid, worker_id="sweep-dry"):
                continue
            claimed.append(pid)
            continue
        from .workers.tasks import publish_publication

        try:
            result = await publish_publication(None, pid)
        except Exception as exc:                        # noqa: BLE001 — at-least-once
            log.warning("publish failed %s: %s", pid, exc)
            result = {"status": "retry_scheduled", "error": str(exc)}
        if result.get("skipped") in {"not_claimable", "already_published", "not_found"}:
            continue                                    # пост забрал другой воркер или уже вышел
        claimed.append(pid)
        published.append({"publication_id": pid, **result})

    return {"due": len(due), "claimed": len(claimed), "reclaimed": len(reclaimed),
            "results": published}


async def run_forever(interval: int = 20) -> None:      # pragma: no cover — процесс
    """Режим без Redis: таймер, который каждые ``interval`` секунд ищет созревшее."""
    log.info("sweep-таймер запущен: интервал %s c", interval)
    while True:
        try:
            await sweep_due()
        except Exception as exc:                        # noqa: BLE001 — цикл не должен падать
            log.exception("sweep error: %s", exc)
        await asyncio.sleep(interval)
