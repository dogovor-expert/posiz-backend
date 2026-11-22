"""Хранилище каркаса: память или PostgreSQL — за одним интерфейсом.

Слой выбирается переменной ``DATA_LAYER`` (``memory`` по умолчанию, ``db`` при заданном
``DATABASE_URL``). Критичное для бизнеса состояние — короткие ссылки и клики, публикации
и отложенная очередь, счётчики квот, журнал аудита, токены каналов — при ``db`` уходит
в :mod:`orbita.persistence` и переживает перезапуск контейнера и второй воркер.
В памяти остаются только демо-объекты, которых нет в живом контуре (сигналы, варианты).

Токены каналов шифруются всегда (AES-256-GCM, :mod:`orbita.crypto`), даже в памяти:
открытый секрет не должен попадать ни в дамп, ни в лог.
"""
from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from . import crypto, tenancy
from .persistence import database

_lock = threading.Lock()


def _db():
    """Активное подключение к БД или None (память)."""
    return database()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uid(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def _as_aware(value) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if isinstance(value, datetime) and value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


CHANNELS: dict[str, dict[str, Any]] = {
    "ma_1": {
        "id": "ma_1", "channel": "max", "title": "dogovor.expert · MAX",
        "audience_size": 41200, "status": "active",
        "capabilities": {"publish": True, "media": ["image", "video", "document"],
                         "edit": False, "buttons": True, "markdown": "plain-safe"},
        "health": {"last_ok_at": _now().isoformat(), "error_rate_24h": 0.002, "delivery_risk": "low"},
        "note": "Bot API platform-api.max.ru · бесплатно · бот верифицирован",
    },
    "vk_1": {
        "id": "vk_1", "channel": "vk", "title": "Договор Эксперт · VK",
        "audience_size": 28400, "status": "active",
        "capabilities": {"publish": True, "media": ["image", "video", "carousel"],
                         "edit": False, "buttons": False, "markdown": "limited"},
        "health": {"last_ok_at": _now().isoformat(), "error_rate_24h": 0.004, "delivery_risk": "low"},
        "note": "токен сообщества + service key; write через wall.post",
    },
    "tg_1": {
        "id": "tg_1", "channel": "telegram", "title": "@dogovor_expert",
        "audience_size": 31900, "status": "active",
        "capabilities": {"publish": True, "media": ["image", "video", "document", "album"],
                         "edit": True, "buttons": True, "markdown": "markdown_v2"},
        "health": {"last_ok_at": _now().isoformat(), "error_rate_24h": 0.031, "delivery_risk": "high"},
        "note": "РКН: замедление с 10.02.2026 · повышенный риск доставки",
    },
    "ok_1": {
        "id": "ok_1", "channel": "ok", "title": "Договор Эксперт · ОК",
        "audience_size": 8600, "status": "active",
        "capabilities": {"publish": True, "media": ["image"], "edit": False, "buttons": False},
        "health": {"last_ok_at": _now().isoformat(), "error_rate_24h": 0.008, "delivery_risk": "medium"},
        "note": "ограниченный набор методов, строгая модерация",
    },
    "x_1": {
        "id": "x_1", "channel": "x", "title": "@dogovor_expert",
        "audience_size": 5100, "status": "active",
        "capabilities": {"publish": True, "media": ["image"], "edit": True, "buttons": False},
        "health": {"last_ok_at": _now().isoformat(), "error_rate_24h": 0.0, "delivery_risk": "low"},
        "note": "pay-per-use: $0,015/пост, $0,20 со ссылкой в тексте",
    },
}

SIGNALS: dict[str, dict[str, Any]] = {}
VARIANTS: dict[str, dict[str, Any]] = {}
PUBLICATIONS: dict[str, dict[str, Any]] = {}
IDEMPOTENCY: dict[str, str] = {}          # idempotency_key -> publication_id
METRICS: list[dict[str, Any]] = []
INBOX: list[dict[str, Any]] = []          # нормализованные события Инбокса (см. workers.sync)
INBOX_INDEX: dict[str, dict[str, Any]] = {}
CHANNEL_METRICS: dict[str, dict[str, Any]] = {}


def upsert_inbox(items: list[dict[str, Any]]) -> int:
    """Дедупликация по id события: повторный синк не плодит дубли."""
    stored = 0
    for item in items:
        key = item["id"]
        if key in INBOX_INDEX:
            INBOX_INDEX[key].update({k: v for k, v in item.items() if k != "status"})
            continue
        INBOX_INDEX[key] = item
        INBOX.append(item)
        stored += 1
    return stored


def inbox_tail(limit: int = 50, masked: bool = True) -> list[dict[str, Any]]:
    rows = sorted(INBOX, key=lambda i: i.get("created_at", ""), reverse=True)[:limit]
    return [{**r, "text": r["text_masked"] if masked else r["text"]} for r in rows]


def update_metrics(channel: str, totals: dict[str, Any]) -> dict[str, Any]:
    """Снимок метрик канала от воркера (охваты/реакции/комментарии)."""
    import datetime as _dt

    snapshot = {"channel": channel, **totals,
                "collected_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")}
    CHANNEL_METRICS[channel] = snapshot
    return snapshot


