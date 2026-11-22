"""Изоляция рабочих пространств — «по построению», а не по договорённости.

Аудит (P0-4): выборки документов не сужались по workspace, а ``X-Workspace-Id``
проверялся там, где о нём вспомнили. Здесь проверяется три уровня защиты:

1. :mod:`orbita.tenancy` — контекст арендатора;
2. `persistence`/`store` — запросы сужаются по контексту на уровне слоя данных;
3. HTTP — чужой workspace не даёт чужих данных даже при валидном токене.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from orbita import store, tenancy
from orbita.main import app

client = TestClient(app)


# ---------------------------------------------------------------- контекст
def test_context_is_absent_outside_request():
    assert tenancy.current() is None
    assert tenancy.scope_workspace() is None          # платформенный режим


def test_bind_scopes_reads():
    with tenancy.bind("ws_a"):
        assert tenancy.scope_workspace() == "ws_a"
        assert tenancy.matches("ws_a") is True
        assert tenancy.matches("ws_b") is False       # невидимо → «404», а не «данные»
    assert tenancy.scope_workspace() is None


def test_public_scope_is_explicit_and_documented():
    """Публичные страницы читают без сужения — но это отдельный, явный режим."""
    with tenancy.public_scope():
        assert tenancy.scope_workspace() is None
        assert tenancy.current().platform is True


def test_require_workspace_raises_without_context():
    with pytest.raises(tenancy.TenancyError):
        tenancy.require_workspace()
    with tenancy.bind("ws_a"):
        assert tenancy.require_workspace() == "ws_a"


def test_platform_admin_is_not_scoped():
    with tenancy.bind("ws_a", platform=True):
        assert tenancy.scope_workspace() is None
        assert tenancy.matches("ws_b") is True


# ---------------------------------------------------------------- слой данных
def test_signals_are_invisible_across_workspaces():
    sig = store.create_signal("manual", "Договор аренды: залог", None, None, "lead",
                              workspace_id="ws_a")
    assert store.get_signal(sig["id"], workspace_id="ws_a")["id"] == sig["id"]
    assert store.get_signal(sig["id"], workspace_id="ws_b") is None


def test_variants_are_invisible_across_workspaces():
    sig = store.create_signal("manual", "Залог и возврат", None, None, "lead",
                              workspace_id="ws_a")
    var = store.save_variant(sig["id"], "vk", "post", "T", "Текст варианта для проверки.",
                             None, False, 4.2, workspace_id="ws_a")
    assert store.get_variant(var["id"], workspace_id="ws_a")["id"] == var["id"]
    assert store.get_variant(var["id"], workspace_id="ws_b") is None


def test_doc_put_rejects_foreign_workspace():
    """Запись в чужой workspace — не «тихая ошибка», а явный отказ."""
    from orbita import persistence

    db = persistence.database()
    if db is None:
        pytest.skip("проверка относится к слою БД (DATA_LAYER=db)")
    with tenancy.bind("ws_a"):
        with pytest.raises(tenancy.TenancyError):
            db.doc_put("probe", "id_1", "ws_b", {})


def test_tenancy_status_is_observable():
    with tenancy.bind("ws_a", role="lawyer"):
        status = tenancy.status()
        assert status["bound"] is True and status["scoped"] is True
        assert status["workspace_id"] == "ws_a"


# ---------------------------------------------------------------- HTTP
def test_api_isolates_workspaces_by_header(monkeypatch):
    """Токен/заголовок с чужим workspace не открывает данные другого клиента."""
    sig = store.create_signal("manual", "Секретный сигнал клиента A", None, None, "lead",
                              workspace_id="ws_client_a")
    headers = {"X-Actor-Role": "owner", "X-Workspace-Id": "ws_client_a"}
    own = client.get(f"/api/v1/signals/{sig['id']}", headers=headers)
    assert own.status_code == 200, own.text

    foreign = client.get(f"/api/v1/signals/{sig['id']}",
                         headers={"X-Actor-Role": "owner", "X-Workspace-Id": "ws_client_b"})
    assert foreign.status_code == 404, "чужой workspace не должен видеть сигнал"


def test_middleware_binds_workspace_for_request():
    from orbita import rbac

    scope = rbac.scope_from_headers({"x-actor-role": "owner", "x-workspace-id": "ws_x"})
    assert scope[0] == "ws_x"


def test_anonymous_request_has_no_tenant_context(monkeypatch):
    """Без токена и без демо-режима запрос не получает контекст арендатора вовсе."""
    from orbita import rbac

    monkeypatch.delenv("RBAC_DEMO_ROLES", raising=False)
    monkeypatch.setenv("APP_ENV", "production")
    assert rbac.scope_from_headers({}) is None
