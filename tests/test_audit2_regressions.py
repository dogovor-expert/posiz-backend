"""Регрессии по второму аудиту: база, изоляция, регистрация, публикации, PII.

Каждый тест здесь соответствует конкретной находке второго аудита. Если правило
ослабят, тест упадёт — именно этого не хватало в первой версии, где часть проблем
была помечена исправленной раньше, чем код действительно закрыл их.
"""
from __future__ import annotations

import asyncio
import pathlib
from datetime import datetime, timedelta, timezone

import pytest
import yaml

from orbita import accounts, compliance, store, tenancy
from orbita.adapters.base import NormalizedError, PublishResult
from orbita.workers import tasks

ROOT = pathlib.Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------- 3.1 citext
def test_citext_extension_is_created_by_cluster_init():
    """`users.email citext` требует расширения, иначе baseline падает на чистой базе."""
    schema = (ROOT / "schema.sql").read_text()
    cluster = (ROOT / "sql" / "00_cluster_init.sql").read_text()
    assert "citext" in schema
    assert "CREATE EXTENSION IF NOT EXISTS citext" in cluster


# ---------------------------------------------------------------- 3.2 ORM ↔ схема
def test_orm_columns_exist_in_schema_or_migrations():
    """Колонки модели обязаны существовать в схеме: иначе чтение падает на реальной БД."""
    from orbita import persistence

    schema = (ROOT / "schema.sql").read_text()
    migrations = "\n".join(p.read_text() for p in
                           (ROOT / "migrations" / "versions").glob("*.py"))
    missing = []
    for table in persistence.Base.metadata.tables.values():
        if f"CREATE TABLE {table.name}" not in schema and table.name not in schema:
            continue
        for column in table.columns:
            if column.name in schema or f"ADD COLUMN IF NOT EXISTS {column.name}" in migrations:
                continue
            if f"{column.name} " in migrations or f"{column.name}:" in migrations:
                continue
            missing.append(f"{table.name}.{column.name}")
    assert not missing, f"колонки модели без соответствия в схеме: {missing}"


def test_publication_status_is_an_enum_in_orm():
    """Строка в enum-колонку PostgreSQL не вставляется — тип должен быть Enum."""
    from sqlalchemy import Enum as SAEnum

    from orbita import persistence

    column = persistence.Base.metadata.tables["publications"].c.status
    assert isinstance(column.type, SAEnum)
    assert column.type.name == "publication_status"


def test_identifier_columns_are_uuid_and_tokens_are_bytea():
    from sqlalchemy import LargeBinary
    from sqlalchemy.types import TypeDecorator

    from orbita import persistence

    users = persistence.Base.metadata.tables["users"].c
    accounts_table = persistence.Base.metadata.tables["channel_accounts"].c
    assert users["id"].type.__class__.__name__ == "UuidStr"
    assert accounts_table["token_ciphertext"].type.__class__.__name__ == "CipherText"
    assert issubclass(persistence.CipherText, TypeDecorator)
    assert persistence.CipherText.impl is LargeBinary


# ---------------------------------------------------------------- 4.1 контекст в транзакции
def test_pg_context_is_applied_per_transaction():
    """`app_set_context` должен ВЫЗЫВАТЬСЯ, а не просто существовать."""
    source = (ROOT / "src" / "orbita" / "persistence.py").read_text()
    assert 'event.listen(self.session_factory, "after_begin"' in source
    assert "set_config" in source
    body = source.split("def _apply_pg_context", 1)[1][:900]
    # имена переменных обязаны совпадать с теми, что читают политики и SQL-функции:
    # app_tenant_id()/app_workspace_id()/app_user_id()/app_role()
    for name in ("tenant_id", "workspace_id", "user_id", "actor_role"):
        assert f'"{name}"' in body, f"контекст не выставляет app.{name}"
    assert "app.{name}" in source


# ---------------------------------------------------------------- 4.2/4.3 RLS
def test_rls_policies_cover_workspaces_memberships_and_entity_docs():
    migration = (ROOT / "migrations" / "versions" / "0005_schema_alignment.py").read_text()
    for table in ("workspaces", "memberships", "entity_docs", "channel_accounts"):
        assert table in migration, f"нет политики для {table}"
    assert "can_access_workspace" in migration
    assert "platform" in migration          # платформенный режим явно разрешён


def test_can_access_workspace_uses_request_context():
    """Политика не может опираться на рекурсивный обход таблиц, закрытых RLS."""
    migration = (ROOT / "migrations" / "versions" / "0005_schema_alignment.py").read_text()
    assert "app_workspace_id()" in migration
    assert "app.actor_role" in migration