def channel_metrics_tail() -> list[dict[str, Any]]:
    return list(CHANNEL_METRICS.values())

# ---------- v2: ссылки, ОРД, аудит, одобрения ----------
LINKS: dict[str, dict[str, Any]] = {}                 # code -> link
CLICKS: list[dict[str, Any]] = []                     # журнал переходов (analytics_events)
ORD_SUBMISSIONS: dict[str, dict[str, Any]] = {}       # erid -> заявка (ord_report_queue)
CHANNEL_TOKENS: dict[str, str] = {}                   # account_id -> шифротекст (AES-256-GCM)
AUDIT_LOG: list[dict[str, Any]] = []                  # кто что сделал: роль, путь, статус
APPROVALS: list[dict[str, Any]] = []                  # подписи под рекламные размещения


# ---------- документная персистентность сигналов и вариантов ----------
# Раньше сигналы и варианты жили только в памяти процесса: API создавал вариант,
# клал задачу в Redis, а воркер в другом процессе не находил его и публикация падала.
# Теперь при DATA_LAYER=db они пишутся в entity_docs (ADR 0014) и читаются любым
# процессом; память остаётся кэшем для демо-режима.
SIGNAL_COLLECTION = "signals"
VARIANT_COLLECTION = "variants"


def _doc_put(collection: str, doc_id: str, workspace_id: str, payload: dict) -> dict:
    db = _db()
    if db is not None:
        db.doc_put(collection, doc_id, workspace_id, payload)
        # и в типизированную таблицу: на неё ссылаются publications/variants (FK) и её
        # закрывает RLS; без этой записи публикация падала на publications_variant_id_fkey
        if collection == SIGNAL_COLLECTION:
            db.signal_upsert(payload)
        elif collection == VARIANT_COLLECTION:
            db.variant_upsert(payload)
    return payload


def _doc_get(collection: str, doc_id: str, workspace_id: str | None = None) -> dict | None:
    """Прочитать документ с сужением по контексту арендатора.

    Раньше при `workspace_id=None` проверка пропускала всё: `ContextVar` появился, но
    путь чтения из памяти его игнорировал — в режиме памяти клиент видел чужие сигналы
    (воспроизведено вторым аудитом). Теперь «не передан» = «взять из контекста».
    """
    scope = workspace_id or tenancy.scope_workspace()
    db = _db()
    if db is not None:
        return db.doc_get(collection, doc_id, workspace_id=scope)
    doc = (SIGNALS if collection == SIGNAL_COLLECTION else VARIANTS).get(doc_id)
    if doc is not None and scope is not None and doc.get("workspace_id") != scope:
        return None
    return doc


