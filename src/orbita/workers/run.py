"""Запуск воркера.

Что важно после аудита: раньше этот модуль при доступном Redis писал в лог «воркер ARQ
готов» и уходил в бесконечное ожидание — контейнер жил, задачи не обрабатывались.
Теперь ``main()`` создаёт **настоящий** :class:`arq.worker.Worker` с функциями и cron-задачами
из :class:`WorkerSettings` и запускает его цикл.

Режимы:

* ``arq orbita.workers.run.WorkerSettings`` — рекомендованный запуск в проде (так же
  поступает docker-compose);
* ``python -m orbita.workers.run`` — то же самое из Python (создаёт Worker и запускает);
* Redis недоступен → «деградированный» режим: cron внутри процесса
  (:func:`orbita.scheduler.run_forever`), публикации выходят с точностью до интервала;
* ``--once`` — один проход sweep (CI, ручная проверка очереди без брокера);
* ``--check`` — самопроверка: список функций, cron-задач и доступность Redis. Код возврата
  ``1``, если ARQ не сможет работать — это проверяется в release gate.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os

from arq import create_pool, cron
from arq.connections import RedisSettings
from arq.worker import Worker

from .. import store
from ..config import redis_url as _redis_url
from ..publishing import DEFAULT_LEASE_SECONDS
from ..scheduler import run_forever, sweep_due
from .tasks import (collect_metrics, publish_due, publish_publication, purge_media,
                    reclaim_stuck, sync_inbox)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("orbita.worker.run")


def redis_settings() -> RedisSettings | None:
    url = _redis_url("worker")
    if not url:
        return None
    from urllib.parse import urlparse

    parsed = urlparse(url)
    return RedisSettings(host=parsed.hostname or "localhost", port=parsed.port or 6379,
                         database=int((parsed.path or "/0").strip("/") or 0))


async def on_startup(ctx) -> None:
    """Воркер поднялся: фиксируем id и сразу возвращаем «брошенные» публикации в очередь."""
    ctx["worker_id"] = f"worker-{os.getpid()}"
    log.info("воркер %s стартовал; storage=%s", ctx["worker_id"],
             "db" if store.database() is not None else "memory")
    reclaimed = store.reclaim_expired()
    if reclaimed:
        log.warning("на старте возвращено в очередь %s публикаций с истёкшей арендой",
                    len(reclaimed))


async def on_shutdown(ctx) -> None:
    log.info("воркер %s остановлен", ctx.get("worker_id"))


class WorkerSettings:
    """Конфигурация ARQ. Запуск: ``arq orbita.workers.run.WorkerSettings``."""

    functions = [publish_publication, sync_inbox, collect_metrics, publish_due, reclaim_stuck,
                 purge_media]
    cron_jobs = [
        # Страховка: если отложенная задача потерялась (Redis перезапустили),
        # очередь доберётся этим проходом.
        cron(publish_due, second={0, 15, 30, 45}, run_at_startup=True),
        # Аренда: публикации, которые воркер взял и не закрыл, возвращаются в очередь.
        cron(reclaim_stuck, second={5, 35}, run_at_startup=True),
        # Медиа-хранилище: артефакты старше MEDIA_TTL_DAYS (14 дней) удаляются раз в сутки.
        # Час выбран ночной: рендеры в это время редки, а место освобождается к утру.
        cron(purge_media, hour=4, minute=20, run_at_startup=False),
    ]
    max_tries = 5
    # Аренда (120 c) продлевается heartbeat-ом задачи, поэтому таймаут задачи должен быть
    # ЗАМЕТНО больше: иначе леase истекает ровно на границе и пост может быть взят дважды.
    job_timeout = DEFAULT_LEASE_SECONDS * 3
    keep_result = 3600
    on_startup = on_startup
    on_shutdown = on_shutdown
    redis_settings = redis_settings() or RedisSettings()


def describe() -> dict:
    """Что именно умеет воркер — используется в ``--check`` и в ``/health``."""
    return {
        "functions": [f.__name__ for f in WorkerSettings.functions],
        "cron_count": len(WorkerSettings.cron_jobs),
        "job_timeout": WorkerSettings.job_timeout,
        "max_tries": WorkerSettings.max_tries,
        "redis": _redis_url("worker") or None,
        "backend": "redis" if redis_settings() is not None else "timer",
        "storage": "db" if store.database() is not None else "memory",
    }


async def check() -> int:
    """Самопроверка перед запуском: ARQ действительно сможет забрать задачи."""
    info = describe()
    log.info("воркер: %s", info)
    if not info["functions"]:
        log.error("у воркера нет функций: задачи некому выполнять")
        return 1
    if info["backend"] == "timer":
        log.warning("REDIS_URL не задан: очередь работает таймером, ARQ-задачи не доставляются")
        return 0
    try:
        pool = await create_pool(WorkerSettings.redis_settings)
        await pool.ping()
        await pool.close()
    except Exception as exc:                    # noqa: BLE001 — Redis опционален для проверки
        log.error("Redis недоступен (%s): ARQ не сможет принимать задачи", exc)
        return 1
    return 0


async def main(once: bool = False, check_only: bool = False) -> int:
    if check_only:
        return await check()
    if once:
        summary = await sweep_due()
        log.info("sweep once: %s", summary)
        return 0
    if redis_settings() is None:
        log.warning("REDIS_URL не задан — воркер работает таймером (без ARQ)")
        await run_forever(int(os.getenv("SWEEP_INTERVAL", "20")))
        return 0
    worker = Worker(
        functions=WorkerSettings.functions,
        cron_jobs=WorkerSettings.cron_jobs,
        redis_settings=WorkerSettings.redis_settings,
        max_tries=WorkerSettings.max_tries,
        job_timeout=WorkerSettings.job_timeout,
        keep_result=WorkerSettings.keep_result,
        on_startup=WorkerSettings.on_startup,
        on_shutdown=WorkerSettings.on_shutdown,
    )
    log.info("ARQ worker запущен: %s", _mask(os.getenv("REDIS_URL", "")))
    await worker.async_run()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Posiz worker")
    parser.add_argument("--once", action="store_true", help="один проход очереди и выход")
    parser.add_argument("--check", action="store_true", help="проверить конфигурацию и выход")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(once=args.once, check_only=args.check)))