# ---------------------------------------------------------------- 4.4 утечка в памяти
def test_memory_layer_honours_tenancy_context():
    """Воспроизведение находки: ws_B читал сигнал и вариант ws_A без явного workspace."""
    signal = store.create_signal("manual", "Секрет клиента A", None, None, "lead",
                                 workspace_id="ws_iso_a")
    variant = store.save_variant(signal["id"], "vk", "post", "T",
                                 "Текст варианта, который не должен утечь к другому клиенту.",
                                 None, False, 4.1, workspace_id="ws_iso_a")

    with tenancy.bind("ws_iso_b"):
        assert store.get_signal(signal["id"]) is None
        assert store.get_variant(variant["id"]) is None
    with tenancy.bind("ws_iso_a"):
        assert store.get_signal(signal["id"]) is not None
        assert store.get_variant(variant["id"]) is not None


def test_publication_is_scoped_to_workspace():
    signal = store.create_signal("manual", "Публикация A", None, None, "lead",
                                 workspace_id="ws_pub_a")
    variant = store.save_variant(signal["id"], "vk", "post", "T",
                                 "Текст для проверки видимости публикации по workspace.",
                                 None, False, 4.0, workspace_id="ws_pub_a")
    pub = store.create_publication(variant["id"], "vk_1", datetime.now(timezone.utc),
                                   None, is_ad=False, require_approval=False)
    with tenancy.bind("ws_pub_b"):
        assert store.get_publication(pub["id"]) is None
    with tenancy.bind("ws_pub_a"):
        assert store.get_publication(pub["id"]) is not None


# ---------------------------------------------------------------- 5 регистрация
def test_registration_creates_separate_tenants_and_workspaces():
    first = accounts.register("client-a@example.ru")
    second = accounts.register("client-b@example.ru")
    assert first["workspace"]["id"] != second["workspace"]["id"]
    assert first["tenant"]["id"] != second["tenant"]["id"]
    assert first["membership"]["role"] == "owner"
    assert first["workspace"]["id"] not in {"ws_de", ""}


def test_registration_is_idempotent():
    first = accounts.register("same@example.ru")
    second = accounts.register("same@example.ru")
    assert first["workspace"]["id"] == second["workspace"]["id"]
    assert second["is_new"] is False


def test_magic_link_gives_new_user_own_workspace(monkeypatch):
    monkeypatch.setenv("APP_ENV", "dev")
    from orbita import auth

    first = auth.request_magic("magic-a@example.ru")
    second = auth.request_magic("magic-b@example.ru")
    assert first["workspace_id"] != second["workspace_id"]
    assert first["workspace_id"] != "ws_de"
    assert first["role"] == "owner"


# ---------------------------------------------------------------- 5 2FA
def test_two_factor_is_required_at_login(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-jwt-secret-value")
    from orbita import auth

    email = "2fa-user@example.ru"
    monkeypatch.setitem(auth.MAGIC, "tok-1", {
        "email": email, "workspace_id": None, "role": "owner",
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5)})
    first = auth.verify_magic("tok-1")
    setup = auth.setup_2fa(first["user_id"], email)
    assert len(setup["backup_codes"]) == 10
    assert auth.activate_2fa(first["user_id"], "000000").get("code") == "invalid_totp"
    assert auth.activate_2fa(first["user_id"], auth._totp(setup["secret"]))["enabled"] is True

    monkeypatch.setitem(auth.MAGIC, "tok-2", {
        "email": email, "workspace_id": None, "role": "owner",
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5)})
    second = auth.verify_magic("tok-2")
    assert second["two_factor_required"] is True
    assert "access_token" not in second
    done = auth.verify_second_factor(second["challenge"], setup["backup_codes"][0])
    assert done["access_token"] and done["two_factor_verified"] is True


def test_two_factor_routes_exist():
    from orbita.main import app

    paths = {route.path for route in app.routes}
    for path in ("/api/v1/auth/2fa/setup", "/api/v1/auth/2fa/activate",
                 "/api/v1/auth/2fa/verify"):
        assert path in paths