# ---------- сигналы ----------
def create_signal(kind: str, title: str, source_url: str | None, raw_text: str | None, goal: str,
                  workspace_id: str = "ws_de") -> dict:
    with _lock:
        sid = _uid("sig_")
        doc = {
            "id": sid, "kind": kind, "title": title, "source_url": source_url,
            "raw_text": raw_text, "goal": goal, "status": "new", "created_at": _now(),
            "workspace_id": workspace_id,
        }
        SIGNALS[sid] = doc
    return _doc_put(SIGNAL_COLLECTION, sid, workspace_id, doc)


def get_signal(sid: str, workspace_id: str | None = None) -> dict | None:
    return _doc_get(SIGNAL_COLLECTION, sid, workspace_id)


def list_signals(workspace_id: str | None = None, limit: int = 100) -> list[dict]:
    """Инфоповоды рабочего пространства. Без ``workspace_id`` берётся контекст арендатора."""
    workspace_id = workspace_id or tenancy.scope_workspace()
    db = _db()
    if db is not None:
        rows = db.doc_all(SIGNAL_COLLECTION, limit=limit, workspace_id=workspace_id)
    else:
        rows = list(SIGNALS.values())
        if workspace_id:
            rows = [r for r in rows if r.get("workspace_id") == workspace_id]
    return sorted(rows, key=lambda r: r.get("created_at") or "", reverse=True)[:limit]


# ---------- варианты ----------
def save_variant(signal_id: str, channel: str, fmt: str, title: str, body: str,
                 link_url: str | None, link_in_reply: bool, score: float,
                 workspace_id: str = "ws_de") -> dict:
    with _lock:
        vid = _uid("var_")
        doc = {
            "id": vid, "signal_id": signal_id, "channel": channel, "format": fmt,
            "title": title, "body": body, "link_url": link_url,
            "link_in_reply": link_in_reply, "review_score": score,
            "char_count": len(body), "workspace_id": workspace_id,
        }
        VARIANTS[vid] = doc
    return _doc_put(VARIANT_COLLECTION, vid, workspace_id, doc)


def get_variant(vid: str, workspace_id: str | None = None) -> dict | None:
    """Вариант по id. Воркер читает его из БД: вариант создан в другом процессе."""
    if not vid:
        return None
    return _doc_get(VARIANT_COLLECTION, str(vid), workspace_id)


# ---------- публикации ----------
def estimate_cost(channel: str, has_link: bool, link_in_reply: bool) -> float:
    """Стоимость доставки в ₽ (курс условный: $1 = 80 ₽)."""
    if channel == "x":
        return 0.20 * 80 if (has_link and not link_in_reply) else 0.015 * 80
    return 0.0


