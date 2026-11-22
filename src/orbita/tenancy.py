"""Контекст арендатора: единственная граница изоляции рабочих пространств.

Проблема из аудита: часть запросов к коллекциям не фильтровалась по workspace
(``doc_all``/``doc_get`` отдавали всё), а `X-Workspace-Id` проверялся только там,
где о нём вспомнили. Изоляция осуществлялась «по договорённости», а не по построению.

Как устроено теперь:

* При обработке запроса :mod:`orbita.rbac` вызывает :func:`bind` с workspace и ролью
  актора. Дальше **слой данных сам** сужает выборки: `persistence` и `store` берут
  :func:`scope_workspace` и добавляют условие в SQL.
* Вне запроса (миграции, cron, воркер, тесты) контекст не задан — это «платформенный»
  режим: видны все арендаторы. Такой режим **нельзя** получить из HTTP-запроса:
  обработчик без актора просто не дойдёт до данных (401/403 в `rbac`).
* Публичные страницы (`/l/<code>`, `/@<slug>`) читаются через
  :func:`public_scope` — это осознанное исключение: ссылка и визитка публичны по URL,
  но запись в них по-прежнему возможна только в своём workspace.

Отсюда два правила для кода:

1. Хочешь всё — не привязывай контекст (это возможно только вне HTTP).
2. Хочешь чужое в HTTP — получишь пустой результат, а не данные клиента.
"""
from __future__ import annotations

import contextlib
import contextvars
from dataclasses import dataclass


class TenancyError(RuntimeError):
    """Данные запрошены без контекста арендатора там, где он обязателен."""


@dataclass(frozen=True)
class Context:
    workspace_id: str | None = None
    tenant_id: str | None = None
    user_id: str | None = None
    role: str | None = None
    platform: bool = False        # True — платформенный администратор/системный процесс

    @property
    def scoped(self) -> bool:
        return bool(self.workspace_id) and not self.platform


_current: contextvars.ContextVar[Context | None] = contextvars.ContextVar(
    "orbita_tenant_context", default=None)


def current() -> Context | None:
    return _current.get()


@contextlib.contextmanager
def bind(workspace_id: str | None = None, *, tenant_id: str | None = None,
         user_id: str | None = None, role: str | None = None, platform: bool = False):
    """Привязать контекст арендатора на время блока (для обработчика, задачи, теста)."""
    token = _current.set(Context(workspace_id=workspace_id, tenant_id=tenant_id, user_id=user_id,
                                 role=role, platform=platform))
    try:
        yield _current.get()
    finally:
        _current.reset(token)


@contextlib.contextmanager
def public_scope():
    """Публичное чтение по URL (короткая ссылка, визитка): без сужения по workspace."""
    with bind(workspace_id=None, platform=True):
        yield


def bind_for(actor) -> contextlib.AbstractContextManager:
    """Привязать контекст к актору из :mod:`orbita.rbac`."""
    return bind(workspace_id=getattr(actor, "workspace_id", None),
                role=getattr(actor, "role", None),
                user_id=getattr(actor, "id", None),
                platform=bool(getattr(actor, "is_platform_admin", False)
                              or getattr(actor, "platform_admin", False)))


def scope_workspace() -> str | None:
    """Workspace, которым нужно ограничить выборку данных. ``None`` — платформенный режим."""
    ctx = _current.get()
    if ctx is None or ctx.platform:
        return None
    return ctx.workspace_id


def require_workspace() -> str:
    """Workspace обязателен (запись данных клиента, квоты, ключи)."""
    workspace = scope_workspace()
    if not workspace:
        raise TenancyError("операция требует контекста workspace: актор не привязан")
    return workspace


def matches(workspace_id: str | None) -> bool:
    """Можно ли читать запись с таким workspace_id в текущем контексте."""
    scope = scope_workspace()
    if scope is None:
        return True
    return workspace_id == scope


def status() -> dict:
    ctx = _current.get()
    return {"bound": ctx is not None,
            "workspace_id": ctx.workspace_id if ctx else None,
            "platform": ctx.platform if ctx else None,
            "scoped": ctx.scoped if ctx else False}
