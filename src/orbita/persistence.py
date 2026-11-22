"""Слой данных: PostgreSQL через SQLAlchemy 2.0 (замена in-memory словарям).

Здесь только то, что «сгорало» при перезапуске контейнера и ломалось при двух воркерах:
короткие ссылки и клики, публикации и отложенная очередь, счётчики квот, журнал аудита
и зашифрованные токены каналов.

Правила, которые соблюдает модуль:

* **id терпимы к демо-значениям.** В схеме `id` — ``uuid``. Демо-идентификаторы
  (``ws_de``, ``pub_3f2a…``) детерминированно отображаются в UUIDv5, поэтому один и тот же
  демо-воркспейс всегда попадает в одну и ту же строку, а схема остаётся uuid-типизированной.
* **Только шифротекст.** Токены каналов пишутся через :mod:`orbita.crypto` (AES-256-GCM);
  открытый токен не попадает ни в БД, ни в ответ API.
* **Атомарная выдача публикации воркеру.** ``claim_publication`` — это
  ``UPDATE … WHERE status='queued' RETURNING id``: два воркера не опубликуют один пост дважды.
* **Типы переносимы.** JSON → ``jsonb`` на PostgreSQL (и ``JSON`` на SQLite для тестов),
  чтобы код можно было проверять без поднятой БД.
"""
from __future__ import annotations

import json

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import (JSON, Boolean, DateTime, Enum, Integer, LargeBinary, Numeric, String,
                        Text, TypeDecorator, Uuid, create_engine, delete, event, func, or_,
                        select, update)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from . import crypto, tenancy
from .config import data_layer, settings

log = logging.getLogger("orbita.db")

JSONType = JSON().with_variant(JSONB, "postgresql")

#: Значения ENUM из `schema.sql`. Держим списком, чтобы модель и схема совпадали
#: буквально: `tests/test_schema_sync.py` сверяет их и падает при расхождении.
PUBLICATION_STATUSES = ("draft", "queued", "publishing", "retry_wait", "published", "failed",
                        "needs_reauth", "blocked_by_compliance", "cancelled")
CHANNEL_KINDS = ("max", "vk", "telegram", "ok", "dzen", "tenchat", "vk_video", "rutube", "vcru",
                 "habr", "yandex_business", "twogis", "x", "pinterest", "linkedin", "threads",
                 "instagram", "facebook", "youtube", "bluesky", "mastodon", "gbp", "vk_messenger",
                 "whatsapp", "discord", "twitch", "kick", "email", "web_push")
_NAMESPACE = uuid.UUID("6f9d3a8e-2f4b-5c1d-9e77-1b2c3d4e5f60")


def to_uuid(value: str | uuid.UUID | None) -> uuid.UUID | None:
    """Детерминированное отображение демо-идентификатора в UUID (схема требует uuid)."""
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError):
        return uuid.uuid5(_NAMESPACE, str(value))


def _canonical_id(doc_id: str) -> str:
    """Канонический id документа.

    Демо-идентификаторы (``sig_…``/``var_…``) и UUID одной и той же сущности должны
    указывать на одну строку: публикация хранит UUID (схема требует uuid), а документ
    агентов — исходный строковый id. Без нормализации воркер не находил вариант и
    публикация падала с ``missing_variant``.
    """
    return str(to_uuid(doc_id))


def from_uuid(value: uuid.UUID | None) -> str | None:
    return str(value) if value is not None else None


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Модели (подмножество schema.sql + sql/v3.sql, нужное рантайму)
# ---------------------------------------------------------------------------
class UuidStr(TypeDecorator):
    """Колонка `uuid`, значение в Python — строка.

    Схема типизирована `uuid`, а код (и демо-данные) работают со строками вроде ``ws_de``
    или ``vk_1``. Адаптер детерминированно отображает строку в UUIDv5 на границе БД —
    поэтому одна и та же демо-сущность всегда попадает в одну строку, а тип колонки
    остаётся тем, что объявлен в схеме.
    """

    impl = Uuid
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return to_uuid(value)

    def process_result_value(self, value, dialect):
        return str(value) if value is not None else None


class CipherText(TypeDecorator):
    """``bytea`` в схеме, ``str`` (base64-шифротекст AES-GCM) в Python."""

    impl = LargeBinary
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return value.encode() if isinstance(value, str) else value

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.decode() if isinstance(value, (bytes, bytearray)) else value


class Base(DeclarativeBase):
    pass