def create_publication(variant_id: str, channel_account_id: str, scheduled_at: datetime,
                       erid: str | None, is_ad: bool = False,
                       require_approval: bool = True,
                       media_ids: list[str] | None = None) -> dict:
    # Читаем через слои-геттеры, а не из словарей памяти: при DATA_LAYER=db вариант
    # и аккаунт живут в БД, и прямое обращение к VARIANTS/CHANNELS падало KeyError
    # (тот самый разрыв слоёв, который нашёл аудит).
    variant = get_variant(variant_id)
    if variant is None:
        raise ValueError(f"вариант {variant_id} не найден")
    account = get_channel_account(channel_account_id)
    if account is None:
        raise ValueError(f"аккаунт канала {channel_account_id} не найден")
    idem = f"{variant_id}:{channel_account_id}"
    db = _db()
    if db is not None:
        if (existing := db.pub_by_idempotency(idem)) is not None:
            return existing
        pid = _uid("pub_")
        has_link = bool(variant.get("link_url"))
        return db.pub_create({
            "id": pid, "variant_id": variant_id, "channel_account_id": channel_account_id,
            "workspace_id": (variant.get("workspace_id") or tenancy.scope_workspace()
                             or "ws_de"),
            "channel": account["channel"], "scheduled_at": scheduled_at,
            "status": "draft" if (is_ad or require_approval) else "queued",
            "idempotency_key": idem, "erid": erid, "is_ad": is_ad,
            "cost_estimate_rub": estimate_cost(account["channel"], has_link,
                                               variant["link_in_reply"]),
            "media_ids": list(media_ids or []),
        })
    with _lock:
        if idem in IDEMPOTENCY:                     # идемпотентность: не создаём дубль
            return PUBLICATIONS[IDEMPOTENCY[idem]]
        pid = _uid("pub_")
        has_link = bool(variant.get("link_url"))
        pub = {
            "id": pid, "variant_id": variant_id, "channel_account_id": channel_account_id,
            # workspace обязателен: без него публикация «не принадлежит никому» и,
            # после включения изоляции, не находится ни в одном контексте
            "workspace_id": (variant.get("workspace_id") or tenancy.scope_workspace()
                             or "ws_de"),
            "channel": account["channel"], "scheduled_at": scheduled_at,
            "status": "queued", "idempotency_key": idem, "erid": erid,
            "cost_estimate_rub": estimate_cost(account["channel"], has_link, variant["link_in_reply"]),
            "is_ad": is_ad,
            "media_ids": list(media_ids or []),
            # реклама и режим «сначала согласование» стартуют в draft: очередь только после подписи
            "status": "draft" if (is_ad or require_approval) else "queued",
            "approved_by_role": None, "approved_at": None,
        }
        PUBLICATIONS[pid] = pub
        IDEMPOTENCY[idem] = pid
        return pub


def get_publication(pid: str) -> dict | None:
    """Публикация. В контексте арендатора чужая публикация = «не найдена»."""
    scope = tenancy.scope_workspace()
    if (db := _db()) is not None:
        pub = db.pub_get(pid)
        return pub if pub and (scope is None or pub.get("workspace_id") == scope) else None
    pub = PUBLICATIONS.get(pid)
    if pub is not None and scope is not None and pub.get("workspace_id") != scope:
        return None
    return pub


def due_publications(now: datetime | None = None, limit: int = 50) -> list[dict]:
    """Пора отправлять: ``queued`` по расписанию и ``retry_wait`` с наступившим сроком повтора."""
    if (db := _db()) is not None:
        return db.due_publications(now, limit)
    moment = now or _now()
    with _lock:
        rows = [
            p for p in PUBLICATIONS.values()
            if p["status"] in ("queued", "retry_wait")
            and _as_aware(p["scheduled_at"]) <= moment
            and (p.get("next_attempt_at") is None or _as_aware(p["next_attempt_at"]) <= moment)
        ]
    return sorted(rows, key=lambda p: _as_aware(p["scheduled_at"]))[:limit]


def claim_publication(pid: str, worker_id: str | None = None,
                      lease_seconds: int = 120) -> bool:
    """Атомарно забрать публикацию в работу и выдать аренду.

    Захват — единственный вход в публикацию: и ARQ-задача, и cron-sweep, и ручной
    «опубликовать сейчас» идут через него, поэтому два процесса не возьмут один пост.
    """
    from .publishing import lease_until

    if (db := _db()) is not None:
        return db.claim_publication(pid, worker_id=worker_id, lease_seconds=lease_seconds)
    with _lock:
        pub = PUBLICATIONS.get(pid)
        if pub is None or pub["status"] not in ("queued", "retry_wait"):
            return False
        moment = _now()
        due = pub.get("next_attempt_at")
        if due is not None and _as_aware(due) > moment:
            return False
        pub["status"] = "publishing"
        pub["attempt_count"] = pub.get("attempt_count", 0) + 1
        pub["claimed_by"] = worker_id
        pub["lease_until"] = lease_until(lease_seconds, moment)
        pub["next_attempt_at"] = None
        return True


