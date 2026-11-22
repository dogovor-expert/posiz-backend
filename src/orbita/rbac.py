"""Серверное исполнение RBAC.

Правило проекта: интерфейс — это UX, а не защита. Любая проверка прав, показанная в UI,
дублируется здесь (зависимость FastAPI) и в PostgreSQL через RLS (см. ``sql/rls.sql``).

Источник роли в проде — подписанный JWT (claims ``sub``, ``role``, ``workspace_id``),
подпись и срок проверяются в :mod:`orbita.security`. Заголовок ``X-Actor-Role`` —
демонстрационный режим, по умолчанию **выключен** (``RBAC_DEMO_ROLES=false``):
иначе любой клиент назначает себе роль одним заголовком.
"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException

from . import security, tenancy
from .config import demo_roles_enabled

# ---------- матрица прав ----------
PERMISSIONS: set[str] = {
    "workspace:read", "workspace:manage", "billing:manage",
    "channel:read", "channel:manage",
    "signal:create", "signal:read", "variant:generate",
    "publication:read", "publication:create", "publication:submit",
    "publication:approve", "publication:publish", "publication:cancel",
    "ad:request_erid", "ad:approve",
    "link:create", "link:read",
    "analytics:read",
    # платформенные права: НЕ входят ни в одну роль воркспейса — только is_platform_admin
    "platform:admin", "platform:impersonate", "platform:ops",
}

PLATFORM_PERMISSIONS: frozenset[str] = frozenset({"platform:admin", "platform:impersonate",
                                                 "platform:ops"})

ROLE_PERMISSIONS: dict[str, set[str]] = {
    # owner воркспейса НЕ получает platform:* — иначе любой клиент бесплатного тарифа
    # видел бы метрики всей платформы и мог имперсонироваться под другими.
    "owner": {p for p in PERMISSIONS if p not in PLATFORM_PERMISSIONS},
    "copywriter": {
        "workspace:read", "channel:read", "signal:create", "signal:read",
        "variant:generate", "publication:read", "publication:create",
        "publication:submit", "ad:request_erid", "link:create", "link:read",
        "analytics:read",
    },
    "lawyer": {
        "workspace:read", "channel:read", "signal:read", "publication:read",
        "publication:approve", "ad:approve", "ad:request_erid",
        "link:read", "analytics:read",
    },
    "analyst": {
        "workspace:read", "channel:read", "signal:read", "publication:read",
        "link:read", "analytics:read",
    },
}

ROLE_TITLES = {
    "owner": "Владелец",
    "copywriter": "Копирайтер",
    "lawyer": "Юрист / Комплаенс",
    "analyst": "Аналитик",
}


@dataclass(frozen=True)
class Actor:
    id: str
    role: str
    workspace_id: str
    source: str = "header"          # header | jwt
    is_platform_admin: bool = False

    @property
    def permissions(self) -> set[str]:
        perms = set(ROLE_PERMISSIONS.get(self.role, set()))
        if self.is_platform_admin:
            perms |= PLATFORM_PERMISSIONS
        return perms


def can(role: str, permission: str, *, platform_admin: bool = False) -> bool:
    if permission in PLATFORM_PERMISSIONS and platform_admin:
        return True
    return permission in ROLE_PERMISSIONS.get(role, set())


def roles_with(permission: str) -> list[str]:
    if permission in PLATFORM_PERMISSIONS:
        return ["platform:admin"]
    return sorted(r for r, perms in ROLE_PERMISSIONS.items() if permission in perms)


def _forbidden(permission: str, role: str, actor: Actor | None = None) -> HTTPException:
    return HTTPException(
        403,
        detail={
            "kind": "forbidden",
            "permission": permission,
            "role": role,
            "role_title": ROLE_TITLES.get(role, role),
            "allowed_roles": roles_with(permission),
            "hint": "Право проверено на сервере. UI ничего не гарантирует: запрос отклонён.",
        },
    )


def _actor_from_claims(claims: dict, workspace_header: str | None) -> Actor:
    role = str(claims.get("role", "")).strip().lower()
    if role not in ROLE_PERMISSIONS:
        raise HTTPException(403, detail={"kind": "unknown_role", "role": role,
                                         "allowed_roles": sorted(ROLE_PERMISSIONS)})
    workspace = str(claims.get("workspace_id") or workspace_header or "ws_de")
    return Actor(id=str(claims.get("sub") or f"jwt:{role}"), role=role,
                 workspace_id=workspace, source="jwt",
                 is_platform_admin=bool(claims.get("platform_admin", False)))


def scope_from_headers(headers) -> tuple[str | None, str | None, str | None] | None:
    """Определить (workspace, роль, пользователь) для привязки контекста арендатора.

    Используется middleware, чтобы **слой данных** видел арендатора до того, как
    обработчик начнёт читать коллекции. Возвращает ``None``, если запрос анонимный —
    тогда данные остаются в платформенном режиме только для публичных маршрутов.
    """
    authorization = headers.get("authorization") or ""
    if authorization.lower().startswith("bearer "):
        try:
            claims = security.decode_jwt(authorization.split(" ", 1)[1].strip())
        except Exception:                                   # noqa: BLE001 — битый токен
            return None
        return (str(claims.get("workspace_id") or headers.get("x-workspace-id") or "ws_de"),
                str(claims.get("role") or "").lower(), str(claims.get("sub") or "") or None)
    if demo_roles_enabled():
        role = str(headers.get("x-actor-role") or "owner").strip().lower()
        return (headers.get("x-workspace-id") or "ws_de", role, None)
    return None


def current_actor(
    x_actor_role: str | None = Header(default=None, alias="X-Actor-Role"),
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    authorization: str | None = Header(default=None),
    x_platform_admin: str | None = Header(default=None, alias="X-Platform-Admin"),
) -> Actor:
    """Определяет действующее лицо запроса.

    1. ``Authorization: Bearer <JWT>`` — проверяется подпись, срок и claim ``role``.
    2. ``X-Actor-Role`` — только при ``RBAC_DEMO_ROLES=true`` (демо и автотесты).
    3. Иначе — 401: анонимных запросов к API нет.
    """
    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise HTTPException(401, detail={"kind": "unauthenticated",
                                            "message": "Ожидается заголовок «Authorization: Bearer <JWT>»."})
        return _actor_from_claims(security.decode_jwt(token.strip()), x_workspace_id)

    if demo_roles_enabled():
        role = (x_actor_role or "owner").strip().lower()
        if role not in ROLE_PERMISSIONS:
            raise HTTPException(403, detail={"kind": "unknown_role", "role": role,
                                             "allowed_roles": sorted(ROLE_PERMISSIONS)})
        demo_platform = str(x_platform_admin or "").lower() in {"1", "true", "yes"}
        return Actor(id=f"demo:{role}", role=role, workspace_id=x_workspace_id or "ws_de",
                     source="header", is_platform_admin=demo_platform)

    raise HTTPException(401, detail={
        "kind": "unauthenticated",
        "message": "Требуется JWT: роль из заголовка X-Actor-Role отключена (RBAC_DEMO_ROLES=false).",
        "hint": "POST /api/v1/auth/magic-link → /auth/verify выдаёт токен; в демо включите RBAC_DEMO_ROLES=true.",
    })


def require(permission: str):
    """Зависимость FastAPI: пропускает запрос только при наличии права."""

    def dependency(actor: Actor = Depends(current_actor)) -> Actor:
        if not can(actor.role, permission):
            raise _forbidden(permission, actor.role, actor)
        return actor

    dependency.__name__ = f"require_{permission.replace(':', '_')}"
    return dependency


def require_platform_admin(permission: str = "platform:admin"):
    """Зависимость FastAPI для платформенных ручек: только is_platform_admin."""

    def dependency(actor: Actor = Depends(current_actor)) -> Actor:
        if not actor.is_platform_admin:
            raise HTTPException(403, detail={
                "type": "forbidden",
                "kind": "forbidden",
                "permission": permission,
                "role": actor.role,
                "message": "Платформенная ручка доступна только администратору платформы "
                           "(users.is_platform_admin), а не владельцу воркспейса.",
                "hint": "Права platform:* не выдаются ни одной роли воркспейса.",
            })
        return actor

    dependency.__name__ = "require_platform_admin"
    return dependency


def describe() -> dict:
    return {
        "roles": [
            {"role": r, "title": ROLE_TITLES.get(r, r), "permissions": sorted(perms)}
            for r, perms in ROLE_PERMISSIONS.items()
        ],
        "demo_header": demo_roles_enabled(),
        "platform_permissions": sorted(PLATFORM_PERMISSIONS),
        "auth": "jwt" if not demo_roles_enabled() else "jwt+header",
        "note": "В проде роль приходит из JWT; X-Actor-Role игнорируется.",
    }