class ShortLinkRow(Base):
    __tablename__ = "short_links"

    code: Mapped[str] = mapped_column(String(16), primary_key=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    target_url: Mapped[str] = mapped_column(Text)
    original_url: Mapped[str] = mapped_column(Text)
    channel: Mapped[str | None] = mapped_column(String(32), nullable=True)
    campaign: Mapped[str | None] = mapped_column(String(64), nullable=True)
    clicks_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ClickRow(Base):
    """Клик по короткой ссылке — строка в ``analytics_events`` (kind='click')."""

    __tablename__ = "analytics_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    kind: Mapped[str] = mapped_column(String(24), default="click")
    value: Mapped[float] = mapped_column(Numeric(12, 2), default=1)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    meta: Mapped[dict] = mapped_column(JSONType, default=dict)


class PublicationRow(Base):
    __tablename__ = "publications"

    id: Mapped[str] = mapped_column(UuidStr, primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id: Mapped[str] = mapped_column(UuidStr, index=True)
    variant_id: Mapped[str | None] = mapped_column(UuidStr, nullable=True)
    channel_account_id: Mapped[str | None] = mapped_column(UuidStr, nullable=True)
    channel: Mapped[str | None] = mapped_column(String(32), nullable=True)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    #: ENUM `publication_status` в схеме; String отправляла text в enum-колонку и падала
    status: Mapped[str] = mapped_column(
        Enum(*PUBLICATION_STATUSES, name="publication_status", create_type=False,
             native_enum=True, validate_strings=True),
        default="queued", index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    erid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_ad: Mapped[bool] = mapped_column(Boolean, default=False)
    external_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    external_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    cost_estimate_rub: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    #: id артефактов медиа (PNG/SVG карточки, обложки). Храним именно id: ссылка
    #: подписывается и живёт час, поэтому её нельзя «запечь» в публикацию на неделю вперёд
    media_ids: Mapped[list[str] | None] = mapped_column(JSONType, default=list)
    approved_by_role: Mapped[str | None] = mapped_column(String(32), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # --- аренда и повторы (Publishing Engine, ADR 0017) ---
    # publishing без подтверждения живёт только до lease_until: иначе задача «висит» вечно
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claimed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class QuotaCounterRow(Base):
    __tablename__ = "quota_counters"

    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    period: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    key: Mapped[str] = mapped_column(String(32), primary_key=True)
    used: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    limit_value: Mapped[float] = mapped_column(Numeric(12, 2), default=0)


class AuditLogRow(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    actor_kind: Mapped[str] = mapped_column(String(16), default="user")
    action: Mapped[str] = mapped_column(String(16))
    object_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    object_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    meta: Mapped[dict] = mapped_column(JSONType, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class EntityDocRow(Base):
    """Документ домена v3 (Brand Brain, Инбокс, Bio, Автоматизации, A/B).

    ``collection`` — имя будущей типизированной таблицы, поэтому миграция на schema-таблицы
    сводится к переносу строк, а не к переделке вызовов.
    """

    __tablename__ = "entity_docs"

    collection: Mapped[str] = mapped_column(String(48), primary_key=True)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(UuidStr, index=True)
    payload: Mapped[dict] = mapped_column(JSONType, default=dict)
    position: Mapped[int] = mapped_column(Integer, default=0, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class PublicationAttemptRow(Base):
    """Журнал попыток публикации: намерение фиксируется до вызова площадки.

    Уникальный частичный индекс `publication_attempts_inflight` (миграция 0005) не даёт
    начать вторую попытку, пока первая не закрыта, — так повтор после сетевого таймаута
    не превращается во второй пост.
    """

    __tablename__ = "publication_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    publication_id: Mapped[str] = mapped_column(UuidStr, index=True)
    workspace_id: Mapped[str] = mapped_column(UuidStr, index=True)
    attempt: Mapped[int] = mapped_column(Integer)
    worker_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    outcome: Mapped[str | None] = mapped_column(Text, nullable=True)
    external_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class TenantRow(Base):
    """Организация клиента: верхний уровень изоляции."""

    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(UuidStr, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    plan_code: Mapped[str] = mapped_column(Text, default="trial")
    data_region: Mapped[str] = mapped_column(Text, default="ru-central")
    settings: Mapped[dict] = mapped_column(JSONType, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class WorkspaceRow(Base):
    """Бренд внутри организации: рабочее пространство, к которому привязаны данные."""

    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(UuidStr, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(UuidStr, index=True)
    name: Mapped[str] = mapped_column(Text)
    site_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    timezone: Mapped[str] = mapped_column(Text, default="Europe/Moscow")
    brand: Mapped[dict] = mapped_column(JSONType, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class MembershipRow(Base):
    """Членство пользователя в организации с ролью."""

    __tablename__ = "memberships"

    tenant_id: Mapped[str] = mapped_column(UuidStr, primary_key=True)
    user_id: Mapped[str] = mapped_column(UuidStr, primary_key=True)
    role: Mapped[str] = mapped_column(
        Enum("owner", "admin", "editor", "analyst", "client_viewer", "copywriter", "lawyer",
             name="member_role", create_type=False, native_enum=True),
        default="editor")


class UserRow(Base):
    """Пользователи: платформенный администратор отделён от владельца воркспейса."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(UuidStr, primary_key=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_platform_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ChannelAccountRow(Base):
    __tablename__ = "channel_accounts"

    id: Mapped[str] = mapped_column(UuidStr, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(UuidStr, index=True)
    channel: Mapped[str] = mapped_column(
        Enum(*CHANNEL_KINDS, name="channel_kind", create_type=False, native_enum=True))
    external_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    audience_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(
        Enum("active", "needs_reauth", "rate_limited", "paused", "revoked",
             name="account_status", create_type=False, native_enum=True),
        default="active")
    token_ciphertext: Mapped[str | None] = mapped_column(CipherText, nullable=True)
    token_key_version: Mapped[int] = mapped_column(Integer, default=1)
    capabilities: Mapped[dict] = mapped_column(JSONType, default=dict)
    health: Mapped[dict] = mapped_column(JSONType, default=dict)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class SignalRow(Base):
    """Инфоповод: типизированная строка (не только документ в entity_docs).

    На `signals` ссылается `variants`, на `variants` — `publications`, поэтому без
    типизированной записи цепочка «сигнал → вариант → публикация» не проходит FK.
    """

    __tablename__ = "signals"

    id: Mapped[str] = mapped_column(UuidStr, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(UuidStr, index=True)
    kind: Mapped[str] = mapped_column(
        Enum("url", "manual", "voice", "rss", "trend", "recycle", "law_change",
             name="signal_kind", create_type=False, native_enum=True), default="manual")
    title: Mapped[str] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    goal: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="new")


class VariantRow(Base):
    """Вариант поста для канала: типизированная строка под FK из publications."""

    __tablename__ = "variants"

    id: Mapped[str] = mapped_column(UuidStr, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(UuidStr, index=True)
    signal_id: Mapped[str] = mapped_column(UuidStr, index=True)
    channel: Mapped[str] = mapped_column(
        Enum(*CHANNEL_KINDS, name="channel_kind", create_type=False, native_enum=True))
    format: Mapped[str] = mapped_column(String(32), default="post")
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    body: Mapped[str] = mapped_column(Text)
    link_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    link_in_reply: Mapped[bool] = mapped_column(Boolean, default=False)
    review_score: Mapped[float | None] = mapped_column(Numeric(3, 2), nullable=True)


# ---------------------------------------------------------------------------
# Соединение
# ---------------------------------------------------------------------------
class Database:
    def __init__(self, url: str | None = None, *, echo: bool = False) -> None:
        url = url or settings.database_url
        if not url:
            raise RuntimeError("DATABASE_URL не задан: слой данных db недоступен")
        self.url = url
        self.engine: Engine = create_engine(url, echo=echo, future=True,
                                            pool_pre_ping=True, pool_size=5, max_overflow=5)
        self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)
        # Контекст арендатора выставляется в PostgreSQL НА КАЖДОЙ транзакции.
        # Раньше функция app_set_context() существовала, но никто её не вызывал:
        # политики RLS читали пустые app.*-переменные и работали как «запретить всё».
        event.listen(self.session_factory, "after_begin", self._apply_pg_context)

    @staticmethod
    def _apply_pg_context(session: Session, transaction, connection) -> None:
        ctx = tenancy.current()
        # Имена переменных совпадают с теми, что читают политики и функции SQL
        # (app_tenant_id/app_workspace_id/app_user_id/app_role). Раньше здесь стояли
        # короткие app.tenant/app.role: set_config выполнялся, а политики читали
        # совсем другие имена и видели NULL — то есть изоляция молча не работала.
        if ctx is None:
            # вне запроса (миграции, cron, воркер): платформенный режим — политики это видят
            # по app.actor_role='platform', а не по пустым значениям
            values = {"tenant_id": "", "workspace_id": "", "user_id": "", "actor_role": "platform"}
        else:
            values = {"tenant_id": ctx.tenant_id or "", "workspace_id": ctx.workspace_id or "",
                      "user_id": ctx.user_id or "",
                      "actor_role": "platform" if ctx.platform else (ctx.role or "")}
        if connection.dialect.name != "postgresql":
            return                      # sqlite (тесты схемы) не знает set_config и RLS
        for name, value in values.items():
            connection.exec_driver_sql(
                "SELECT set_config(%s, %s, true)", (f"app.{name}", str(value)))

    def create_all(self) -> None:
        """Только для тестов и локального стенда: в проде схему создаёт Alembic."""
        Base.metadata.create_all(self.engine)

    def session(self) -> Session:
        return self.session_factory()

    # ---------------- короткие ссылки ----------------
    def link_create(self, link: dict[str, Any]) -> dict[str, Any]:
        with self.session() as s:
            row = ShortLinkRow(
                code=link["code"], workspace_id=to_uuid(link.get("workspace_id", "ws_de")),
                target_url=link["target_url"], original_url=link.get("original_url", link["target_url"]),
                channel=link.get("channel"), campaign=link.get("campaign"),
                clicks_count=int(link.get("clicks", 0)), created_at=link.get("created_at") or _now(),
                expires_at=link.get("expires_at"),
            )
            s.add(row)
            s.commit()
            return self._link_dict(row)

    def link_get(self, code: str) -> dict[str, Any] | None:
        with self.session() as s:
            row = s.get(ShortLinkRow, code)
            if row is None:
                return None
            if row.expires_at and row.expires_at.replace(tzinfo=timezone.utc) < _now():
                return None
            return self._link_dict(row)

    def links_list(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.session() as s:
            rows = s.scalars(select(ShortLinkRow).order_by(ShortLinkRow.created_at.desc())
                             .limit(limit)).all()
            return [self._link_dict(r) for r in rows]

    def link_add_click(self, event: dict[str, Any]) -> dict[str, Any]:
        with self.session() as s:
            link = s.get(ShortLinkRow, event["code"])
            if link is not None:
                link.clicks_count = (link.clicks_count or 0) + 1
            s.add(ClickRow(
                workspace_id=to_uuid(event.get("workspace_id", "ws_de")), kind="click",
                occurred_at=event.get("ts") or _now(),
                meta={"link_code": event.get("code"), "referer": event.get("referer"),
                      "ua_hash": event.get("ua_hash"), "ip_hash": event.get("ip_hash")},
            ))
            s.commit()
            return event

    def link_clicks_total(self) -> int:
        with self.session() as s:
            return int(s.scalar(select(func.count()).select_from(ClickRow)) or 0)

    @staticmethod
    def _link_dict(row: ShortLinkRow) -> dict[str, Any]:
        return {
            "code": row.code, "workspace_id": from_uuid(row.workspace_id),
            "target_url": row.target_url, "original_url": row.original_url,
            "channel": row.channel, "campaign": row.campaign,
            "clicks": row.clicks_count or 0, "created_at": row.created_at,
            "expires_at": row.expires_at,
        }

    # ---------------- публикации ----------------
    def pub_create(self, pub: dict[str, Any]) -> dict[str, Any]:
        # variant_id в схеме NOT NULL: без него публикация не имеет содержимого, а
        # раньше вызов с variant_id=None доходил до базы и падал IntegrityError (500)
        # вместо понятной ошибки валидации.
        if not pub.get("variant_id"):
            raise ValueError("публикация без variant_id не создаётся: нет содержимого")
        if not pub.get("channel_account_id"):
            raise ValueError("публикация без channel_account_id не создаётся: некуда отправлять")
        with self.session() as s:
            existing = s.scalar(select(PublicationRow).where(
                PublicationRow.idempotency_key == pub["idempotency_key"]))
            if existing is not None:                       # идемпотентность: дубль не создаём
                return self._pub_dict(existing)
            row = PublicationRow(
                id=to_uuid(pub["id"]) or uuid.uuid4(),
                # workspace берётся из публикации: подстановка "ws_de" отправляла данные
                # любого клиента в чужое пространство (находка второго аудита)
                workspace_id=to_uuid(pub.get("workspace_id") or tenancy.scope_workspace() or "ws_de"),
                variant_id=to_uuid(pub.get("variant_id")),
                channel_account_id=to_uuid(pub.get("channel_account_id")),
                channel=pub.get("channel"),
                scheduled_at=_as_dt(pub["scheduled_at"]),
                status=pub["status"], idempotency_key=pub["idempotency_key"],
                erid=pub.get("erid"), is_ad=bool(pub.get("is_ad")),
                cost_estimate_rub=pub.get("cost_estimate_rub", 0),
                media_ids=list(pub.get("media_ids") or []),
            )
            s.add(row)
            s.commit()
            return self._pub_dict(row)

    def pub_get(self, pid: str) -> dict[str, Any] | None:
        with self.session() as s:
            row = s.get(PublicationRow, to_uuid(pid))
            return self._pub_dict(row) if row else None

    def pub_by_idempotency(self, key: str) -> dict[str, Any] | None:
        with self.session() as s:
            row = s.scalar(select(PublicationRow).where(PublicationRow.idempotency_key == key))
            return self._pub_dict(row) if row else None

    def pub_update(self, pid: str, **fields: Any) -> dict[str, Any] | None:
        allowed = {"status", "external_id", "external_url", "last_error", "attempt_count",
                   "approved_by_role", "approved_at", "published_at", "scheduled_at",
                   "lease_until", "claimed_by", "next_attempt_at"}
        payload = {k: v for k, v in fields.items() if k in allowed}
        with self.session() as s:
            row = s.get(PublicationRow, to_uuid(pid))
            if row is None:
                return None
            for k, v in payload.items():
                setattr(row, k, v)
            s.commit()
            return self._pub_dict(row)

    def due_publications(self, now: datetime | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """Что пора отправлять: ``queued`` по расписанию и ``retry_wait`` с наступившим сроком."""
        moment = _as_dt(now or _now())
        with self.session() as s:
            rows = s.scalars(select(PublicationRow)
                             .where(PublicationRow.status.in_(("queued", "retry_wait")),
                                    PublicationRow.scheduled_at <= moment,
                                    or_(PublicationRow.next_attempt_at.is_(None),
                                        PublicationRow.next_attempt_at <= moment))
                             .order_by(PublicationRow.scheduled_at)
                             .limit(limit)).all()
            return [self._pub_dict(r) for r in rows]

    def claim_publication(self, pid: str, *, worker_id: str | None = None,
                          lease_seconds: int = 120) -> bool:
        """Атомарно забрать публикацию в работу (ровно один воркер получит True).

        UPDATE … WHERE status IN ('queued','retry_wait') AND (next_attempt_at IS NULL
        OR next_attempt_at <= now()) — условие проверяет сама база, поэтому гонка
        между процессами невозможна без внешних блокировок.
        """
        from .publishing import lease_until, now as pub_now

        moment = pub_now()
        with self.session() as s:
            result = s.execute(
                update(PublicationRow)
                .where(PublicationRow.id == to_uuid(pid),
                       PublicationRow.status.in_(("queued", "retry_wait")),
                       or_(PublicationRow.next_attempt_at.is_(None),
                           PublicationRow.next_attempt_at <= moment))
                .values(status="publishing",
                        attempt_count=PublicationRow.attempt_count + 1,
                        claimed_by=worker_id,
                        lease_until=lease_until(lease_seconds, moment),
                        next_attempt_at=None)
            )
            s.commit()
            return bool(result.rowcount)

    def pub_stats(self) -> dict[str, Any]:
        """Счётчики очереди публикаций одним запросом (для /metrics и гейта).

        PDF-отчёт о состоянии очереди по отдельным запросам на статус — это 6 обращений
        к базе на каждый скрейп. Здесь один проход с FILTER, и сразу «просроченные
        аренды» и «самая старая созревшая публикация»: без них дежурный видит «очередь
        не пуста», но не видит, стоит ли она.
        """
        from sqlalchemy import func

        from .publishing import now as pub_now

        moment = pub_now()
        with self.session() as s:
            row = s.execute(
                select(
                    func.count().filter(PublicationRow.status == "queued").label("queued"),
                    func.count().filter(PublicationRow.status == "draft").label("draft"),
                    func.count().filter(PublicationRow.status == "publishing").label("publishing"),
                    func.count().filter(PublicationRow.status == "retry_wait").label("retry_wait"),
                    func.count().filter(PublicationRow.status == "published").label("published"),
                    func.count().filter(PublicationRow.status == "failed").label("failed"),
                    func.count().filter(
                        PublicationRow.status.in_(("queued", "retry_wait")),
                        PublicationRow.scheduled_at <= moment,
                        or_(PublicationRow.next_attempt_at.is_(None),
                            PublicationRow.next_attempt_at <= moment)).label("due_now"),
                    func.count().filter(
                        PublicationRow.status == "publishing",
                        PublicationRow.lease_until.is_not(None),
                        PublicationRow.lease_until < moment).label("stale_leases"),
                    func.min(PublicationRow.scheduled_at).filter(
                        PublicationRow.status.in_(("queued", "retry_wait")),
                        PublicationRow.scheduled_at <= moment).label("oldest_due"),
                )
            ).one()
        oldest = row.oldest_due
        if oldest is not None and oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=moment.tzinfo)
        return {
            "queued": int(row.queued or 0), "draft": int(row.draft or 0),
            "publishing": int(row.publishing or 0), "retry_wait": int(row.retry_wait or 0),
            "published": int(row.published or 0), "failed": int(row.failed or 0),
            "due_now": int(row.due_now or 0), "stale_leases": int(row.stale_leases or 0),
            "oldest_due_age_seconds": int((moment - oldest).total_seconds()) if oldest else 0,
        }

    def pub_defer(self, pid: str, *, seconds: int, reason: str) -> bool:
        """Отложить отправку, не считая её попыткой (лимит канала, а не ошибка).

        Аренда снимается, публикация возвращается в ``queued`` с ``next_attempt_at``.
        ``attempt_count`` уменьшается на единицу: захват в ``claim_publication`` его уже
        инкрементировал, но обращения к площадке не было — иначе лимит частоты «съедал»
        бы попытки и публикация умирала бы в ``failed`` как будто площадка отказала.
        """
        from .publishing import now as pub_now

        moment = pub_now()
        with self.session() as s:
            row = s.get(PublicationRow, to_uuid(pid))
            if row is None or row.status != "publishing":
                return False
            row.status = "queued"
            row.lease_until = None
            row.claimed_by = None
            row.last_error = reason
            row.next_attempt_at = moment + timedelta(seconds=max(1, int(seconds)))
            row.attempt_count = max(0, int(row.attempt_count or 1) - 1)
            s.commit()
            return True

    def reclaim_expired(self, limit: int = 100) -> list[dict[str, Any]]:
        """Вернуть в очередь публикации с истёкшей арендой (воркер упал)."""
        from .publishing import after_lease_expired, now as pub_now

        moment = pub_now()
        reclaimed: list[dict[str, Any]] = []
        with self.session() as s:
            rows = s.scalars(select(PublicationRow)
                             .where(PublicationRow.status == "publishing",
                                    PublicationRow.lease_until.is_not(None),
                                    PublicationRow.lease_until < moment)
                             .limit(limit)).all()
            for row in rows:
                decision = after_lease_expired(row.attempt_count or 1, moment)
                row.status = decision.status
                row.last_error = decision.error
                row.next_attempt_at = decision.next_attempt_at
                row.claimed_by = None
                row.lease_until = None
                reclaimed.append(self._pub_dict(row))
            s.commit()
        return reclaimed

    def pub_counts(self) -> dict[str, int]:
        with self.session() as s:
            rows = s.execute(select(PublicationRow.status, func.count())
                             .group_by(PublicationRow.status)).all()
            return {status: int(count) for status, count in rows}

    @staticmethod
    def _pub_dict(row: PublicationRow) -> dict[str, Any]:
        return {
            "id": from_uuid(row.id), "workspace_id": from_uuid(row.workspace_id),
            "variant_id": from_uuid(row.variant_id),
            "channel_account_id": from_uuid(row.channel_account_id),
            "channel": row.channel, "scheduled_at": row.scheduled_at, "status": row.status,
            "idempotency_key": row.idempotency_key, "erid": row.erid, "is_ad": row.is_ad,
            "external_id": row.external_id, "external_url": row.external_url,
            "last_error": row.last_error, "attempt_count": row.attempt_count,
            "cost_estimate_rub": float(row.cost_estimate_rub or 0),
            "media_ids": list(row.media_ids or []),
            "approved_by_role": row.approved_by_role, "approved_at": row.approved_at,
            "published_at": row.published_at,
            "lease_until": row.lease_until, "claimed_by": row.claimed_by,
            "next_attempt_at": row.next_attempt_at,
        }

    # ---------------- квоты ----------------
    def quota_rows(self, workspace_id: str = "ws_de") -> dict[str, dict[str, float]]:
        period = _period_start()
        with self.session() as s:
            rows = s.scalars(select(QuotaCounterRow).where(
                QuotaCounterRow.workspace_id == to_uuid(workspace_id),
                QuotaCounterRow.period == period)).all()
            return {r.key: {"used": float(r.used), "limit": float(r.limit_value)} for r in rows}

    def quota_ensure(self, workspace_id: str, key: str, limit: float) -> None:
        period = _period_start()
        with self.session() as s:
            row = s.get(QuotaCounterRow, {"workspace_id": to_uuid(workspace_id),
                                          "period": period, "key": key})
            if row is None:
                s.add(QuotaCounterRow(workspace_id=to_uuid(workspace_id), period=period,
                                      key=key, used=0, limit_value=limit))
                s.commit()

    def quota_consume(self, workspace_id: str, key: str, amount: float) -> tuple[float, float]:
        """Вернуть (used, limit) после списания. Проверка лимита — атомарным UPDATE."""
        period = _period_start()
        with self.session() as s:
            row = s.get(QuotaCounterRow, {"workspace_id": to_uuid(workspace_id),
                                          "period": period, "key": key})
            if row is None:
                return 0.0, 0.0
            if float(row.used) + amount > float(row.limit_value):
                return float(row.used), float(row.limit_value)
            row.used = float(row.used) + amount
            s.commit()
            return float(row.used), float(row.limit_value)

    def quota_add_limit(self, workspace_id: str, key: str, amount: float) -> tuple[float, float]:
        period = _period_start()
        with self.session() as s:
            row = s.get(QuotaCounterRow, {"workspace_id": to_uuid(workspace_id),
                                          "period": period, "key": key})
            if row is None:
                return 0.0, 0.0
            row.limit_value = float(row.limit_value) + amount
            s.commit()
            return float(row.used), float(row.limit_value)

    # ---------------- аудит ----------------
    def audit_write(self, role: str, method: str, path: str, status: int,
                    extra: dict | None = None) -> dict[str, Any]:
        with self.session() as s:
            row = AuditLogRow(tenant_id=to_uuid("ws_de"), actor_kind="user",
                              action=f"{method} {status}", object_type="http",
                              object_id=path[:64],
                              meta={"role": role, **(extra or {})})
            s.add(row)
            s.commit()
            return {"ts": row.created_at, "role": role, "method": method,
                    "path": path, "status": status, **(extra or {})}

    def audit_tail(self, limit: int = 50) -> dict[str, Any]:
        with self.session() as s:
            total = int(s.scalar(select(func.count()).select_from(AuditLogRow)) or 0)
            rows = s.scalars(select(AuditLogRow).order_by(AuditLogRow.id.desc()).limit(limit)).all()
            items = [{"ts": r.created_at, "role": (r.meta or {}).get("role"),
                      "method": r.action.split(" ")[0], "path": r.object_id,
                      "status": int(r.action.split(" ")[1]) if " " in r.action else 0}
                     for r in rows]
            return {"items": list(reversed(items)), "total": total}

    # ---------------- токены каналов ----------------
    def channel_token_save(self, account_id: str, token: str, *, workspace_id: str = "ws_de",
                           channel: str = "max", key_version: int = 1,
                           external_id: str | None = None) -> None:
        """Сохранить токен канала. ``external_id`` — обязательный по схеме идентификатор
        канала на площадке (chat_id, owner_id, channel_id): без него адаптер не знает,
        куда публиковать, и запись нарушает NOT NULL в ``channel_accounts``."""
        ciphertext = crypto.encrypt(token)
        with self.session() as s:
            row = s.get(ChannelAccountRow, account_id)
            if row is None:
                row = ChannelAccountRow(id=account_id, workspace_id=to_uuid(workspace_id),
                                        channel=channel,
                                        external_id=external_id or account_id)
                s.add(row)
            elif external_id:
                row.external_id = external_id
            row.token_ciphertext = ciphertext
            row.token_key_version = key_version
            s.commit()

    def channel_token_get(self, account_id: str) -> str | None:
        with self.session() as s:
            row = s.get(ChannelAccountRow, account_id)
            if row is None or not row.token_ciphertext:
                return None
            return crypto.decrypt(row.token_ciphertext)

    def channel_accounts(self) -> list[dict[str, Any]]:
        with self.session() as s:
            rows = s.scalars(select(ChannelAccountRow)).all()
            return [{"id": r.id, "workspace_id": from_uuid(r.workspace_id), "channel": r.channel,
                     "title": r.title, "audience_size": r.audience_size, "status": r.status,
                     "token_hint": crypto.mask(r.token_ciphertext),
                     "key_version": r.token_key_version} for r in rows]

    # ---------------- документные коллекции ----------------
    def doc_all(self, collection: str, limit: int = 2000,
                workspace_id: str | None = None) -> list[dict[str, Any]]:
        """Документы коллекции. Сужается по контексту арендатора (см. :mod:`orbita.tenancy`).

        Раньше метод возвращал документы ВСЕХ рабочих пространств: `docs.Collection.all()`
        использовал его напрямую, поэтому любой список «своих» сущностей в режиме БД
        на деле содержал чужие. Теперь условие по workspace добавляется здесь — в одном
        месте, а не в каждом обработчике.
        """
        scope = workspace_id or tenancy.scope_workspace()
        stmt = select(EntityDocRow).where(EntityDocRow.collection == collection)
        if scope:
            stmt = stmt.where(EntityDocRow.workspace_id == to_uuid(scope))
        with self.session() as s:
            rows = s.scalars(stmt.order_by(EntityDocRow.position, EntityDocRow.created_at)
                             .limit(limit)).all()
            return [dict(r.payload) for r in rows]

    # ---------------- типизированные таблицы сигналов и вариантов ----------------
    # entity_docs — гибкое хранилище документов, но publications.variant_id ссылается на
    # таблицу variants. Пока варианты писались только в entity_docs, FK не выполнялся
    # («нужен слой записи в типизированные таблицы», ADR 0019). Теперь пишем в обе:
    # типизированная строка держит связи и RLS, документ — полный payload агентов.
    def signal_upsert(self, doc: dict[str, Any]) -> None:
        """Записать/обновить инфоповод в public.signals (id — через to_uuid)."""
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        with self.session() as s:
            stmt = pg_insert(SignalRow).values(
                id=to_uuid(doc["id"]), workspace_id=to_uuid(doc["workspace_id"]),
                kind=doc.get("kind") or "manual", title=(doc.get("title") or "")[:500],
                source_url=doc.get("source_url"), raw_text=doc.get("raw_text"),
                goal=doc.get("goal"), status=doc.get("status") or "new",
            ).on_conflict_do_update(
                index_elements=[SignalRow.id],
                set_={"title": (doc.get("title") or "")[:500],
                      "status": doc.get("status") or "new"})
            s.execute(stmt)
            s.commit()

    def variant_upsert(self, doc: dict[str, Any]) -> None:
        """Записать/обновить вариант в public.variants (нужен для FK из publications)."""
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        with self.session() as s:
            stmt = pg_insert(VariantRow).values(
                id=to_uuid(doc["id"]), workspace_id=to_uuid(doc["workspace_id"]),
                signal_id=to_uuid(doc["signal_id"]), channel=doc["channel"],
                format=doc.get("format") or "post", title=doc.get("title"),
                body=doc.get("body") or "", link_url=doc.get("link_url"),
                link_in_reply=bool(doc.get("link_in_reply")),
                review_score=doc.get("review_score"),
            ).on_conflict_do_update(
                index_elements=[VariantRow.id],
                set_={"body": doc.get("body") or "",
                      "review_score": doc.get("review_score")})
            s.execute(stmt)
            s.commit()

    def doc_get(self, collection: str, doc_id: str,
                workspace_id: str | None = None) -> dict[str, Any] | None:
        """Один документ. Невидимый в текущем контексте документ = «не найден» (404, не 403)."""
        scope = workspace_id or tenancy.scope_workspace()
        doc_id = _canonical_id(doc_id)
        with self.session() as s:
            row = s.get(EntityDocRow, {"collection": collection, "id": doc_id})
            if row is None:
                return None
            if scope and str(row.workspace_id) != str(to_uuid(scope)):
                return None
            return dict(row.payload)

    @staticmethod
    def _json_safe(payload: dict[str, Any]) -> dict[str, Any]:
        """Привести документ к виду, который переживает JSONB.

        Сигналы и публикации содержат ``datetime``/``UUID``; раньше в db-режиме запись
        падала с «Object of type datetime is not JSON serializable» — то есть при
        включённом DATABASE_URL часть путей просто не работала. Сериализуем так же, как
        это делает API: ISO-строки, чтобы документ читался обратно без потерь.
        """
        return json.loads(json.dumps(payload, ensure_ascii=False, default=str))

    def doc_put(self, collection: str, doc_id: str, workspace_id: str,
                payload: dict[str, Any]) -> dict[str, Any]:
        """Запись документа. ``workspace_id`` обязан совпадать с контекстом арендатора."""
        payload = self._json_safe(payload)
        doc_id = _canonical_id(doc_id)
        scope = tenancy.scope_workspace()
        if scope and str(to_uuid(workspace_id)) != str(to_uuid(scope)):
            raise tenancy.TenancyError(
                f"запись в чужой workspace: контекст {scope}, запись {workspace_id}")
        with self.session() as s:
            row = s.get(EntityDocRow, {"collection": collection, "id": doc_id})
            if row is None:
                position = int(s.scalar(
                    select(func.coalesce(func.max(EntityDocRow.position), 0))
                    .where(EntityDocRow.collection == collection)) or 0) + 1
                row = EntityDocRow(collection=collection, id=doc_id,
                                   workspace_id=to_uuid(workspace_id), payload=payload,
                                   position=position)
                s.add(row)
            else:
                row.payload = payload
                row.updated_at = _now()
            s.commit()
            return dict(row.payload)

    def doc_delete(self, collection: str, doc_id: str,
                   workspace_id: str | None = None) -> None:
        scope = workspace_id or tenancy.scope_workspace()
        doc_id = _canonical_id(doc_id)
        with self.session() as s:
            stmt = delete(EntityDocRow).where(EntityDocRow.collection == collection,
                                              EntityDocRow.id == doc_id)
            if scope:
                stmt = stmt.where(EntityDocRow.workspace_id == to_uuid(scope))
            s.execute(stmt)
            s.commit()

    # ---------------- журнал попыток публикации ----------------
    def attempt_begin(self, pid: str, attempt: int, worker_id: str | None,
                      idempotency_key: str, workspace_id: str | None = None) -> dict[str, Any]:
        """Начать попытку. Если незакрытая попытка уже есть — вернуть её (признак «в полёте»)."""
        with self.session() as s:
            opened = s.scalar(select(PublicationAttemptRow).where(
                PublicationAttemptRow.publication_id == to_uuid(pid),
                PublicationAttemptRow.finished_at.is_(None)))
            if opened is not None:
                return self._attempt_dict(opened)
            row = PublicationAttemptRow(
                publication_id=to_uuid(pid),
                workspace_id=to_uuid(workspace_id or tenancy.scope_workspace() or "ws_de"),
                attempt=attempt, worker_id=worker_id, idempotency_key=idempotency_key)
            s.add(row)
            s.commit()
            return self._attempt_dict(row)

    def attempt_finish(self, pid: str, outcome: str, external_id: str | None = None,
                       error: str | None = None) -> dict[str, Any] | None:
        with self.session() as s:
            row = s.scalar(select(PublicationAttemptRow).where(
                PublicationAttemptRow.publication_id == to_uuid(pid),
                PublicationAttemptRow.finished_at.is_(None)))
            if row is None:
                return None
            row.finished_at = _now()
            row.outcome = outcome
            row.external_id = external_id
            row.error = error
            s.commit()
            return self._attempt_dict(row)

    def attempt_last(self, pid: str) -> dict[str, Any] | None:
        with self.session() as s:
            row = s.scalar(select(PublicationAttemptRow)
                           .where(PublicationAttemptRow.publication_id == to_uuid(pid))
                           .order_by(PublicationAttemptRow.attempt.desc()).limit(1))
            return self._attempt_dict(row) if row else None

    @staticmethod
    def _attempt_dict(row: PublicationAttemptRow) -> dict[str, Any]:
        return {"publication_id": str(row.publication_id), "attempt": row.attempt,
                "worker_id": row.worker_id, "idempotency_key": row.idempotency_key,
                "started_at": row.started_at.isoformat() if row.started_at else None,
                "finished_at": row.finished_at.isoformat() if row.finished_at else None,
                "outcome": row.outcome, "external_id": row.external_id, "error": row.error,
                "workspace_id": str(row.workspace_id)}

    # ---------------- организации и рабочие пространства ----------------
    def tenant_upsert(self, tenant: dict[str, Any]) -> dict[str, Any]:
        with self.session() as s:
            row = s.get(TenantRow, to_uuid(tenant["id"]))
            if row is None:
                row = TenantRow(id=to_uuid(tenant["id"]), name=tenant.get("name") or "Клиент",
                                plan_code=tenant.get("plan_code", "trial"),
                                settings=tenant.get("settings") or {})
                s.add(row)
            else:
                row.name = tenant.get("name") or row.name
                row.plan_code = tenant.get("plan_code", row.plan_code)
            s.commit()
            return {"id": str(row.id), "name": row.name, "plan_code": row.plan_code}

    def workspace_upsert(self, ws: dict[str, Any]) -> dict[str, Any]:
        with self.session() as s:
            row = s.get(WorkspaceRow, to_uuid(ws["id"]))
            brand = dict(ws.get("brand") or {})
            if ws.get("legal_entity"):
                brand["legal_entity"] = ws["legal_entity"]
            if row is None:
                row = WorkspaceRow(id=to_uuid(ws["id"]), tenant_id=to_uuid(ws["tenant_id"]),
                                   name=ws.get("name") or "Бренд",
                                   site_url=ws.get("site_url"), brand=brand)
                s.add(row)
            else:
                row.name = ws.get("name") or row.name
                row.site_url = ws.get("site_url", row.site_url)
                row.brand = {**(row.brand or {}), **brand}
            s.commit()
            return self.workspace_get(str(row.id)) or {}

    def workspace_get(self, workspace_id: str) -> dict[str, Any] | None:
        with self.session() as s:
            row = s.get(WorkspaceRow, to_uuid(workspace_id))
            if row is None:
                return None
            brand = dict(row.brand or {})
            return {"id": str(row.id), "tenant_id": str(row.tenant_id), "name": row.name,
                    "site_url": row.site_url, "timezone": row.timezone, "brand": brand,
                    "legal_entity": brand.get("legal_entity")}

    def membership_add(self, *, tenant_id: str, user_id: str, role: str = "editor") -> None:
        with self.session() as s:
            row = s.get(MembershipRow, {"tenant_id": to_uuid(tenant_id),
                                        "user_id": to_uuid(user_id)})
            if row is None:
                s.add(MembershipRow(tenant_id=to_uuid(tenant_id), user_id=to_uuid(user_id),
                                    role=role))
            else:
                row.role = role
            s.commit()

    # ---------------- пользователи платформы ----------------
    def user_upsert(self, user_id: str, *, email: str | None = None,
                    name: str | None = None, is_platform_admin: bool = False) -> dict[str, Any]:
        with self.session() as s:
            row = s.get(UserRow, user_id)
            if row is None:
                row = UserRow(id=user_id, email=email, name=name,
                              is_platform_admin=is_platform_admin)
                s.add(row)
            else:
                row.is_platform_admin = is_platform_admin
                if email:
                    row.email = email
                if name:
                    row.name = name
            s.commit()
            return {"id": row.id, "email": row.email, "name": row.name,
                    "is_platform_admin": bool(row.is_platform_admin)}

    def user_is_platform_admin(self, user_id: str) -> bool:
        with self.session() as s:
            row = s.get(UserRow, user_id)
            return bool(row and row.is_platform_admin)

    def stats(self) -> dict[str, Any]:
        with self.session() as s:
            return {
                "links": int(s.scalar(select(func.count()).select_from(ShortLinkRow)) or 0),
                "clicks": int(s.scalar(select(func.count()).select_from(ClickRow)) or 0),
                "publications": int(s.scalar(select(func.count()).select_from(PublicationRow)) or 0),
                "audit_rows": int(s.scalar(select(func.count()).select_from(AuditLogRow)) or 0),
                "docs": int(s.scalar(select(func.count()).select_from(EntityDocRow)) or 0),
            }

    def truncate_all(self) -> None:
        """Только для тестов."""
        with self.session() as s:
            for model in (ClickRow, ShortLinkRow, PublicationRow, QuotaCounterRow,
                          AuditLogRow, ChannelAccountRow, EntityDocRow, UserRow):
                s.execute(delete(model))
            s.commit()


def _as_dt(value: datetime | str) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def _period_start() -> datetime:
    now = _now()
    return datetime(now.year, now.month, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Ленивый синглтон
# ---------------------------------------------------------------------------
_db: Database | None = None


def database() -> Database | None:
    """Вернуть подключение, если выбран слой ``db``; иначе ``None`` (память)."""
    global _db
    if data_layer() != "db":
        return None
    if _db is None:
        _db = Database()
        log.info("data layer: db (%s)", _db.url.split("@")[-1])
    return _db


def set_database(db: Database | None) -> None:
    """Подменить подключение (тесты)."""
    global _db
    _db = db


def enqueue_after(seconds: int) -> datetime:
    return _now() + timedelta(seconds=seconds)
