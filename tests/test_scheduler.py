"""Отложенная очередь: созревшие публикации выходят сами, дублей нет.

Redis в тестах не поднимается — проверяется «деградированный», но рабочий путь:
sweep находит публикации со `scheduled_at <= now`, атомарно забирает их и публикует
через dry-run адаптеры. Именно этот путь раньше отсутствовал: пост с датой в будущем
создавался и висел в статусе `queued` навсегда.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from orbita import scheduler, store


def _prepare(scheduled_at: datetime) -> str:
    signal = store.create_signal("manual", "Отложенный пост про залог", None, None, "lead")
    variant = store.save_variant(signal["id"], "vk", "post", "Заголовок",
                                 "Текст поста про залог и договор аренды, достаточно длинный.",
                                 None, False, 4.5)
    pub = store.create_publication(variant["id"], "vk_1", scheduled_at, None,
                                   is_ad=False, require_approval=False)
    return pub["id"]


def test_future_publication_is_not_swept():
    pid = _prepare(datetime.now(timezone.utc) + timedelta(hours=2))
    summary = asyncio.run(scheduler.sweep_due())
    # проверяем именно эту публикацию: хранилище общее на прогон, и в очереди могут
    # лежать публикации других тестов
    assert pid not in [r["publication_id"] for r in summary["results"]]
    assert store.get_publication(pid)["status"] == "queued"


def test_due_publication_is_published_by_sweep():
    pid = _prepare(datetime.now(timezone.utc) - timedelta(seconds=5))
    summary = asyncio.run(scheduler.sweep_due())
    assert summary["due"] >= 1
    assert summary["claimed"] >= 1
    pub = store.get_publication(pid)
    assert pub["status"] == "published", pub
    assert pub["external_id"]


def test_claim_is_exclusive():
    pid = _prepare(datetime.now(timezone.utc) - timedelta(seconds=1))
    assert store.claim_publication(pid) is True
    assert store.claim_publication(pid) is False, "второй воркер не должен взять тот же пост"


def test_dry_run_sweep_claims_without_publishing():
    pid = _prepare(datetime.now(timezone.utc) - timedelta(seconds=1))
    summary = asyncio.run(scheduler.sweep_due(publish=False))
    assert summary["claimed"] >= 1
    assert store.get_publication(pid)["status"] == "publishing"


def test_defer_seconds_never_negative():
    assert scheduler.defer_seconds(datetime.now(timezone.utc) - timedelta(days=1)) == 0
    ahead = scheduler.defer_seconds(datetime.now(timezone.utc) + timedelta(minutes=5))
    assert 290 < ahead <= 300


def test_schedule_falls_back_to_sweep_without_redis(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    scheduler.reset_pool()
    pid = _prepare(datetime.now(timezone.utc) + timedelta(minutes=1))
    result = asyncio.run(scheduler.schedule_publication(pid, datetime.now(timezone.utc) + timedelta(minutes=1)))
    assert result["queued"] == "sweep"
    assert result["delay_seconds"] > 0


def test_publish_task_skips_draft_publications():
    signal = store.create_signal("manual", "Черновик", None, None, "lead")
    variant = store.save_variant(signal["id"], "vk", "post", "T", "Тело поста для проверки.", None,
                                 False, 4.0)
    pub = store.create_publication(variant["id"], "vk_1", datetime.now(timezone.utc),
                                   None, is_ad=True, require_approval=True)
    assert pub["status"] == "draft"
    from orbita.workers.tasks import publish_publication

    result = asyncio.run(publish_publication(None, pub["id"]))
    assert result["skipped"].startswith("not_queued")
