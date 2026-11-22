"""Слой данных: SQLAlchemy-модели, персистентность и делегирование из store.

Тесты работают на SQLite (та же ORM-модель, другой диалект), поэтому проверяют именно
логику репозитория, а не наличие поднятого PostgreSQL. DDL дополнительно компилируется
под postgresql-диалект — так ловится несовместимость типов до деплоя.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.schema import CreateTable

from orbita import persistence, quota, store
from orbita.persistence import (Base, Database, PublicationRow, QuotaCounterRow, ShortLinkRow,
                                to_uuid)


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "orbita.db"
    database = Database(f"sqlite:///{path}")
    database.create_all()
    monkeypatch.setenv("DATA_LAYER", "db")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{path}")
    persistence.set_database(database)
    yield database
    persistence.set_database(None)


def test_models_compile_for_postgresql():
    """DDL моделей собирается под postgresql: uuid/jsonb/числовые типы совместимы."""
    from sqlalchemy.dialects import postgresql

    for table in (ShortLinkRow.__table__, PublicationRow.__table__, QuotaCounterRow.__table__):
        ddl = str(CreateTable(table).compile(dialect=postgresql.dialect()))
        assert "CREATE TABLE" in ddl
    assert "UUID" in str(CreateTable(ShortLinkRow.__table__).compile(dialect=postgresql.dialect()))


def test_demo_ids_map_to_uuid_deterministically():
    assert to_uuid("ws_de") == to_uuid("ws_de")
    assert to_uuid("ws_de") != to_uuid("ws_ps")
    assert isinstance(to_uuid("pub_1"), uuid.UUID)
    real = uuid.uuid4()
    assert to_uuid(real) == real


def test_links_survive_restart(db, tmp_path):
    store.save_link({"code": "a1b2", "target_url": "https://dogovor.expert/x",
                     "original_url": "https://dogovor.expert/x?utm_source=vk",
                     "channel": "vk", "campaign": "c1", "clicks": 0,
                     "created_at": datetime.now(timezone.utc), "expires_at": None})
    store.add_click({"code": "a1b2", "ts": datetime.now(timezone.utc), "ip_hash": "h"})

    # «перезапуск контейнера»: новое подключение к той же базе
    persistence.set_database(Database(f"sqlite:///{tmp_path / 'orbita.db'}"))
    link = store.get_link("a1b2")
    assert link is not None and link["clicks"] == 1, "клики должны пережить рестарт"
    assert store.list_links()[0]["code"] == "a1b2"
    assert store.link_clicks_total() == 1


def test_publications_and_idempotency(db):
    when = datetime.now(timezone.utc) + timedelta(hours=1)
    pub = {
        "id": "pub_abc123", "workspace_id": "ws_de", "variant_id": "var_1",
        "channel_account_id": "vk_1", "channel": "vk", "scheduled_at": when,
        "status": "queued", "idempotency_key": "var_1:vk_1", "cost_estimate_rub": 0,
    }
    first = db.pub_create(pub)
    second = db.pub_create({**pub, "id": "pub_other"})
    assert first["id"] == second["id"], "идемпотентность: второй вызов не создаёт дубль"
    assert db.stats()["publications"] == 1


def test_due_selection_and_atomic_claim(db):
    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    future = datetime.now(timezone.utc) + timedelta(hours=3)
    db.pub_create({"id": "pub_due", "workspace_id": "ws_de", "variant_id": "v1",
                   "channel_account_id": "vk_1", "channel": "vk", "scheduled_at": past,
                   "status": "queued", "idempotency_key": "due"})
    db.pub_create({"id": "pub_later", "workspace_id": "ws_de", "variant_id": "v2",
                   "channel_account_id": "vk_1", "channel": "vk", "scheduled_at": future,
                   "status": "queued", "idempotency_key": "later"})

    due = db.due_publications()
    assert [d["id"] for d in due] == [to_uuid("pub_due").__str__()]

    assert db.claim_publication("pub_due") is True
    assert db.claim_publication("pub_due") is False, "второй воркер не должен забрать пост"
    assert db.pub_get("pub_due")["status"] == "publishing"
    assert db.pub_get("pub_due")["attempt_count"] == 1


def test_quota_counters_delegate_to_db(db):
    counters = quota.get("ws_de")["counters"]
    assert counters["generations"]["limit"] == 600
    quota.consume("generations", 10, "ws_de")
    assert quota.get("ws_de")["counters"]["generations"]["used"] == 10
    quota.buy_pack("generations_300", "ws_de")
    assert quota.get("ws_de")["counters"]["generations"]["limit"] == 900
    with pytest.raises(quota.QuotaExceeded):
        quota.consume("generations", 10_000, "ws_de")


def test_audit_log_is_persisted(db):
    store.audit("owner", "POST", "/publications", 201, {"publication_id": "pub_1"})
    tail = store.audit_tail(10)
    assert tail["total"] == 1
    assert tail["items"][0]["path"] == "/publications"
    assert tail["items"][0]["role"] == "owner"


def test_channel_tokens_are_ciphertext_in_db(db):
    store.set_channel_token("max_1", "MAX-RAW-TOKEN", channel="max")
    with db.session() as s:
        row = s.get(persistence.ChannelAccountRow, "max_1")
        assert row.token_ciphertext.startswith("v1:")
        assert "MAX-RAW-TOKEN" not in row.token_ciphertext
    assert store.get_channel_token("max_1") == "MAX-RAW-TOKEN"
    assert store.channel_token_hint("max_1").startswith("…")


def test_memory_layer_is_default(monkeypatch):
    monkeypatch.delenv("DATA_LAYER", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert persistence.database() is None