def _guard_transition(pid: str, target: str | None, *, force: bool = False) -> dict | None:
    """Не дать сменить статус в обход машины состояний.

    Раньше `pub_update()`, `mark_*()` и `approve_publication()` меняли статус напрямую,
    поэтому таблица `ALLOWED` существовала, но никого не ограничивала: из `published`
    можно было вернуться в `queued` и отправить пост повторно.
    """
    from . import publishing

    pub = get_publication(pid)
    if pub is None:
        return None
    current = pub.get("status")
    if target is None or force or target == current:
        return pub
    if not publishing.can_transition(current, target):
        raise publishing.TransitionError(str(current), target)
    return pub


def pub_update(pid: str, *, force: bool = False, **fields: Any) -> dict | None:
    """Обновить поля публикации в активном слое (используется тестами и воркером).

    Набор разрешённых полей ограничен слоем БД (``persistence.pub_update``): нельзя
    «подкрутить» статус произвольным полем в обход машины состояний.
    """
    _guard_transition(pid, fields.get("status"), force=force)
    if (db := _db()) is not None:
        return db.pub_update(pid, **fields)
    pub = PUBLICATIONS.get(pid)
    if pub is None:
        return None
    with _lock:
        pub.update(fields)
    return pub


def mark_retry(pid: str, error: str, next_attempt_at: datetime) -> dict | None:
    """Временная ошибка: публикация уходит в ``retry_wait``, а не «зависает в publishing»."""
    if _guard_transition(pid, "retry_wait") is None:
        return None
    if (db := _db()) is not None:
        return db.pub_update(pid, status="retry_wait", last_error=error,
                             next_attempt_at=next_attempt_at, lease_until=None,
                             claimed_by=None)
    pub = PUBLICATIONS.get(pid)
    if pub is not None:
        pub.update(status="retry_wait", last_error=error,
                   next_attempt_at=next_attempt_at, lease_until=None, claimed_by=None)
    return pub


# ---------- журнал попыток (защита от дубля после сетевого сбоя) ----------
#: Попытка, начатая и не закрытая: ``{publication_id: {...}}``. В БД — таблица
#: ``publication_attempts`` с уникальным индексом на незакрытую попытку.
ATTEMPTS: dict[str, dict] = {}


def attempt_begin(pid: str, attempt: int, worker_id: str | None, idempotency_key: str,
                  workspace_id: str | None = None) -> dict:
    """Зафиксировать намерение опубликовать **до** обращения к площадке.

    Порядок важен: если воркер упадёт между успешным ответом площадки и записью
    ``external_id``, попытка останется незакрытой, и следующая задача узнает, что
    публикация могла состояться — вместо слепого повтора (риск двух постов).
    """
    if (db := _db()) is not None:
        return db.attempt_begin(pid, attempt, worker_id, idempotency_key,
                                workspace_id or tenancy.scope_workspace())
    row = {"publication_id": pid, "attempt": attempt, "worker_id": worker_id,
           "idempotency_key": idempotency_key, "started_at": _now(), "finished_at": None,
           "outcome": None, "external_id": None, "error": None,
           "workspace_id": workspace_id or tenancy.scope_workspace() or "ws_de"}
    ATTEMPTS[pid] = row
    return row


def attempt_finish(pid: str, outcome: str, external_id: str | None = None,
                   error: str | None = None) -> dict | None:
    if (db := _db()) is not None:
        return db.attempt_finish(pid, outcome, external_id=external_id, error=error)
    row = ATTEMPTS.get(pid)
    if row is None:
        return None
    row.update(finished_at=_now(), outcome=outcome, external_id=external_id, error=error)
    return row


def attempt_last(pid: str) -> dict | None:
    """Последняя попытка публикации — включая незакрытую (признак «возможно, ушло»)."""
    if (db := _db()) is not None:
        return db.attempt_last(pid)
    return ATTEMPTS.get(pid)


