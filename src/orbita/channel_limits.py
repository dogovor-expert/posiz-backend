"""Лимиты частоты публикаций на канал (token bucket).

Зачем отдельно от ``ratelimit``: тот защищает **входящий** API от клиента, а здесь —
исходящие вызовы площадок. У VK, Telegram, MAX и Дзена свои ограничения на число
постов в час, и превышение — это не «502 на один пост», а блокировка сообщества или
приложения на стороне платформы. До этой итерации движок публикации отправлял столько,
сколько созрело: на 300 постов в очереди площадка ответила бы ограничением, а мы бы
ушли в цикл ``retry_wait`` и создали себе шторм повторов.

Модель: **token bucket** на ключ ``(workspace, channel_account)``. Ёмкость — сколько
постов можно отправить «пачкой», пополнение — с постоянной скоростью. Состояние живёт
в Redis (общее для всех воркеров), без Redis — в памяти процесса (один воркер, тесты).

Если ведро пусто, публикация не отправляется и не считается неудачной: воркер возвращает
``deferred``, публикация остаётся в очереди до момента следующего токена. То есть лимит
площадки не превращается в ошибку публикации.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass

#: Профили площадок: (ёмкость, секунд на пополнение одного токена).
#: Значения сознательно консервативнее опубликованных лимитов: цель — не выжать
#: максимум, а не получить блокировку сообщества.
PROFILES: dict[str, tuple[int, int]] = {
    "vk": (12, 60),          # VK: мягкий предел ~1 пост/мин на сообщество
    "telegram": (20, 30),    # Telegram: 20 сообщений/мин в чат
    "max": (12, 60),
    "dzen": (4, 300),
    "x": (10, 120),
    "instagram": (8, 300),
    "default": (10, 60),
}

_PREFIX = "chanrl"


class ChannelLimited(RuntimeError):
    """Ведро пусто: отправку нужно отложить, а не помечать ошибкой."""

    def __init__(self, key: str, retry_after: int) -> None:
        super().__init__(f"лимит канала исчерпан: {key}, повтор через {retry_after} с")
        self.key = key
        self.retry_after = retry_after


@dataclass
class Budget:
    allowed: bool
    remaining: float
    retry_after: int


_lock = threading.Lock()
_memory: dict[str, tuple[float, float]] = {}       # ключ → (токены, отметка времени)


def enabled() -> bool:
    """Лимиты можно выключить только явно (тесты, локальные прогоны)."""
    return os.getenv("CHANNEL_LIMITS_ENABLED", "true").lower() not in {"0", "false", "no"}


def profile(channel: str) -> tuple[int, int]:
    return PROFILES.get((channel or "").lower(), PROFILES["default"])


def _key(workspace: str | None, account: str | None, channel: str) -> str:
    return f"{_PREFIX}:{workspace or '-'}:{account or '-'}:{channel or '-'}"


def _redis():
    try:
        from .ratelimit import limiter            # переиспользуем один клиент Redis

        return limiter._redis if getattr(limiter, "_redis", None) is not None else None
    except Exception:                             # noqa: BLE001 — Redis необязателен
        return None


def _take_memory(key: str, capacity: int, refill: int, cost: int = 1) -> Budget:
    now = time.monotonic()
    with _lock:
        tokens, stamp = _memory.get(key, (float(capacity), now))
        tokens = min(capacity, tokens + (now - stamp) / refill)
        if tokens >= cost:
            _memory[key] = (tokens - cost, now)
            return Budget(True, tokens - cost, 0)
        _memory[key] = (tokens, now)
        wait = int((cost - tokens) * refill) + 1
        return Budget(False, tokens, wait)


def _take_redis(store, key: str, capacity: int, refill: int, cost: int = 1) -> Budget:
    """Атомарное списание в Redis: скрипт, чтобы два воркера не «одолжили» один токен."""
    script = """
    local capacity = tonumber(ARGV[1])
    local refill = tonumber(ARGV[2])
    local cost = tonumber(ARGV[3])
    local now = tonumber(ARGV[4])
    local data = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
    local tokens = tonumber(data[1])
    local ts = tonumber(data[2])
    if tokens == nil then tokens = capacity end
    if ts == nil then ts = now end
    tokens = math.min(capacity, tokens + (now - ts) / refill)
    local allowed = 0
    local wait = 0
    if tokens >= cost then
      tokens = tokens - cost
      allowed = 1
    else
      wait = math.ceil((cost - tokens) * refill)
    end
    redis.call('HSET', KEYS[1], 'tokens', tostring(tokens), 'ts', tostring(now))
    redis.call('EXPIRE', KEYS[1], math.ceil(refill * capacity) + 60)
    return {allowed, tostring(tokens), wait}
    """
    allowed, tokens, wait = store.eval(script, 1, key, capacity, refill, cost, time.time())
    return Budget(bool(int(allowed)), float(tokens), int(wait))


def take(channel: str, *, workspace_id: str | None = None,
         account_id: str | None = None, cost: int = 1) -> Budget:
    """Попытаться занять токен(ы) на отправку. Не бросает — возвращает решение."""
    if not enabled():
        return Budget(True, float(profile(channel)[0]), 0)
    capacity, refill = profile(channel)
    key = _key(workspace_id, account_id, channel)
    store = _redis()
    if store is not None:
        try:
            return _take_redis(store, key, capacity, refill, cost)
        except Exception:                         # noqa: BLE001 — Redis упал, не блокируем работу
            return _take_memory(key, capacity, refill, cost)
    return _take_memory(key, capacity, refill, cost)


def reserve(channel: str, *, workspace_id: str | None = None,
            account_id: str | None = None, cost: int = 1) -> None:
    """Занять токен или поднять :class:`ChannelLimited` (для путей, где нужен отказ)."""
    budget = take(channel, workspace_id=workspace_id, account_id=account_id, cost=cost)
    if not budget.allowed:
        raise ChannelLimited(f"{channel}:{account_id}", budget.retry_after)


def describe() -> dict:
    return {
        "enabled": enabled(),
        "backend": "redis" if _redis() is not None else "in-process",
        "profiles": {ch: {"capacity": cap, "refill_seconds": ref}
                     for ch, (cap, ref) in PROFILES.items()},
    }
