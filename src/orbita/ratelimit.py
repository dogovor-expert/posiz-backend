"""Rate limiting для публичных эндпоинтов.

Без него открытые ``/l/{code}``, ``/bio/{slug}`` и ``/webhooks/comments/{channel}``
позволяют накручивать клики в аналитике и засыпать очередь автоответов.

Счётчики — в Redis (``INCR`` + ``EXPIRE``, фиксированное окно), чтобы лимит был общим
для всех инстансов API. Если Redis не настроен или недоступен, ограничитель честно
переключается на локальный счётчик процесса и пишет предупреждение — публичный эндпоинт
не остаётся вообще без защиты, но лимит действует на инстанс.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from fastapi import HTTPException, Request

from .config import rate_limit_enabled, rate_limit_trust_proxy, settings

log = logging.getLogger("orbita.ratelimit")

LIMITS: dict[str, tuple[int, int]] = {
    # scope: (запросов, окно в секундах)
    "link_redirect": (60, 60),
    "bio_page": (120, 60),
    "comment_webhook": (30, 60),
    "public_tool": (30, 60),          # бесплатные SEO-утилиты posiz.ru/tools/*
    "public_default": (120, 60),
    # --- аутентификация: вход, проверка ключей и сессии ---
    # Без лимитов эти маршруты превращались в бесплатный перебор: `/auth/verify`
    # подбирает одноразовый токен, `/keys/verify` — API-ключ (32 символа алфавита
    # base62 дают огромное пространство, но защита не должна держаться на одной энтропии).
    "auth_login": (10, 60),           # magic-link, oauth callback
    "auth_verify": (20, 60),          # verify/refresh/logout по токену
    "auth_2fa": (10, 300),            # подбор кода TOTP
    "key_verify": (20, 60),           # POST /keys/verify
    "webhook": (240, 60),             # приём вебхуков каналов и платежей
}


@dataclass
class Decision:
    allowed: bool
    limit: int
    remaining: int
    retry_after: int


def _mask_url(url: str) -> str:
    """URL без пароля для журнала (пароль в логах — это утечка, а не диагностика)."""
    if not url:
        return "(пусто)"
    try:
        from urllib.parse import urlsplit

        parts = urlsplit(url)
        auth = "***@" if parts.password else ""
        return f"{parts.scheme}://{auth}{parts.hostname or '?'}:{parts.port or 6379}"
    except Exception:                             # noqa: BLE001
        return "(url)"


class RateLimiter:
    def __init__(self) -> None:
        self._redis = None
        self._redis_checked = False
        self._mem: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()

    # ---------- backend ----------
    def _client(self):
        if self._redis_checked:
            return self._redis
        self._redis_checked = True
        url = settings.redis_url
        if not url:
            return None
        try:
            import redis

            client = redis.Redis.from_url(url, socket_timeout=0.2, socket_connect_timeout=0.2)
            client.ping()
            self._redis = client
            log.info("rate limiter: redis %s", _mask_url(url))
        except Exception as exc:                       # noqa: BLE001 — Redis опционален
            log.warning("rate limiter: redis недоступен (%s), локальный счётчик", exc)
            self._redis = None
        return self._redis

    def reset(self) -> None:
        with self._lock:
            self._mem.clear()
        self._redis_checked = False
        self._redis = None

    # ---------- core ----------
    def hit(self, scope: str, key: str, limit: int, window: int) -> Decision:
        bucket = f"rl:{scope}:{key}"
        client = self._client()
        if client is not None:
            try:
                pipe = client.pipeline()
                pipe.incr(bucket)
                pipe.ttl(bucket)
                count, ttl = pipe.execute()
                count = int(count)
                if ttl is None or int(ttl) < 0:
                    client.expire(bucket, window)
                    ttl = window
                ttl = int(ttl)
                return Decision(count <= limit, limit, max(0, limit - count), max(ttl, 1))
            except Exception as exc:                   # noqa: BLE001 — Redis упал в рантайме
                log.warning("rate limiter: redis error (%s), fallback", exc)
                self._redis = None

        now = time.monotonic()
        with self._lock:
            count, expires = self._mem.get(bucket, (0, now + window))
            if expires <= now:
                count, expires = 0, now + window
            count += 1
            self._mem[bucket] = (count, expires)
            if len(self._mem) > 10_000:                # защита от роста памяти
                for k, (_, exp) in list(self._mem.items()):
                    if exp <= now:
                        self._mem.pop(k, None)
        return Decision(count <= limit, limit, max(0, limit - count), max(1, int(expires - now)))


limiter = RateLimiter()


def client_key(request: Request) -> str:
    """IP клиента. ``X-Forwarded-For`` — только за доверенным прокси."""
    if rate_limit_trust_proxy():
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def enforce(scope: str, request: Request, *, limit: int | None = None,
            window: int | None = None) -> Decision:
    default_limit, default_window = LIMITS.get(scope, LIMITS["public_default"])
    decision = limiter.hit(scope, client_key(request),
                           limit or default_limit, window or default_window)
    if not decision.allowed:
        raise HTTPException(
            429,
            detail={"kind": "rate_limited", "scope": scope,
                    "message": "Слишком много запросов, повторите позже.",
                    "retry_after": decision.retry_after},
            headers={"Retry-After": str(decision.retry_after),
                     "X-RateLimit-Limit": str(decision.limit),
                     "X-RateLimit-Remaining": "0"},
        )
    return decision


def rate_limit(scope: str):
    """Зависимость FastAPI: ``Depends(rate_limit('link_redirect'))``."""

    def dependency(request: Request) -> None:
        if not rate_limit_enabled():
            return
        enforce(scope, request)

    dependency.__name__ = f"rate_limit_{scope}"
    return dependency


def describe() -> dict:
    return {
        "enabled": rate_limit_enabled(),
        "backend": "redis" if limiter._redis is not None else "in-process",
        "limits": {scope: {"requests": lim, "window_seconds": win}
                   for scope, (lim, win) in LIMITS.items()},
    }