def pub_stats() -> dict:
    """Счётчики очереди публикаций (для /metrics и релиз-гейта)."""
    if (db := _db()) is not None:
        return db.pub_stats()
    moment = _now()
    statuses = ("queued", "draft", "publishing", "retry_wait", "published", "failed")
    stats = {status: 0 for status in statuses}
    stats.update({"due_now": 0, "stale_leases": 0, "oldest_due_age_seconds": 0})
    oldest = None
    with _lock:
        for pub in PUBLICATIONS.values():
            status = pub.get("status")
            if status in stats:
                stats[status] += 1
            scheduled = pub.get("scheduled_at")
            next_at = pub.get("next_attempt_at")
            if status in ("queued", "retry_wait") and scheduled is not None and scheduled <= moment:
                if next_at is None or next_at <= moment:
                    stats["due_now"] += 1
                    oldest = scheduled if oldest is None else min(oldest, scheduled)
            if (status == "publishing" and pub.get("lease_until") is not None
                    and pub["lease_until"] < moment):
                stats["stale_leases"] += 1
    if oldest is not None:
        stats["oldest_due_age_seconds"] = int((moment - oldest).total_seconds())
    return stats


def defer_publication(pid: str, *, retry_in: int, reason: str = "channel_rate_limited") -> bool:
    """Отложить публикацию из-за лимита площадки (не считая это попыткой)."""
    from datetime import timedelta

    if (db := _db()) is not None:
        return db.pub_defer(pid, seconds=retry_in, reason=reason)
    with _lock:
        pub = PUBLICATIONS.get(pid)
        if pub is None or pub.get("status") != "publishing":
            return False
        pub["status"] = "queued"
        pub["lease_until"] = None
        pub["claimed_by"] = None
        pub["last_error"] = reason
        pub["next_attempt_at"] = _now() + timedelta(seconds=max(1, int(retry_in)))
            # захват уже увеличил счётчик, но обращения к площадке не было
        pub["attempt_count"] = max(0, int(pub.get("attempt_count") or 1) - 1)
        return True


def reclaim_expired(limit: int = 100) -> list[dict]:
    """Вернуть в работу публикации с истёкшей арендой (упавший воркер)."""
    from .publishing import after_lease_expired

    if (db := _db()) is not None:
        return db.reclaim_expired(limit)
    moment = _now()
    out: list[dict] = []
    with _lock:
        for pub in PUBLICATIONS.values():
            lease = pub.get("lease_until")
            if pub.get("status") != "publishing" or lease is None:
                continue
            if _as_aware(lease) >= moment:
                continue
            decision = after_lease_expired(pub.get("attempt_count", 1), moment)
            pub.update(status=decision.status, last_error=decision.error,
                       next_attempt_at=decision.next_attempt_at, lease_until=None,
                       claimed_by=None)
            out.append(pub)
            if len(out) >= limit:
                break
    return out


def schedule_publication(pid: str, when: datetime) -> dict | None:
    if (db := _db()) is not None:
        return db.pub_update(pid, scheduled_at=when, status="queued")
    pub = PUBLICATIONS.get(pid)
    if pub is not None:
        pub["scheduled_at"] = when
        pub["status"] = "queued"
    return pub


def recent_publications(limit: int = 20, workspace_id: str | None = None) -> list[dict]:
    """Последние публикации — источник для RSS-ленты Дзена."""
    rows = [p for p in PUBLICATIONS.values()
            if workspace_id in (None, p.get("workspace_id"))
            and p.get("status", "published") == "published"]
    rows.sort(key=lambda p: str(p.get("scheduled_at") or p.get("id")), reverse=True)
    return rows[:limit]


def mark_published(pid: str, external_id: str, external_url: str) -> dict:
    if _guard_transition(pid, "published") is None:
        raise KeyError(pid)
    if (db := _db()) is not None:
        return db.pub_update(pid, status="published", external_id=external_id,
                             external_url=external_url, published_at=_now()) or {}
    pub = PUBLICATIONS[pid]
    pub.update(status="published", external_id=external_id, external_url=external_url)
    METRICS.append({"publication_id": pid, "channel": pub["channel"], "impressions": 0,
                    "reactions": 0, "clicks": 0, "cost_rub": pub["cost_estimate_rub"]})
    return pub


