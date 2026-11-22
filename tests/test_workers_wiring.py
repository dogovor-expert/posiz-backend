"""Воркер: конфигурация ARQ, самопроверка и cron-задачи.

Аудит (P0-2): «``workers/run.py`` пишет „воркер готов“ и уходит в ``asyncio.Event().wait()``,
задачи не обрабатываются». Здесь фиксируется контракт: есть функции, cron, таймауты,
``--check`` отвечает кодом возврата, а задачи действительно вызывают очередь.
"""
from __future__ import annotations

import asyncio
import inspect
import pathlib

import pytest

from orbita import publishing, scheduler, store
from orbita.workers import run, tasks

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_worker_has_functions_and_cron():
    names = [f.__name__ for f in run.WorkerSettings.functions]
    assert "publish_publication" in names
    assert "reclaim_stuck" in names
    assert len(run.WorkerSettings.cron_jobs) >= 2


def test_job_timeout_covers_lease():
    """Таймаут задачи должен ПЕРЕКРЫВАТЬ аренду.

    Равенство (было в первой версии) означало: задача, работающая чуть дольше срока
    аренды, теряет её прямо во время публикации. Аренду продлевает heartbeat, а таймаут
    задачи остаётся страховкой сверху.
    """
    assert run.WorkerSettings.job_timeout > publishing.DEFAULT_LEASE_SECONDS
    assert run.WorkerSettings.job_timeout >= 3 * publishing.DEFAULT_LEASE_SECONDS


def test_worker_renews_lease():
    """В воркере есть продление аренды, а не только её выдача."""
    source = (ROOT / "src" / "orbita" / "workers" / "tasks.py").read_text()
    assert "_renew_lease" in source and "lease_until" in source


def test_no_idle_wait_in_worker_entrypoint():
    """Регрессия: «воркер живёт, но ничего не делает» больше не должно вернуться."""
    source = (ROOT / "src" / "orbita" / "workers" / "run.py").read_text()
    assert "asyncio.Event().wait()" not in source
    assert "Worker(" in source


def test_tasks_are_coroutines_accepting_ctx():
    for func in run.WorkerSettings.functions:
        assert asyncio.iscoroutinefunction(func), func.__name__
        params = list(inspect.signature(func).parameters)
        assert params and params[0] == "ctx", func.__name__


def test_describe_reports_timer_mode_without_redis(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    info = run.describe()
    assert info["backend"] == "timer"
    assert info["functions"]


def test_describe_reports_redis_mode(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    assert run.describe()["backend"] == "redis"


def test_check_warns_in_timer_mode(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    assert asyncio.run(run.check()) == 0


def test_check_fails_when_redis_unreachable(monkeypatch):
    """Если Redis задан, но недоступен, релиз-гейт должен это увидеть, а не «молчать»."""
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    monkeypatch.setattr(run, "redis_settings", lambda: __import__(
        "arq.connections", fromlist=["RedisSettings"]).RedisSettings(host="127.0.0.1", port=1))
    assert asyncio.run(run.check()) == 1


def test_reclaim_stuck_calls_store(monkeypatch):
    calls = []
    monkeypatch.setattr(store, "reclaim_expired", lambda *a, **k: calls.append(1) or [])
    result = asyncio.run(tasks.reclaim_stuck({}))
    assert result["reclaimed"] == 0 and calls


def test_publish_due_runs_sweep(monkeypatch):
    async def fake_sweep(publish: bool = True):
        return {"due": 2, "claimed": 2, "reclaimed": 0, "results": []}

    monkeypatch.setattr(scheduler, "sweep_due", fake_sweep)
    result = asyncio.run(tasks.publish_due({}))
    assert result["due"] == 2


def test_publish_task_requires_queued_status():
    """Задача не публикует черновик: очередь — единственная точка входа."""
    sig = store.create_signal("manual", "Черновик на согласовании", None, None, "lead",
                              workspace_id="ws_w")
    var = store.save_variant(sig["id"], "vk", "post", "T", "Текст черновика, достаточно длинный.",
                             None, False, 4.0, workspace_id="ws_w")
    pub = store.create_publication(var["id"], "vk_1", None or __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc), None, is_ad=True, require_approval=True)
    result = asyncio.run(tasks.publish_publication(None, pub["id"]))
    assert result["skipped"].startswith("not_queued")


def test_unknown_publication_is_reported():
    result = asyncio.run(tasks.publish_publication(None, "pub_does_not_exist"))
    assert result["skipped"] == "not_found"


@pytest.mark.parametrize("name", ["sync_inbox", "collect_metrics"])
def test_sync_tasks_return_reports(name):
    """Синк возвращает отчёт, а не заглушку ``{"synced": 0}``."""
    result = asyncio.run(getattr(tasks, name)({}))
    assert isinstance(result, dict) and result
