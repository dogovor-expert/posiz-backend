"""Машина состояний публикации: запрещённые переходы, backoff, истечение аренды.

Аудит утверждал, что публикации «могут зависнуть в publishing навсегда» и «могут уйти
дважды». Эти тесты фиксируют правила, которые закрывают оба случая, — и падают, если
правила ослабят.
"""
from datetime import datetime, timedelta, timezone

import pytest

from orbita import publishing


def test_every_status_has_transitions():
    """Нет статуса «в никуда»: из любого либо есть переходы, либо он финальный."""
    for state in publishing.STATES:
        assert state in publishing.ALLOWED, f"статус {state} не описан в ALLOWED"
    assert publishing.ALLOWED["published"] == set()
    assert publishing.ALLOWED["cancelled"] == set()


def test_published_is_terminal():
    """Опубликованное нельзя вернуть в очередь: это защита от повторной отправки в канал."""
    for target in publishing.STATES:
        assert not publishing.can_transition("published", target)


@pytest.mark.parametrize("current,target", [
    ("draft", "published"),            # нельзя опубликовать черновик мимо очереди и проверки
    ("draft", "publishing"),
    ("queued", "published"),           # нельзя проскочить состояние работы
    ("cancelled", "queued"),
    ("blocked_by_compliance", "published"),
    ("published", "retry_wait"),
])
def test_forbidden_transitions(current, target):
    assert not publishing.can_transition(current, target)
    with pytest.raises(publishing.TransitionError):
        publishing.ensure_transition(current, target)


@pytest.mark.parametrize("current,target", [
    ("draft", "queued"),
    ("queued", "publishing"),
    ("publishing", "published"),
    ("publishing", "retry_wait"),
    ("retry_wait", "publishing"),
    ("failed", "queued"),              # ручной «повторить»
])
def test_allowed_transitions(current, target):
    publishing.ensure_transition(current, target)


def test_backoff_grows_and_is_capped():
    delays = [publishing.backoff_seconds(i) for i in range(1, 8)]
    assert delays[0] == 30
    assert delays == sorted(delays)
    assert delays[1] == 2 * delays[0]
    assert max(delays) <= publishing.BACKOFF_CAP
    assert publishing.backoff_seconds(50) == publishing.BACKOFF_CAP


def test_transient_error_schedules_retry():
    moment = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)
    decision = publishing.after_error("transient", "таймаут", attempt=2, moment=moment)
    assert decision.status == "retry_wait"
    assert decision.next_attempt_at == moment + timedelta(seconds=60)


def test_retries_exhausted_becomes_failed():
    decision = publishing.after_error("rate_limit", "429", attempt=publishing.MAX_ATTEMPTS)
    assert decision.status == "failed"
    assert str(publishing.MAX_ATTEMPTS) in (decision.error or "")


def test_auth_error_requires_reauth_not_retry():
    """Токен отозван клиентом: молчаливые повторы бессмысленны, нужна переавторизация."""
    decision = publishing.after_error("auth", "401", attempt=1)
    assert decision.status == "needs_reauth"
    assert decision.notify_reauth is True
    assert decision.next_attempt_at is None


@pytest.mark.parametrize("kind", ["policy", "fatal", "unknown_kind"])
def test_non_retryable_errors_fail_fast(kind):
    """Модерация/неизвестная ошибка не должны крутить бесконечные повторы."""
    assert publishing.after_error(kind, "x", attempt=1).status == "failed"


def test_lease_is_time_boxed():
    moment = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)
    lease = publishing.lease_until(60, moment)
    assert lease == moment + timedelta(seconds=60)


def test_expired_lease_returns_to_queue():
    decision = publishing.after_lease_expired(attempt=2)
    assert decision.status == "retry_wait"
    assert decision.next_attempt_at is not None       # сразу, без backoff: это не ошибка канала


def test_expired_lease_at_max_attempts_fails():
    decision = publishing.after_lease_expired(publishing.MAX_ATTEMPTS)
    assert decision.status == "failed"


def test_describe_matches_rules():
    map_ = publishing.describe()
    assert set(map_["states"]) == set(publishing.STATES)
    assert "publishing" not in map_["claimable"]
    assert map_["lease_seconds"] == publishing.DEFAULT_LEASE_SECONDS
