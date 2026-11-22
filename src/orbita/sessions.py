"""Сессии аутентификации: access + refresh, ротация, отзыв (Redis или память).

Прод-требования, которые здесь закрыты:
* **access** — JWT на 15 минут с ролью, воркспейсом и ``platform_admin``;
* **refresh** — непрозрачный токен, хранится **хэшем** (SHA-256), не в открытом виде;
* **ротация** — каждый refresh одноразовый; повторное использование старого токена
  отзывает всю цепочку (детект кражи);
* **отзыв** — ``revoke_session``/``revoke_all`` при выходе и смене пароля;
* хранилище — Redis (``REDIS_URL``), иначе память процесса с честным статусом в ``/health``.

Ключ в Redis: ``sess:{sha256(refresh)}`` → JSON записи, TTL = срок жизни refresh.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import secrets
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from . import security

log = logging.getLogger("orbita.sessions")

ACCESS_TTL = 900            # 15 минут
REFRESH_TTL = 30 * 86400    # 30 дней
REUSE_WINDOW = 300          # сколько живёт пометка «токен использован» (окно детекта кражи)
#: Память процесса: значение + момент истечения. Без срока (как было раньше) refresh жил
#: вечно, а «выход со всех устройств» отзывал только те токены, что клиент прислал сам.
_MEMORY: dict[str, tuple[str, float]] = {}
_USER_INDEX: dict[str, set[str]] = {}      # user_id → {ключи refresh-токенов}
_LOCK = threading.RLock()                  # критическая секция ротации в памяти процесса


def _redis():
    from .config import redis_url as _redis_url

    url = _redis_url().strip()
    if not url:
        return None
    try:
        import redis  # type: ignore

        return redis.Redis.from_url(url, decode_responses=True)
    except Exception as exc:                       # noqa: BLE001
        log.warning("Redis недоступен (%s) — сессии в памяти процесса", exc)
        return None


def storage() -> str:
    return "redis" if _redis() is not None else "memory"


def _key(token: str) -> str:
    return "sess:" + hashlib.sha256(token.encode()).hexdigest()


def _set(key: str, value: str, ttl: int, user_id: str | None = None) -> None:
    store = _redis()
    if store is not None:
        store.set(key, value, ex=ttl)
        if user_id:
            store.sadd(f"user_sessions:{user_id}", key)
            store.expire(f"user_sessions:{user_id}", REFRESH_TTL)
        return
    _MEMORY[key] = (value, time.time() + ttl)
    if user_id:
        _USER_INDEX.setdefault(user_id, set()).add(key)


def _get(key: str) -> str | None:
    store = _redis()
    if store is not None:
        return store.get(key)
    entry = _MEMORY.get(key)
    if entry is None:
        return None
    value, expires_at = entry
    if expires_at <= time.time():                  # TTL в памяти раньше не проверялся
        _MEMORY.pop(key, None)
        return None
    return value


def _del(key: str) -> None:
    store = _redis()
    if store is not None:
        store.delete(key)
        return
    entry = _MEMORY.pop(key, None)
    if entry:
        for keys in _USER_INDEX.values():
            keys.discard(key)


@dataclass
class Session:
    session_id: str
    user_id: str
    email: str
    role: str
    workspace_id: str
    platform_admin: bool = False
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    rotated_at: str | None = None
    chain: list[str] = field(default_factory=list)   # id предыдущих сессий (детект кражи)


def issue(user_id: str, email: str, role: str = "owner", workspace_id: str = "ws_de",
          platform_admin: bool = False) -> dict[str, Any]:
    """Выдать пару токенов. JWT подписывается ``JWT_SECRET`` (пусто → демо-режим)."""
    session = Session(session_id=f"sess_{secrets.token_hex(8)}", user_id=user_id, email=email,
                      role=role, workspace_id=workspace_id, platform_admin=platform_admin)
    refresh = secrets.token_urlsafe(32)
    _set(_key(refresh), json.dumps(asdict(session)), REFRESH_TTL, user_id=user_id)
    access = security.issue_jwt({
        "sub": user_id, "email": email, "role": role, "workspace_id": workspace_id,
        "platform_admin": platform_admin, "sid": session.session_id,
    }, ttl=ACCESS_TTL)
    return {
        "access_token": access, "refresh_token": refresh, "token_type": "bearer",
        "expires_in": ACCESS_TTL, "session_id": session.session_id,
        "platform_admin": platform_admin,
    }


def refresh(refresh_token: str) -> dict[str, Any]:
    """Ротация refresh. Повтор уже использованного токена отзывает сессию."""
    raw = _get(_key(refresh_token))
    if raw is None:
        return {"error": "refresh-токен неизвестен или истёк", "code": "invalid_refresh"}
    data = json.loads(raw)
    if data.get("revoked"):
        # Использованный refresh «воскрес» — значит, у кого-то копия. Гасим ВСЕ сессии
        # пользователя по индексу, а не только эту цепочку: атакующий мог уже
        # прокрутить ротацию и получить свою пару токенов.
        user_id = data.get("user_id", "")
        log.warning("sessions: повтор refresh у %s — отзываю все сессии", user_id)
        result = revoke_all(user_id)
        return {"error": "refresh-токен уже использован — все сессии отозваны",
                "code": "refresh_reuse", "revoked": result["revoked"]}
    session = Session(**{k: v for k, v in data.items() if k in Session.__dataclass_fields__})
    old_key = _key(refresh_token)
    new_refresh = secrets.token_urlsafe(32)
    session.rotated_at = datetime.now(timezone.utc).isoformat()
    session.chain = session.chain + [session.session_id]
    # порядок важен: сначала фиксируем пометку о ротации на старом токене, затем выдаём новый
    # и только потом удаляем старый. Раньше удаление шло последним шагом «на честном слове»:
    # два параллельных запроса успевали прочитать токен до удаления и оба получали пару
    # токенов — то есть ротация обходилась. Теперь переход атомарен для памяти процесса
    # (одна блокировка) и через WATCH/MULTI для Redis.
    # Атомарная замена «сравнить-и-записать»: старый токен становится использованным,
    # только если его значение всё ещё равно прочитанному. Второй параллельный запрос
    # получит -1 — это трактуется как повтор (кража), а не как вторая ротация.
    outcome = _rotate_atomic(old_key, raw,
                             json.dumps({**asdict(session), "revoked": True}),
                             _key(new_refresh), json.dumps(asdict(session)))
    if outcome == 0:
        return {"error": "refresh-токен неизвестен или истёк", "code": "invalid_refresh"}
    if outcome == -1:
        log.warning("sessions: гонка ротации у %s — отзываю сессии", session.user_id)
        revoked = revoke_all(session.user_id)
        return {"error": "refresh-токен уже использован — все сессии отозваны",
                "code": "refresh_reuse", "revoked": revoked["revoked"]}
    access = security.issue_jwt({
        "sub": session.user_id, "email": session.email, "role": session.role,
        "workspace_id": session.workspace_id, "platform_admin": session.platform_admin,
        "sid": session.session_id,
    }, ttl=ACCESS_TTL)
    return {"access_token": access, "refresh_token": new_refresh, "token_type": "bearer",
            "expires_in": ACCESS_TTL, "session_id": session.session_id, "rotated": True}


ROTATE_LUA = """
local current = redis.call('GET', KEYS[1])
if not current then return 0 end
if current ~= ARGV[1] then return -1 end
redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[5])
redis.call('SET', KEYS[2], ARGV[3], 'EX', ARGV[6])
if ARGV[4] ~= '' then redis.call('SADD', KEYS[3], KEYS[2]) end
return 1
"""


def _rotate_atomic(old_key: str, expected: str, revoked_payload: str,
                   new_key: str, new_payload: str) -> int:
    """Атомарная ротация refresh: «сравнить и заменить» одним действием.

    1 — ротация выполнена, 0 — токена нет, -1 — гонка (значение уже изменилось).

    Почему так, а не WATCH/MULTI: раньше `watch()` открывался на одном pipeline, а запись
    шла отдельным клиентом — транзакция была пустой и ничего не защищала. Два параллельных
    запроса могли оба увидеть старый токен и оба получить пару токенов. В памяти процесса
    то же самое делается под блокировкой, поэтому поведение одинаково в обоих режимах.
    """
    store = _redis()
    if store is None:
        with _LOCK:
            current = _get(old_key)
            if current is None:
                return 0
            if current != expected:
                return -1
            _MEMORY[old_key] = (revoked_payload, time.time() + REUSE_WINDOW)
            _MEMORY[new_key] = (new_payload, time.time() + REFRESH_TTL)
            user_id = _user_id_of(new_payload)
            if user_id:                       # индекс нужен для «выйти со всех устройств»
                _USER_INDEX.setdefault(user_id, set()).add(new_key)
            return 1
    try:
        return int(store.eval(ROTATE_LUA, 3, old_key, new_key, _user_index_key(expected),
                              expected, revoked_payload, new_payload,
                              _user_id_of(expected), REUSE_WINDOW, REFRESH_TTL))
    except Exception as exc:                       # noqa: BLE001 — без атомарности лучше отказать
        log.warning("sessions: атомарная ротация недоступна (%s)", exc)
        return -1


def _user_id_of(raw: str) -> str:
    try:
        return str(json.loads(raw).get("user_id", ""))
    except Exception:                              # noqa: BLE001
        return ""


def _user_index_key(raw: str) -> str:
    user_id = _user_id_of(raw)
    return f"user_sessions:{user_id}" if user_id else "user_sessions:_"


def revoke(refresh_token: str) -> dict[str, Any]:
    raw = _get(_key(refresh_token))
    if raw is None:
        return {"revoked": False, "reason": "сессия не найдена"}
    _del(_key(refresh_token))
    return {"revoked": True, "session_id": json.loads(raw)["session_id"]}


def revoke_all(user_id: str, tokens: list[str] | None = None) -> dict[str, Any]:
    """Отзыв ВСЕХ сессий пользователя: выход со всех устройств, смена пароля.

    Раньше отзывались только переданные клиентом токены — то есть человек выходил
    «со всех устройств», а сессия на украденном ноутбуке продолжала работать.
    Теперь есть индекс ``user_id → refresh-ключи``, и по нему гасятся все сессии.
    """
    keys: set[str] = set()
    store = _redis()
    if store is not None:
        keys.update(store.smembers(f"user_sessions:{user_id}") or set())
        store.delete(f"user_sessions:{user_id}")
    else:
        keys.update(_USER_INDEX.pop(user_id, set()))
    for token in tokens or []:                     # совместимость: явно присланные токены
        raw = _get(_key(token))
        if raw and json.loads(raw).get("user_id") == user_id:
            keys.add(_key(token))
    count = 0
    for key in keys:
        raw = _get(key)
        if raw is None:
            continue
        if json.loads(raw).get("user_id") == user_id:
            _del(key)
            count += 1
    return {"revoked": count, "user_id": user_id, "device_sessions": True}


def describe(session_id: str) -> dict[str, Any] | None:
    for key in list(_MEMORY):
        raw = _get(key)
        if raw is None:
            continue
        data = json.loads(raw)
        if data["session_id"] == session_id:
            return data
    return None


def active_of(user_id: str) -> list[dict[str, Any]]:
    """Активные сессии пользователя (для панели «Безопасность» и revoke_all)."""
    out: list[dict[str, Any]] = []
    for key in list(_USER_INDEX.get(user_id, set())):
        raw = _get(key)
        if raw is None:
            continue
        data = json.loads(raw)
        out.append({"session_id": data["session_id"], "created_at": data["created_at"],
                    "rotated_at": data.get("rotated_at"), "role": data.get("role"),
                    "workspace_id": data.get("workspace_id")})
    return out


def status() -> dict[str, Any]:
    return {"storage": storage(), "access_ttl": ACCESS_TTL, "refresh_ttl": REFRESH_TTL,
            "rotation": True, "reuse_detection": True,
            "jwt_secret": bool(os.getenv("JWT_SECRET"))}


def _now_ts() -> int:          # для тестов TTL
    return int(time.time())
