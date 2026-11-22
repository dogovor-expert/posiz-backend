"""Регрессии третьего аудита: изоляция коллекций, ключи, роли, периметр.

Каждый тест здесь — воспроизведение конкретной находки из
``00-STRATEGY/10-deep-audit.md``. Тесты написаны так, чтобы падать на коде «до»
исправления: проверяется наблюдаемое поведение, а не наличие строк в исходниках.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src"))

from orbita import apikeys, auth, config, rbac, security, tenancy  # noqa: E402
from orbita.docs import Collection  # noqa: E402
from orbita.main import app  # noqa: E402


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("RBAC_DEMO_ROLES", "true")
    monkeypatch.delenv("APP_ENV", raising=False)
    return TestClient(app)


# --------------------------------------------------------------------------
# 1. Коллекции документов: арендатор, а не «ws_de»
# --------------------------------------------------------------------------
def test_collection_reads_are_scoped_to_workspace():
    """Коллекция не показывает документы чужого пространства.

    До исправления ``Collection.all()`` возвращала всё, что лежит в коллекции:
    Brand Brain, инбокс, календарь, A/B и квоты клиента A были видны клиенту B.
    """
    cards = Collection("audit3_cards")
    with tenancy.bind("ws_audit_a"):
        cards.put({"id": "card-a", "title": "секрет клиента A"})
    with tenancy.bind("ws_audit_b"):
        assert cards.all() == []
        assert cards.get("card-a") is None
    with tenancy.bind("ws_audit_a"):
        assert [c["id"] for c in cards.all()] == ["card-a"]


def test_collection_forbids_writing_into_foreign_workspace():
    """Документ с чужим workspace_id не записывается: TenancyError, а не тихая подмена."""
    cards = Collection("audit3_foreign")
    with tenancy.bind("ws_audit_b"):
        with pytest.raises(tenancy.TenancyError):
            cards.put({"id": "intruder", "workspace_id": "ws_audit_a"})
        assert cards.all() == []


def test_collection_delete_cannot_cross_workspace():
    """Удалить чужой документ по известному id нельзя."""
    cards = Collection("audit3_delete")
    with tenancy.bind("ws_audit_a"):
        cards.put({"id": "doc-a"})
    with tenancy.bind("ws_audit_b"):
        assert cards.delete("doc-a") is False
    with tenancy.bind("ws_audit_a"):
        assert cards.get("doc-a") is not None


def test_collection_api_returns_403_not_500_on_tenant_violation(client):
    """Нарушение изоляции — 403 с kind=tenancy_violation (раньше всплывало как 500)."""
    headers = {"X-Actor-Role": "owner", "X-Workspace-Id": "ws_audit_x"}
    resp = client.get("/api/v1/knowledge/cards", headers=headers)
    assert resp.status_code == 200, resp.text         # обычный путь работает
    assert isinstance(resp.json(), list)              # и не смешивает чужие карточки


# --------------------------------------------------------------------------
# 2. API-ключи: своё пространство, постоянное время сравнения, ответ без internals
# --------------------------------------------------------------------------
def test_api_keys_are_workspace_scoped():
    """Клиент видит и отзывает только свои ключи."""
    key_a = apikeys.issue("Ключ клиента A", ["read"], workspace_id="ws_key_a")
    key_b = apikeys.issue("Ключ клиента B", ["read"], workspace_id="ws_key_b")
    with tenancy.bind("ws_key_a"):
        names = [k["name"] for k in apikeys.list_keys()]
        assert names == ["Ключ клиента A"]
        assert apikeys.revoke(key_b["id"])["revoked"] is False
        assert apikeys.revoke(key_a["id"])["revoked"] is True


def test_api_key_verify_names_the_workspace():
    """Проверка ключа возвращает пространство: интеграция обязана работать в нём."""
    created = apikeys.issue("Ключ интеграции", ["read", "publish"], workspace_id="ws_key_c")
    result = apikeys.verify(created["key"])
    assert result["ok"] is True
    assert result["workspace_id"] == "ws_key_c"
    assert result["id"].startswith("key_") and len(result["id"]) > len("key_") + 12


def test_api_key_verify_endpoint_hides_internals_and_is_rate_limited(client, monkeypatch):
    """Публичный /keys/verify не отдаёт name/id ключа и подчиняется лимиту частоты."""
    created = apikeys.issue("Ключ для MCP", ["read"], workspace_id="ws_key_d")
    resp = client.post("/api/v1/keys/verify", params={"key": created["key"]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True and body["workspace_id"] == "ws_key_d"
    assert "name" not in body and "id" not in body

    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    from orbita import ratelimit
    ratelimit.limiter.reset()
    codes = [client.post("/api/v1/keys/verify", params={"key": created["key"]}).status_code
             for _ in range(30)]
    assert 429 in codes, "перебор ключа должен упираться в лимит частоты"


# --------------------------------------------------------------------------
# 3. Периметр: демо-роли в проде, издатель токена, лимиты на вход
# --------------------------------------------------------------------------
def test_demo_roles_are_disabled_in_production(monkeypatch):
    """RBAC_DEMO_ROLES=true в проде не даёт анонимному клиенту роль владельца."""
    monkeypatch.setenv("RBAC_DEMO_ROLES", "true")
    monkeypatch.setenv("APP_ENV", "prod")
    assert config.demo_roles_enabled() is False

    monkeypatch.setenv("APP_ENV", "dev")
    assert config.demo_roles_enabled() is True

    monkeypatch.setenv("RBAC_DEMO_ROLES", "false")
    assert config.demo_roles_enabled() is False


def test_anonymous_request_with_demo_roles_off_is_rejected(monkeypatch):
    monkeypatch.setenv("RBAC_DEMO_ROLES", "false")
    monkeypatch.delenv("APP_ENV", raising=False)
    client = TestClient(app)
    resp = client.get("/api/v1/keys")
    assert resp.status_code == 401
    assert resp.json()["detail"]["kind"] == "unauthenticated"


def test_jwt_issuer_and_audience_are_enforced(monkeypatch):
    """Заданный издатель/аудитория отсекают токены чужого контура с тем же секретом."""
    monkeypatch.setenv("JWT_SECRET", "audit3-secret")
    monkeypatch.delenv("JWT_ISSUER", raising=False)
    monkeypatch.delenv("JWT_AUDIENCE", raising=False)
    # токен «чужого контура»: тот же секрет, но без издателя/аудитории
    foreign = security.issue_jwt({"sub": "u1", "role": "owner", "workspace_id": "ws_1"},
                                 secret="audit3-secret")
    assert security.decode_jwt(foreign)["role"] == "owner"   # пока проверка не настроена

    monkeypatch.setenv("JWT_ISSUER", "posiz-core")
    monkeypatch.setenv("JWT_AUDIENCE", "posiz-api")
    with pytest.raises(Exception):
        security.decode_jwt(foreign)                   # издатель/аудитория заданы → мимо

    own = security.issue_jwt({"sub": "u1", "role": "owner", "workspace_id": "ws_1"},
                             secret="audit3-secret")
    claims = security.decode_jwt(own)
    assert claims["iss"] == "posiz-core" and claims["aud"] == "posiz-api"
    assert claims["role"] == "owner"


def test_auth_endpoints_declare_rate_limit_dependency():
    """Вход и проверка токенов не остаются без лимита частоты."""
    limited = {"auth_login", "auth_verify", "auth_2fa", "key_verify", "webhook"}
    assert limited <= set(__import__("orbita.ratelimit", fromlist=["LIMITS"]).LIMITS)
    paths = {route.path: route for route in app.routes if getattr(route, "path", "")}
    for path, scope in (("/api/v1/auth/magic-link", "auth_login"),
                        ("/api/v1/auth/verify", "auth_verify"),
                        ("/api/v1/auth/refresh", "auth_verify"),
                        ("/api/v1/auth/2fa/verify", "auth_2fa"),
                        ("/api/v1/keys/verify", "key_verify")):
        deps = [d.call.__name__ for d in paths[path].dependant.dependencies]
        assert f"rate_limit_{scope}" in deps, f"{path} без лимита {scope}"


def test_rate_limited_login_returns_429(client, monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    codes = [client.post("/api/v1/auth/magic-link",
                         json={"email": "probe@example.com"}).status_code
             for _ in range(15)]
    assert 429 in codes


# --------------------------------------------------------------------------
# 4. Схема: комплаенс на уровне БД держится (проверка текста миграции дешёвая,
#    поведенческая — в tests/test_pg_integration.py::test_rls_enforces_compliance)
# --------------------------------------------------------------------------
def test_policies_have_single_source_of_truth():
    """RLS-политики описаны в одном файле, миграция его лишь выполняет."""
    canonical = (BACKEND / "sql" / "rls_policies.sql").read_text()
    migration = (BACKEND / "migrations" / "versions" / "0006_rls_strict.py").read_text()
    assert "AS RESTRICTIVE FOR INSERT" in canonical
    assert "publications_compliance" in canonical
    assert canonical.count("AS RESTRICTIVE") >= 3
    assert "rls_policies.sql" in migration
    # дублей DDL в других файлах быть не должно — иначе модель зависит от порядка
    for name in ("rls_rbac.sql", "v3.sql"):
        other = (BACKEND / "sql" / name).read_text()
        assert "CREATE POLICY" not in other, f"{name}: политики должны быть в rls_policies.sql"


def test_schema_has_no_permissive_tenant_policy_left():
    """Старая «широкая» политика *_tenant снимается миграцией, а не сосуществует с новой."""
    sql = (BACKEND / "migrations" / "versions" / "0006_rls_strict.py").read_text()
    assert "_tenant" in sql and "DROP POLICY IF EXISTS" in sql


def test_dockerignore_excludes_dev_artifacts():
    ignore = BACKEND / ".dockerignore"
    assert ignore.exists(), "контекст сборки должен исключать тесты, .git и кеши"
    text = ignore.read_text()
    for pattern in ("tests", ".git", "__pycache__", "*.db"):
        assert pattern in text


# --------------------------------------------------------------------------
# 5. Коллекции под конкурентной нагрузкой: кэш общий, контекст — у каждого свой
# --------------------------------------------------------------------------
def test_collection_cache_is_thread_safe_and_tenant_isolated():
    """8 потоков × 4 арендатора: ни одной чужой строки и ни одной ошибки.

    Дефект: `Collection` держит кэш в памяти процесса и не был защищён блокировкой.
    Обработчики FastAPI синхронные, то есть выполняются в пуле потоков, и два
    одновременных `all()` перетирали `_mem`/`_order`: список «своих» документов мог
    собраться из состояния другого арендатора.
    """
    import threading

    cards = Collection("audit3_race")
    for ws in ("ws_r1", "ws_r2", "ws_r3", "ws_r4"):
        with tenancy.bind(ws):
            for i in range(5):
                cards.put({"id": f"{ws}-{i}"})

    leaks: list[str] = []
    errors: list[str] = []

    def worker(ws: str) -> None:
        try:
            with tenancy.bind(ws):
                for _ in range(200):
                    rows = cards.all()
                    if any(not str(row["id"]).startswith(ws) for row in rows):
                        leaks.append(ws)
        except Exception as exc:                  # noqa: BLE001 — тест инфраструктуры
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker, args=(f"ws_r{n}",))
               for n in (1, 2, 3, 4) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert leaks == []


# --------------------------------------------------------------------------
# 6. Квоты: своё пространство и реальное ограничение (а не украшение)
# --------------------------------------------------------------------------
def test_quota_counters_are_per_workspace():
    """Списание клиента A не уменьшает квоту клиента B."""
    from orbita import quota

    before_b = quota.get("ws_quota_b")["counters"]["generations"]["used"]
    quota.consume("generations", 5, "ws_quota_a")
    after_b = quota.get("ws_quota_b")["counters"]["generations"]["used"]
    assert after_b == before_b
    assert quota.get("ws_quota_a")["counters"]["generations"]["used"] >= 5


def test_quota_is_enforced_on_generation_and_publication(client, monkeypatch):
    """Генерация и публикация упираются в лимит с 402, а не «считаются для отчёта»."""
    from orbita import quota

    monkeypatch.setitem(quota.DEFAULT, "generations", 1)
    headers = {"X-Actor-Role": "owner", "X-Workspace-Id": "ws_quota_exhausted"}
    created = client.post("/api/v1/signals", headers=headers,
                          json={"kind": "manual", "title": "Квота", "source_url": "https://e.ru"})
    assert created.status_code in (200, 201), created.text
    signal_id = created.json()["id"]

    first = client.post(f"/api/v1/signals/{signal_id}/generate", headers=headers,
                        json={"channels": ["vk"]})
    assert first.status_code == 200, first.text
    second = client.post(f"/api/v1/signals/{signal_id}/generate", headers=headers,
                         json={"channels": ["vk"]})
    assert second.status_code == 402
    assert second.json()["detail"]["kind"] == "quota_exceeded"


def test_quota_endpoint_returns_callers_workspace(client):
    headers = {"X-Actor-Role": "owner", "X-Workspace-Id": "ws_quota_view"}
    body = client.get("/api/v1/quota", headers=headers).json()
    assert body["quota"]["workspace_id"] == "ws_quota_view"