def mark_failed(pid: str, error: str, status: str = "failed", *, force: bool = False) -> dict:
    _guard_transition(pid, status, force=force)
    if (db := _db()) is not None:
        return db.pub_update(pid, status=status, last_error=error) or {}
    pub = PUBLICATIONS[pid]
    pub.update(status=status, last_error=error)
    return pub


# ---------- короткие ссылки ----------
def save_link(link: dict) -> dict:
    if (db := _db()) is not None:
        saved = db.link_create(link)
        LINKS[link["code"]] = saved
        return saved
    with _lock:
        LINKS[link["code"]] = link
        return link


def get_link(code: str) -> dict | None:
    if (db := _db()) is not None:
        return db.link_get(code)
    return LINKS.get(code)


def list_links() -> list[dict]:
    if (db := _db()) is not None:
        return db.links_list()
    return sorted(LINKS.values(), key=lambda x: x["created_at"], reverse=True)


def add_click(event: dict) -> dict:
    if (db := _db()) is not None:
        return db.link_add_click(event)
    with _lock:
        CLICKS.append(event)
        link = LINKS.get(event["code"])
        if link:
            link["clicks"] = link.get("clicks", 0) + 1
        return event


def clicks_for(code: str) -> list[dict]:
    return [c for c in CLICKS if c["code"] == code]


def link_clicks_total() -> int:
    if (db := _db()) is not None:
        return db.link_clicks_total()
    return len(CLICKS)


# ---------- ОРД ----------
def save_ord_submission(result) -> dict:
    with _lock:
        row = {
            "erid": result.erid, "provider": result.provider, "status": result.status,
            "creative_id": result.creative_id, "submitted_at": result.submitted_at,
            "marking_block": result.marking_block, "note": result.note,
            "reported_to_erir": False,
        }
        ORD_SUBMISSIONS[result.erid] = row
        return row


def get_ord_submission(erid: str) -> dict | None:
    return ORD_SUBMISSIONS.get(erid)


def list_ord_submissions() -> list[dict]:
    return sorted(ORD_SUBMISSIONS.values(), key=lambda x: x["submitted_at"], reverse=True)


# ---------- аудит ----------
def audit_tail(limit: int = 50) -> dict:
    """Хвост журнала решений: в памяти — из списка, в БД — из таблицы audit_log."""
    if (db := _db()) is not None:
        return db.audit_tail(limit)
    return {"items": AUDIT_LOG[-limit:], "total": len(AUDIT_LOG)}


def audit(actor_role: str, method: str, path: str, status: int, extra: dict | None = None) -> dict:
    if (db := _db()) is not None:
        return db.audit_write(actor_role, method, path, status, extra)
    with _lock:
        row = {"ts": _now(), "role": actor_role, "method": method, "path": path,
               "status": status, **(extra or {})}
        AUDIT_LOG.append(row)
        if len(AUDIT_LOG) > 2000:
            del AUDIT_LOG[:500]
        return row


# ---------- одобрение рекламы ----------
def approve_publication(pid: str, role: str, note: str | None = None) -> dict:
    if (db := _db()) is not None:
        APPROVALS.append({"publication_id": pid, "role": role, "note": note, "ts": _now()})
        return db.pub_update(pid, status="queued", approved_by_role=role,
                             approved_at=_now()) or {}
    pub = PUBLICATIONS[pid]
    with _lock:
        pub.update(status="queued", approved_by_role=role, approved_at=_now())
        APPROVALS.append({"publication_id": pid, "role": role, "note": note, "ts": _now()})
        return pub


# ---------- каналы, варианты, сигналы: доступ через функции, а не через словари ----------
def list_channel_accounts() -> list[dict]:
    if (db := _db()) is not None:
        rows = db.channel_accounts()
        if rows:
            return rows
    return list(CHANNELS.values())


