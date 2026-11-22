"""Аккаунты: tenant → workspace → user → membership, и юридический профиль бренда.

Зачем отдельный модуль. Второй аудит показал: регистрация не создавала ничего — в
`auth.request_magic()` было `workspace_id="ws_de"`, а `resolve_role()` возвращал `"owner"`
каждому новому адресу. То есть десять клиентов становились владельцами одного и того же
пространства и видели данные друг друга. Формально «роль из приглашения» появилась, но
новый пользователь по-прежнему попадал в демо-пространство.

Здесь одна функция отвечает за онбординг целиком:

    register(email) → tenant + workspace + user + membership(owner) + юридический профиль

Правила:

* у каждого нового клиента **свой** tenant и **свой** workspace (`ws_<hash>`), идентификатор
  детерминирован от e-mail — повторный вход не создаёт второй workspace;
* демо-пространство `ws_de` остаётся только для демо-стенда (`APP_ENV=dev`), и это видно
  в ответе (`is_demo`);
* юридические реквизиты (рекламодатель для маркировки по ст. 18.1) хранятся в workspace,
  а не в константе модуля: иначе реклама клиента уходит с реквизитами другого юрлица.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any

from . import tenancy

#: Демо-пространство: используется только когда клиент явно работает на демо-стенде.
DEMO_WORKSPACE = "ws_de"

TENANTS: dict[str, dict] = {}
WORKSPACES: dict[str, dict] = {}
MEMBERSHIPS: dict[tuple[str, str], dict] = {}       # (tenant_id, user_id) → membership
USERS: dict[str, dict] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slug_id(prefix: str, value: str) -> str:
    """Детерминированный идентификатор из строки (e-mail, домена): стабилен между процессами."""
    return f"{prefix}_{hashlib.sha256(value.strip().lower().encode()).hexdigest()[:10]}"


def user_id_for(email: str) -> str:
    return slug_id("u", email)


def workspace_name_from(email: str) -> str:
    """Человеческое имя пространства: домен почты или локальная часть."""
    domain = email.split("@")[-1] if "@" in email else email
    return re.sub(r"^www\.", "", domain) or "Мой бренд"


def default_legal_entity(email: str) -> dict:
    """Реквизиты по умолчанию.

    Это НЕ «Договор Эксперт»: до заполнения профиля маркировка рекламы честно помечается
    как неполная, а не подставляет чужое юрлицо.
    """
    return {"legal_name": None, "site_url": None, "inn": None, "erid_contract": None,
            "is_complete": False, "email": email}


def register(email: str, *, display_name: str | None = None,
             workspace_id: str | None = None) -> dict:
    """Онбординг клиента: tenant + workspace + user + membership(owner).

    Повторный вызов возвращает существующие записи (идемпотентно по e-mail).
    """
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        raise ValueError("нужен корректный e-mail")

    user_id = user_id_for(email)
    ws_id = workspace_id or slug_id("ws", email)
    tenant_id = slug_id("tn", email)
    existed = ws_id in WORKSPACES

    tenant = TENANTS.get(tenant_id) or {
        "id": tenant_id, "name": workspace_name_from(email), "plan_code": "trial",
        "data_region": "ru-central", "created_at": _now(),
    }
    TENANTS[tenant_id] = tenant

    workspace = WORKSPACES.get(ws_id) or {
        "id": ws_id, "tenant_id": tenant_id, "name": workspace_name_from(email),
        "site_url": None, "timezone": "Europe/Moscow", "created_at": _now(),
        "legal_entity": default_legal_entity(email),
        "brand": {"tone": "экспертный, спокойный", "stoplist": []},
    }
    WORKSPACES[ws_id] = workspace

    user = USERS.get(user_id) or {"id": user_id, "email": email,
                                  "name": display_name or email.split("@")[0],
                                  "created_at": _now()}
    USERS[user_id] = user

    MEMBERSHIPS[(tenant_id, user_id)] = {"tenant_id": tenant_id, "user_id": user_id,
                                         "role": "owner", "workspace_id": ws_id,
                                         "created_at": _now()}

    _persist(tenant, workspace, user)
    return {"user": user, "tenant": tenant, "workspace": workspace,
            "membership": MEMBERSHIPS[(tenant_id, user_id)],
            "is_new": not existed}


def workspace_for(email: str) -> str:
    """Workspace пользователя (создаёт персональный при первом обращении)."""
    email = (email or "").strip().lower()
    for (tenant_id, user_id), membership in MEMBERSHIPS.items():
        if USERS.get(user_id, {}).get("email") == email:
            return membership["workspace_id"]
    return register(email)["workspace"]["id"]


def workspace(workspace_id: str, *, workspace_hint: str | None = None) -> dict | None:
    """Профиль workspace. Невидимый в текущем контексте — «не найден»."""
    scope = tenancy.scope_workspace()
    if scope and workspace_id != scope:
        return None
    found = WORKSPACES.get(workspace_id) or _load(workspace_id)
    if found is None and workspace_id == DEMO_WORKSPACE:
        found = _demo_workspace()
    return found


def _demo_workspace() -> dict:
    """Демо-пространство стенда.

    Его реквизиты помечены ``is_demo``: маркировка честно говорит «демо», и подставить
    их в рекламу реального клиента нельзя — для клиента профиль обязателен
    (``compliance.check`` вернёт ``ADVERTISER_MISSING``).
    """
    demo = {
        "id": DEMO_WORKSPACE, "tenant_id": "tn_demo", "name": "Демо-бренд",
        "site_url": "demo.posiz.ru", "timezone": "Europe/Moscow",
        "legal_entity": {"legal_name": "ООО «Демо-бренд»", "site_url": "demo.posiz.ru",
                         "inn": None, "is_complete": True, "is_demo": True},
        "brand": {"tone": "экспертный", "stoplist": []},
    }
    WORKSPACES.setdefault(DEMO_WORKSPACE, demo)
    return WORKSPACES[DEMO_WORKSPACE]


def legal_entity(workspace_id: str) -> dict:
    """Реквизиты рекламодателя для маркировки (без них реклама не публикуется)."""
    ws = workspace(workspace_id) or {}
    return ws.get("legal_entity") or default_legal_entity("")


def set_legal_entity(workspace_id: str, *, legal_name: str, site_url: str | None = None,
                     inn: str | None = None) -> dict:
    """Заполнить рекламодателя для маркировки. Без этого реклама не публикуется."""
    ws = WORKSPACES.get(workspace_id)
    if ws is None:
        raise KeyError(workspace_id)
    legal = {"legal_name": legal_name.strip(), "site_url": (site_url or "").strip() or None,
             "inn": (inn or "").strip() or None, "erid_contract": None,
             "is_complete": bool(legal_name and site_url)}
    ws["legal_entity"] = legal
    WORKSPACES[workspace_id] = ws
    _persist(None, ws, None)
    return ws


def memberships_of_tenant(tenant_id: str) -> list[dict]:
    return [m for (t, _), m in MEMBERSHIPS.items() if t == tenant_id]


def workspaces_of_tenant(tenant_id: str) -> list[dict]:
    return [w for w in WORKSPACES.values() if w["tenant_id"] == tenant_id]


# ---------------------------------------------------------------- слой БД
def _db():
    from . import persistence

    return persistence.database()


def _persist(tenant: dict | None, workspace_: dict | None, user: dict | None) -> None:
    db = _db()
    if db is None:
        return
    # Онбординг выполняется вне контекста клиента (его ещё нет), поэтому пишем в
    # платформенном режиме — это единственное место, где так можно.
    with tenancy.bind(workspace_id=None, platform=True):
        if tenant is not None:
            db.tenant_upsert(tenant)
        if workspace_ is not None:
            db.workspace_upsert(workspace_)
        if user is not None:
            db.user_upsert(user["id"], email=user.get("email"), name=user.get("name"))
            if workspace_ is not None:
                db.membership_add(tenant_id=workspace_["tenant_id"], user_id=user["id"],
                                  role="owner")


def _load(workspace_id: str) -> dict | None:
    db = _db()
    if db is None:
        return None
    row = db.workspace_get(workspace_id)
    if row is not None:
        WORKSPACES[workspace_id] = row
    return row


def reset() -> None:
    """Для тестов: чистое состояние аккаунтов."""
    TENANTS.clear()
    WORKSPACES.clear()
    MEMBERSHIPS.clear()
    USERS.clear()
