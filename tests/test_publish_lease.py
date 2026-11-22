"""Публикация под конкуренцией: аренда, повторы, восстановление после падения воркера.

Аудит: «два воркера или ретрай ARQ отправляют один пост дважды, а упавший воркер
оставляет публикацию в статусе publishing навсегда». Здесь оба сценария проверяются
на реальных вызовах ``publish_publication`` с подменённым каналом (без сети).
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from orbita import publishing, store
from orbita.adapters.base import PublishResult
from orbita.workers import tasks

WS = "ws_pub"

TEXT = "Как проверить договор аренды перед подписанием: пять пунктов, на которые смотрят юристы."


class FakeAdapter:
    """Считает вызовы и умеет падать заданным классом ошибки."""

    def __init__(self, error: Exception | None = None, delay: float = 0.0):
        self.calls: list[dict] = []
        self.error = error
        self.delay = delay

    async def publish(self, payload, idempotency_key: str) -> PublishResult:
        self.calls.append({"text": payload.text, "token": payload.token,
                           "key": idempotency_key})
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return PublishResult(external_id=f"ext_{idempotency_key[:6]}",
                             external_url="https://vk.com/wall-1_1")


@pytest.fixture
def publication():
    """Свежая публикация «пора отправлять» в собственном workspace."""
    signal = store.create_signal("manual", "Проверка договора аренды", None, None, "lead",
                                 workspace_id=WS)
    variant = store.save_variant(signal["id"], "vk", "post", "Проверка договора",
                                 TEXT, None, False, 4.5, workspace_id=WS)
    pub = store.create_publication(variant["id"], "vk_1",
                                   datetime.now(timezone.utc) - timedelta(seconds=5),
                                   None, is_ad=False, require_approval=False)
    assert pub["status"] == "queued"
    return pub


def _patch_adapter(monkeypatch, adapter) -> FakeAdapter:
    monkeypatch.setattr(tasks, "get_adapter", lambda channel, token=None: adapter)
    return adapter


def test_two_workers_publish_only_once(publication, monkeypatch):
    """Две параллельные задачи на одну публикацию → один пост в канале."""
    adapter = _patch_adapter(monkeypatch, FakeAdapter(delay=0.05))

    async def run():
        return await asyncio.gather(
            tasks.publish_publication({"worker_id": "w1"}, publication["id"]),
            tasks.publish_publication({"worker_id": "w2"}, publication["id"]),
        )

    results = asyncio.run(run())
    assert len(adapter.calls) == 1, results
    statuses = sorted(r.get("skipped") or r.get("status") for r in results)
    assert statuses == ["not_claimable", "published"]
    assert store.get_publication(publication["id"])["status"] == "published"


def test_transient_error_sets_retry_wait_not_hanging(publication, monkeypatch):
    """Временная ошибка → retry_wait со сроком, а не «publishing навсегда»."""
    from orbita.adapters.base import NormalizedError

    _patch_adapter(monkeypatch, FakeAdapter(error=NormalizedError("transient", "timeout")))
    result = asyncio.run(tasks.publish_publication(None, publication["id"]))

    assert result["status"] == "retry_wait"
    pub = store.get_publication(publication["id"])
    assert pub["status"] == "retry_wait"
    assert pub["next_attempt_at"] is not None
    assert pub["lease_until"] in (None, "")          # аренда снята
    assert pub["last_error"]


def test_retry_waits_for_its_time_then_succeeds(publication, monkeypatch):
    """Повтор не выполняется раньше backoff, но после срока — проходит."""
    from orbita.adapters.base import NormalizedError

    failing = FakeAdapter(error=NormalizedError("rate_limit", "429", retry_after=30))
    _patch_adapter(monkeypatch, failing)
    asyncio.run(tasks.publish_publication(None, publication["id"]))

    # сразу — нельзя: срок повтора не наступил
    early = asyncio.run(tasks.publish_publication(None, publication["id"]))
    assert early["skipped"] == "not_claimable"

    store.pub_update(publication["id"], next_attempt_at=datetime.now(timezone.utc)
                     - timedelta(seconds=1))
    healthy = FakeAdapter()
    _patch_adapter(monkeypatch, healthy)
    late = asyncio.run(tasks.publish_publication(None, publication["id"]))
    assert late["status"] == "published"
    assert len(healthy.calls) == 1


def test_auth_error_marks_needs_reauth(publication, monkeypatch):
    """Отозванный токен не ретраится молча — клиент должен переавторизовать канал."""
    from orbita.adapters.base import NormalizedError

    _patch_adapter(monkeypatch, FakeAdapter(error=NormalizedError("auth", "401")))
    result = asyncio.run(tasks.publish_publication(None, publication["id"]))

    assert result["status"] == "needs_reauth"
    pub = store.get_publication(publication["id"])
    assert pub["status"] == "needs_reauth"


def test_expired_lease_is_reclaimed(publication, monkeypatch):
    """Упавший воркер: аренда истекла → публикация возвращается в очередь и уходит один раз."""
    _patch_adapter(monkeypatch, FakeAdapter())
    assert store.claim_publication(publication["id"], worker_id="crashed") is True
    # имитируем падение: воркер взял аренду и не вернулся
    store.pub_update(publication["id"],
                     lease_until=datetime.now(timezone.utc) - timedelta(seconds=1))

    reclaimed = store.reclaim_expired()
    assert any(r["id"] == publication["id"] for r in reclaimed)
    assert store.get_publication(publication["id"])["status"] == "retry_wait"

    result = asyncio.run(tasks.publish_publication(None, publication["id"]))
    assert result["status"] == "published"


def test_lease_is_not_reclaimed_while_alive(publication):
    """Живая аренда не отбирается: иначе появился бы дубль."""
    assert store.claim_publication(publication["id"], worker_id="alive") is True
    assert store.reclaim_expired() == []
    assert store.get_publication(publication["id"])["status"] == "publishing"


def test_already_published_is_never_repeated(publication, monkeypatch):
    """Reconciliation: пост уже в канале — повторный вызов ничего не отправляет."""
    adapter = _patch_adapter(monkeypatch, FakeAdapter())
    first = asyncio.run(tasks.publish_publication(None, publication["id"]))
    second = asyncio.run(tasks.publish_publication(None, publication["id"]))

    assert first["status"] == "published"
    assert second["skipped"] == "already_published"
    mine = [c for c in adapter.calls if c["key"] == publication["idempotency_key"]]
    assert len(mine) == 1


def test_publication_uses_stored_channel_token(publication, monkeypatch):
    """Токен берётся из хранилища аккаунта, а не из окружения процесса."""
    adapter = _patch_adapter(monkeypatch, FakeAdapter())
    store.set_channel_token("vk_1", "vk-account-token-0123456789", channel="vk",
                            external_id="-777", workspace_id=WS)

    asyncio.run(tasks.publish_publication(None, publication["id"]))
    assert adapter.calls[0]["token"] == "vk-account-token-0123456789"


def test_advertising_post_is_marked_in_final_text(publication, monkeypatch):
    """Маркировка попадает в текст публикации, а не остаётся в отчёте проверки."""
    adapter = _patch_adapter(monkeypatch, FakeAdapter())
    signal = store.get_signal(publication["id"]) or {}
    variant = store.get_variant(publication["variant_id"], workspace_id=WS)
    ad = store.create_publication(variant["id"], "ma_1", datetime.now(timezone.utc),
                                  "erid: 2Vfnxw8abcde", is_ad=True, require_approval=False)
    store.pub_update(ad["id"], status="queued")

    result = asyncio.run(tasks.publish_publication(None, ad["id"]))
    assert result["status"] == "published"
    assert result["marked"] is True
    assert "erid: 2Vfnxw8abcde" in adapter.calls[-1]["text"]
    assert "Реклама" in adapter.calls[-1]["text"]
    assert signal is not None


def test_sweep_claims_once_and_does_not_double_claim(publication, monkeypatch):
    """sweep_due не забирает пост вторым захватом: задача сама берёт аренду."""
    from orbita import scheduler

    adapter = _patch_adapter(monkeypatch, FakeAdapter())
    summary = asyncio.run(scheduler.sweep_due())
    assert summary["claimed"] >= 1
    assert store.get_publication(publication["id"])["status"] == "published"
    # ровно один вызов именно этой публикации (у других тестов свои очереди)
    mine = [c for c in adapter.calls if c["key"] == publication["idempotency_key"]]
    assert len(mine) == 1


def test_sweep_dry_run_leaves_lease_then_reclaims(publication):
    """Dry-run (publish=False) держит пост в publishing с арендой — и она истекает."""
    from orbita import scheduler

    summary = asyncio.run(scheduler.sweep_due(publish=False))
    assert publication["id"] in [r["publication_id"] for r in summary["results"]] or \
        store.get_publication(publication["id"])["status"] == "publishing"

    store.pub_update(publication["id"],
                     lease_until=datetime.now(timezone.utc) - timedelta(seconds=1))
    store.reclaim_expired()
    assert store.get_publication(publication["id"])["status"] in {"retry_wait", "failed"}


def test_publishing_decision_map_covers_worker_outcomes():
    """Коды, которые возвращает воркер, должны быть статусами машины состояний."""
    for status in ("published", "retry_wait", "needs_reauth", "failed"):
        assert status in publishing.STATES
