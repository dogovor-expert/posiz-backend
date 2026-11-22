"""Publishing Engine: явная машина состояний, lease и идемпотентность.

Проблема, которую решает модуль (P0 из аудита): публикация могла «зависнуть» в статусе
``publishing`` навсегда, два процесса могли взять одну и ту же задачу, а ручной
``publish-now`` шёл мимо атомарного захвата. Здесь это закрыто формально:

* **Переходы заданы таблицей** ``ALLOWED``: любое изменение статуса, которого нет в таблице,
  отклоняется (``TransitionError``), а не «просто пишется в базу».
* **Захват — это аренда (lease).** ``publishing`` живёт только до ``lease_until``;
  если воркер упал, ``reclaim_expired()`` вернёт публикацию в ``retry_wait``,
  и она снова попадёт в выборку. Зависших задач больше нет по построению.
* **Каждый путь публикации начинается с захвата.** ``publish_now``, ARQ-задача и cron-sweep
  идут через одну и ту же функцию, поэтому дублей при параллельном запуске не возникает.
* **Ретрай отделён от падения.** ``retry_wait`` с ``next_attempt_at`` отличается от
  ``failed``: временная ошибка канала — это «попробуем позже», а не «сломалось навсегда».
* **Reconciliation.** Перед отправкой проверяется ``external_id``: если внешний пост уже
  создан, повтор не выполняется (at-least-once + сверка вместо «ровно один раз любой ценой»).

Таблица состояний::

    draft ──approve──▶ queued ──claim──▶ publishing ──ok──▶ published
                          ▲                  │
                          │                  ├─retryable──▶ retry_wait ──claim──▶ …
                          │                  ├─auth───────▶ needs_reauth
                          │                  └─fatal──────▶ failed
                          └── cancell ── cancelled        blocked_by_compliance
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

STATES = (
    "draft", "queued", "publishing", "retry_wait", "published",
    "failed", "needs_reauth", "blocked_by_compliance", "cancelled",
)

#: Разрешённые переходы. Всё, чего здесь нет, — ошибка кода, а не «данные такие».
ALLOWED: dict[str, set[str]] = {
    "draft": {"queued", "cancelled", "blocked_by_compliance"},
    "queued": {"publishing", "cancelled", "blocked_by_compliance", "draft"},
    "publishing": {"published", "retry_wait", "failed", "needs_reauth", "cancelled"},
    "retry_wait": {"publishing", "failed", "cancelled", "needs_reauth"},
    "published": set(),                       # финальный статус: правок нет
    "failed": {"queued", "cancelled"},        # ручной «повторить»
    "needs_reauth": {"queued", "cancelled"},  # после переавторизации канала
    "blocked_by_compliance": {"draft", "cancelled"},
    "cancelled": set(),
}

#: Статусы, из которых публикация готова к захвату воркером.
CLAIMABLE = {"queued", "retry_wait"}

DEFAULT_LEASE_SECONDS = 120         # = job_timeout ARQ; дольше — значит воркер упал
MAX_ATTEMPTS = 5
BACKOFF_BASE = 30                   # секунды, дальше экспонента с потолком
BACKOFF_CAP = 3600


class TransitionError(RuntimeError):
    """Попытка перевести публикацию в недопустимый статус."""

    def __init__(self, current: str, target: str):
        super().__init__(f"переход {current} → {target} запрещён машиной состояний")
        self.current = current
        self.target = target


def now() -> datetime:
    return datetime.now(timezone.utc)


def can_transition(current: str, target: str) -> bool:
    return target in ALLOWED.get(current, set())


def ensure_transition(current: str, target: str) -> None:
    if not can_transition(current, target):
        raise TransitionError(current, target)


def backoff_seconds(attempt: int) -> int:
    """Экспоненциальная задержка повтора: 30 c → 60 → 120 → 240 … но не больше часа."""
    attempt = max(1, int(attempt))
    return min(BACKOFF_BASE * (2 ** (attempt - 1)), BACKOFF_CAP)


def retry_at(attempt: int, moment: datetime | None = None) -> datetime:
    return (moment or now()) + timedelta(seconds=backoff_seconds(attempt))


def lease_until(seconds: int = DEFAULT_LEASE_SECONDS, moment: datetime | None = None) -> datetime:
    return (moment or now()) + timedelta(seconds=max(5, seconds))


@dataclass(frozen=True)
class Decision:
    """Что делать с публикацией после попытки. Возвращается чистой функцией — легко тестировать."""

    status: str
    error: str | None = None
    next_attempt_at: datetime | None = None
    notify_reauth: bool = False


def after_error(kind: str, message: str, attempt: int,
                moment: datetime | None = None) -> Decision:
    """Решение по классу ошибки канала (``NormalizedError.kind``).

    * ``rate_limit`` / ``transient`` — повтор с backoff, пока не кончатся попытки;
    * ``auth`` — нужна переавторизация канала (не ретраим молча);
    * ``policy`` / ``fatal`` — окончательный провал, повтор бессмысленен.
    """
    moment = moment or now()
    retryable = kind in {"rate_limit", "transient"}
    if kind == "auth":
        return Decision(status="needs_reauth", error=message, notify_reauth=True)
    if not retryable:
        return Decision(status="failed", error=message)
    if attempt >= MAX_ATTEMPTS:
        return Decision(status="failed", error=f"{message} (попыток: {attempt})")
    return Decision(status="retry_wait", error=message, next_attempt_at=retry_at(attempt, moment))


def after_lease_expired(attempt: int, moment: datetime | None = None) -> Decision:
    """Воркер умер, не закрыв публикацию: возвращаем в очередь, если попытки остались."""
    moment = moment or now()
    if attempt >= MAX_ATTEMPTS:
        return Decision(status="failed",
                        error=f"аренда истекла, попыток исчерпано ({attempt})")
    return Decision(status="retry_wait", error="аренда истекла: воркер не подтвердил результат",
                    next_attempt_at=moment)


def describe() -> dict:
    """Карта состояний — для документации, /health и тестов."""
    return {"states": list(STATES), "transitions": {k: sorted(v) for k, v in ALLOWED.items()},
            "claimable": sorted(CLAIMABLE), "max_attempts": MAX_ATTEMPTS,
            "lease_seconds": DEFAULT_LEASE_SECONDS,
            "backoff": [backoff_seconds(i) for i in range(1, MAX_ATTEMPTS + 1)]}