# ---------------------------------------------------------------- 5 OAuth
def test_oauth_state_is_bound_to_redirect_uri(monkeypatch):
    monkeypatch.setenv("VK_ID_CLIENT_ID", "cid")
    monkeypatch.setenv("VK_ID_CLIENT_SECRET", "csecret")
    from orbita import auth

    state = auth.oauth_url("vk_id", redirect_uri="https://app.posiz.ru/cb")["state"]
    result = auth.exchange_code("vk_id", "code", redirect_uri="https://evil.example/cb",
                                state=state)
    assert result["code"] == "invalid_state"


# ---------------------------------------------------------------- 6.1 publish-now
def test_publish_now_goes_through_engine():
    source = (ROOT / "src" / "orbita" / "api.py").read_text()
    assert "publish_publication" in source
    # отдельного вызова адаптера из API больше нет
    assert "adapter.publish(" not in source


# ---------------------------------------------------------------- 6.2 машина состояний
def test_status_change_outside_state_machine_is_rejected():
    from orbita import publishing

    signal = store.create_signal("manual", "Машина состояний", None, None, "lead",
                                 workspace_id="ws_sm")
    variant = store.save_variant(signal["id"], "vk", "post", "T",
                                 "Текст для проверки переходов статуса публикации.", None,
                                 False, 4.0, workspace_id="ws_sm")
    pub = store.create_publication(variant["id"], "vk_1", datetime.now(timezone.utc),
                                   None, is_ad=False, require_approval=False)
    store.claim_publication(pub["id"], "worker-1")
    store.mark_published(pub["id"], "ext_1", "")
    with pytest.raises(publishing.TransitionError):
        store.pub_update(pub["id"], status="queued")      # из published обратно нельзя
    with pytest.raises(publishing.TransitionError):
        store.mark_retry(pub["id"], "ошибка", datetime.now(timezone.utc))


# ---------------------------------------------------------------- 6.3 аренда и дубли
def test_job_timeout_covers_lease_and_lease_is_renewed():
    from orbita.workers import run

    assert run.WorkerSettings.job_timeout >= 3 * 120
    source = (ROOT / "src" / "orbita" / "workers" / "tasks.py").read_text()
    assert "_renew_lease" in source


def test_attempt_journal_records_intent_before_publishing(monkeypatch):
    """Намерение фиксируется до площадки: после сбоя видно, что попытка была."""
    started: list[dict] = []
    original = store.attempt_begin

    def spy(pid, attempt, worker_id, key, workspace_id=None):
        started.append({"pid": pid, "key": key})
        return original(pid, attempt, worker_id, key, workspace_id)

    monkeypatch.setattr(store, "attempt_begin", spy)

    class Flaky:
        async def publish(self, payload, idempotency_key):
            assert started, "журнал попытки должен быть записан ДО обращения к каналу"
            raise NormalizedError("transient", "timeout после отправки")

    monkeypatch.setattr(tasks, "get_adapter", lambda channel, token=None: Flaky())

    signal = store.create_signal("manual", "Журнал попыток", None, None, "lead",
                                 workspace_id="ws_journal")
    variant = store.save_variant(signal["id"], "vk", "post", "T",
                                 "Текст для проверки журнала попыток публикации.", None,
                                 False, 4.0, workspace_id="ws_journal")
    pub = store.create_publication(variant["id"], "vk_1",
                                   datetime.now(timezone.utc) - timedelta(seconds=1),
                                   None, is_ad=False, require_approval=False)
    result = asyncio.run(tasks.publish_publication(None, pub["id"]))
    assert result["status"] == "retry_wait"
    last = store.attempt_last(pub["id"])
    assert last is not None and last["attempt"] >= 1


def test_uncertain_attempt_triggers_reconcile(monkeypatch):
    """Незакрытая попытка → следующая публикация идёт со сверкой, а не слепым повтором."""
    seen: dict = {}

    class Recorder:
        async def publish(self, payload, idempotency_key):
            seen["reconcile"] = payload.reconcile
            return PublishResult(external_id="ext_rec", external_url="")

    monkeypatch.setattr(tasks, "get_adapter", lambda channel, token=None: Recorder())

    signal = store.create_signal("manual", "Сверка", None, None, "lead", workspace_id="ws_rec")
    variant = store.save_variant(signal["id"], "vk", "post", "T",
                                 "Текст для проверки режима сверки после сбоя.", None,
                                 False, 4.0, workspace_id="ws_rec")
    pub = store.create_publication(variant["id"], "vk_1",
                                   datetime.now(timezone.utc) - timedelta(seconds=1),
                                   None, is_ad=False, require_approval=False)
    store.attempt_begin(pub["id"], 0, "crashed-worker", pub["idempotency_key"])  # «зависла»
    asyncio.run(tasks.publish_publication(None, pub["id"]))
    assert seen["reconcile"] is True


