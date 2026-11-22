"""Метрики эксплуатации: очередь публикаций, лимиты, зависимости.

Зачем свои, а не «есть /health». Health отвечает на вопрос «жив ли процесс», а дежурному
нужны другие:

* сколько публикаций созрело и ещё не отправлено (``due_now``) и как давно самая старая
  ждёт (``oldest_due_age_seconds``) — это и есть «мы отстаём», а не «очередь не пуста»;
* сколько публикаций висит в ``publishing`` с истёкшей арендой (``stale_leases``) —
  признак упавших воркеров;
* сколько уходит в ``retry_wait``/``failed`` — площадка ограничивает или адаптер сломан;
* доступность Redis/LLM/БД — но не как «ok», а как число.

Формат — Prometheus text exposition (``/metrics``). Без внешних библиотек: одна функция
рендеринга, чтобы метрики не зависели от того, установлен ли клиент Prometheus.

Доступ. ``/metrics`` не публичный: если задан ``METRICS_TOKEN``, требуется
``Authorization: Bearer <token>``; если токен не задан, маршрут отдаётся только вне прода
(``APP_ENV != prod``) — иначе 404, чтобы не подтверждать существование эндпоинта.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any

_CACHE: dict[str, Any] = {"at": 0.0, "snapshot": None}
_CACHE_LOCK = threading.Lock()
_CACHE_TTL = 5.0

#: Счётчики процесса: их не отдаёт БД (генерация, дедупликация, лимиты канала).
COUNTERS: dict[str, float] = {}
_COUNTERS_LOCK = threading.Lock()


def inc(name: str, value: float = 1) -> None:
    """Увеличить внутренний счётчик (переживает запросы, не переживает рестарт)."""
    with _COUNTERS_LOCK:
        COUNTERS[name] = COUNTERS.get(name, 0.0) + value


def reset() -> None:
    with _COUNTERS_LOCK:
        COUNTERS.clear()
    with _CACHE_LOCK:
        _CACHE.update({"at": 0.0, "snapshot": None})


def _publishing() -> dict[str, Any]:
    from . import store

    try:
        stats = store.pub_stats()
        stats["source"] = "db" if store._db() is not None else "memory"
        return stats
    except Exception as exc:                      # noqa: BLE001 — метрики не должны падать
        return {"error": exc.__class__.__name__, "source": "unavailable"}


def _dependencies() -> dict[str, Any]:
    from . import llm
    from .config import redis_url
    from .auth import state_backend

    out: dict[str, Any] = {"llm_mode": "live" if llm.enabled() else "simulated",
                           "state_backend": state_backend()}
    url = redis_url()
    if not url:
        out["redis"] = "not_configured"
    else:
        try:
            import redis  # noqa: PLC0415 — необязательная зависимость

            client = redis.Redis.from_url(url, socket_timeout=0.3, socket_connect_timeout=0.3)
            started = time.monotonic()
            client.ping()
            out["redis"] = "ok"
            out["redis_latency_ms"] = round((time.monotonic() - started) * 1000, 1)
        except Exception as exc:                  # noqa: BLE001
            out["redis"] = f"error:{exc.__class__.__name__}"
    return out


def snapshot(*, use_cache: bool = True) -> dict[str, Any]:
    """Полный снимок состояния: очередь, зависимости, лимиты, счётчики процесса."""
    now = time.monotonic()
    if use_cache:
        with _CACHE_LOCK:
            cached = _CACHE["snapshot"]
            if cached is not None and now - _CACHE["at"] < _CACHE_TTL:
                return cached
    data = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "publishing": _publishing(),
        "dependencies": _dependencies(),
        "counters": dict(COUNTERS),
    }
    with _CACHE_LOCK:
        _CACHE.update({"at": now, "snapshot": data})
    return data


def _line(name: str, value: float | int, labels: dict[str, str] | None = None,
          help_text: str = "") -> list[str]:
    out: list[str] = []
    if help_text:
        out.append(f"# HELP {name} {help_text}")
        out.append(f"# TYPE {name} gauge")
    label_text = ""
    if labels:
        pairs = ",".join(f'{key}="{value}"' for key, value in labels.items())
        label_text = f"{{{pairs}}}"
    out.append(f"{name}{label_text} {value}")
    return out


def prometheus() -> str:
    """Отрендерить снимок в формате Prometheus text exposition."""
    data = snapshot()
    lines: list[str] = []
    publishing = data.get("publishing") or {}
    if "error" not in publishing:
        for status in ("queued", "draft", "publishing", "retry_wait", "published", "failed"):
            lines += _line(f"posiz_publications_{status}", publishing.get(status, 0),
                           help_text="Публикации в статусе")
        lines += _line("posiz_publications_due_now", publishing.get("due_now", 0),
                       help_text="Созрели и ещё не отправлены")
        lines += _line("posiz_publications_stale_leases", publishing.get("stale_leases", 0),
                       help_text="В publishing с истёкшей арендой (упавшие воркеры)")
        lines += _line("posiz_publishing_oldest_due_age_seconds",
                       publishing.get("oldest_due_age_seconds", 0),
                       help_text="Сколько секунд самая старая публикация ждёт отправки")
    dependencies = data.get("dependencies") or {}
    lines += _line("posiz_redis_up", 1 if dependencies.get("redis") == "ok" else 0,
                   help_text="Доступность Redis из процесса")
    lines += _line("posiz_llm_live", 1 if dependencies.get("llm_mode") == "live" else 0,
                   help_text="1 — реальные вызовы модели, 0 — офлайн-симуляция")
    for name, value in sorted((data.get("counters") or {}).items()):
        safe = name.replace("-", "_").replace(".", "_")
        lines += _line(f"posiz_{safe}_total", value, help_text="Счётчик процесса")
    return "\n".join(lines) + "\n"


def authorize(headers: dict[str, str]) -> tuple[bool, str]:
    """Разрешить скрейп: токен из ``METRICS_TOKEN`` либо только вне прода."""
    token = os.getenv("METRICS_TOKEN", "").strip()
    if token:
        provided = (headers.get("authorization") or "").removeprefix("Bearer ").strip()
        if provided != token:
            return False, "неверный или отсутствующий METRICS_TOKEN"
        return True, ""
    if os.getenv("APP_ENV", "dev").lower() in {"prod", "production"}:
        return False, "в проде METRICS_TOKEN обязателен: метрики не отдаются анонимно"
    return True, ""