def get_channel_account(account_id: str) -> dict | None:
    """Аккаунт канала. В контексте арендатора чужой аккаунт не отдаётся."""
    scope = tenancy.scope_workspace()
    if (db := _db()) is not None:
        for row in db.channel_accounts():
            if row["id"] == account_id:
                return row if scope is None or row.get("workspace_id") == scope else None
    account = CHANNELS.get(account_id)
    if account is not None and scope is not None and account.get("workspace_id") not in (None, scope):
        return None
    return account


def set_channel_token(account_id: str, token: str, *, channel: str = "max",
                      external_id: str | None = None,
                      workspace_id: str | None = None) -> dict:
    """Сохранить токен канала: в БД — шифротекст, в памяти — тоже шифротекст.

    ``external_id`` (chat_id, owner_id сообщества, channel_id) обязателен по схеме:
    без него воркер не может адресовать публикацию и запись в ``channel_accounts``
    падает на NOT NULL. Берём из аккаунта, если не передан явно.
    """
    account = get_channel_account(account_id) or {}
    ext = str(external_id or account.get("external_id") or "") or None
    workspace = workspace_id or account.get("workspace_id") or "ws_de"
    if (db := _db()) is not None:
        db.channel_token_save(account_id, token, channel=channel, workspace_id=workspace,
                              external_id=ext)
        return {"id": account_id, "token_hint": crypto.fingerprint(token),
                "external_id": ext}
    CHANNEL_TOKENS[account_id] = crypto.encrypt(token)      # никогда не открытый текст
    return {"id": account_id, "token_hint": crypto.fingerprint(token),
            "external_id": ext}


def get_channel_token(account_id: str) -> str | None:
    if (db := _db()) is not None:
        return db.channel_token_get(account_id)
    ciphertext = CHANNEL_TOKENS.get(account_id)
    return crypto.decrypt(ciphertext) if ciphertext else None


def channel_token_hint(account_id: str) -> str:
    token = get_channel_token(account_id)
    return crypto.fingerprint(token) if token else ""


def update_variant(vid: str, workspace_id: str | None = None, **fields: Any) -> dict | None:
    """Обновить вариант и сохранить результат в активном слое.

    Раньше обновление меняло только объект в памяти: `facts`, `compliance`, `citations`
    и `issues` после прогона роя терялись, а в режиме БД расхождение было ещё заметнее.
    """
    scope = workspace_id or tenancy.scope_workspace()
    variant = get_variant(vid, workspace_id=scope)
    if variant is None:
        return None
    variant.update(fields)
    _doc_put(VARIANT_COLLECTION, vid, variant.get("workspace_id") or scope or "ws_de", variant)
    return variant


def set_signal_status(sid: str, status: str, workspace_id: str | None = None) -> dict | None:
    """Сменить статус инфоповода и сохранить его в активном слое (раньше — только память)."""
    scope = workspace_id or tenancy.scope_workspace()
    signal = get_signal(sid, workspace_id=scope)
    if signal is None:
        return None
    signal["status"] = status
    _doc_put(SIGNAL_COLLECTION, sid, signal.get("workspace_id") or scope or "ws_de", signal)
    return signal


def analytics_summary() -> dict:
    by_channel: dict[str, dict] = {}
    for m in METRICS:
        c = by_channel.setdefault(m["channel"], {"channel": m["channel"], "impressions": 0, "clicks": 0})
        c["impressions"] += m["impressions"]
        c["clicks"] += m["clicks"]
    return {
        "impressions": sum(m["impressions"] for m in METRICS),
        "reactions": sum(m["reactions"] for m in METRICS),
        "clicks": sum(m["clicks"] for m in METRICS) + link_clicks_total(),
        "cost_rub": round(sum(m["cost_rub"] for m in METRICS), 2),
        "by_channel": list(by_channel.values()),
    }