# ---------------------------------------------------------------- 7 маркировка
def test_advertiser_comes_from_workspace_not_module_constant():
    source = (ROOT / "src" / "orbita" / "compliance.py").read_text()
    assert 'advertiser_name = "ООО «Договор Эксперт»"' not in source
    ws_id = accounts.register("adv-client@example.ru")["workspace"]["id"]
    accounts.set_legal_entity(ws_id, legal_name="ООО «Клиент»", site_url="client.ru")
    legal = accounts.legal_entity(ws_id)
    ctx = compliance.VariantContext(channel="vk", body="Реклама", erid="erid: ABC12345",
                                   advertiser_name=legal["legal_name"],
                                   advertiser_site=legal["site_url"])
    assert "ООО «Клиент»" in compliance.apply_marking("Пост.", ctx)


def test_ad_without_advertiser_is_blocked():
    report = compliance.check(compliance.VariantContext(channel="vk", body="Скидка 30%",
                                                       erid="erid: X1"), is_ad=True)
    assert any(r.rule_code == "ADVERTISER_MISSING" for r in report.reasons)


def test_incomplete_marking_is_not_treated_as_marked():
    ctx = compliance.VariantContext(channel="vk", body="текст", erid="erid: ABC12345",
                                    advertiser_name="ООО «Клиент»", advertiser_site="client.ru")
    assert compliance.is_marked("просто текст с упоминанием erid: где-то", ctx) is False
    assert compliance.is_marked("Реклама. ООО «Клиент», client.ru\nerid: ABC12345", ctx) is True


# ---------------------------------------------------------------- 8 PII
def test_ner_receives_no_personal_names(monkeypatch):
    from orbita import pii

    seen: list[str] = []
    monkeypatch.setattr(pii, "_ner_remote", lambda text: (seen.append(text), [])[1])
    pii.mask("Клиент Иван Петров, телефон +7 916 123-45-67, почта ivan@example.ru")
    assert seen
    assert all("Иван" not in text and "916" not in text and "ivan@" not in text
               for text in seen)


def test_embeddings_are_masked(monkeypatch):
    from orbita import llm

    captured: dict = {}

    class FakeLLM(llm.LLM):
        def _post(self, path, payload):
            captured["input"] = payload["input"]
            return {"data": [{"embedding": [0.1] * llm.EMBED_DIM}
                             for _ in payload["input"]],
                    "usage": {"prompt_tokens": 3}}

    monkeypatch.setattr(llm, "enabled", lambda: True)
    FakeLLM().embed(["Договор с Иваном Петровым, тел +7 916 123-45-67"])
    assert captured["input"]
    assert "916" not in captured["input"][0]


# ---------------------------------------------------------------- 9 инфраструктура
def test_compose_has_no_default_secrets_or_public_db_ports():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    services = compose["services"]
    assert "postgres" not in str(services["db"]["environment"]["POSTGRES_PASSWORD"])
    assert not services["db"].get("ports")
    assert not services["redis"].get("ports")
    assert "requirepass" in " ".join(services["redis"]["command"])
    assert "${" in services["api"]["environment"]["DATABASE_URL"]


def test_cluster_init_requires_password_from_environment():
    cluster = (ROOT / "sql" / "00_cluster_init.sql").read_text()
    assert "\\getenv app_password ORBITA_DB_PASSWORD" in cluster
    assert "PASSWORD 'orbita_app'" not in cluster


# ---------------------------------------------------------------- 10 генерация
def test_generator_requires_workspace_and_persists_results():
    source = (ROOT / "src" / "orbita" / "generator.py").read_text()
    assert 'workspace_id: str = "ws_de"' not in source
    assert "workspace_id=workspace_id" in source
    assert "store.update_variant(" in source


def test_generation_stays_in_actor_workspace():
    from orbita import generator

    ws = "ws_gen_a"
    signal = store.create_signal("manual", "Генерация в своём workspace", None, None, "lead",
                                 workspace_id=ws)
    result = generator.run(signal["id"], ["vk"], workspace_id=ws)
    assert result["variants"]
    variant = result["variants"][0]
    assert store.get_variant(variant["id"], workspace_id="ws_gen_b") is None
    assert store.get_variant(variant["id"], workspace_id=ws) is not None
    # результаты проверок сохраняются, а не теряются в памяти
    assert store.get_variant(variant["id"], workspace_id=ws)["facts"]
