"""Интеграционные тесты на РЕАЛЬНЫХ PostgreSQL и Redis.

Зачем отдельный файл: SQLite и память процесса не проверяют ни типы колонок, ни enum,
ни RLS, ни атомарность Redis. Именно поэтому первый аудит и был прав в главном —
«зелёные тесты ничего не доказывают», пока не выполнены на целевой инфраструктуре.

Запуск:

    export ORBITA_TEST_PG_URL="postgresql+psycopg://orbita_app:пароль@127.0.0.1:5432/orbita"
    export ORBITA_TEST_REDIS_URL="redis://:пароль@127.0.0.1:6379/0"
    PYTHONPATH=src pytest tests/test_pg_integration.py -v

Без переменных окружения тесты помечаются `skip` с явной причиной: «нечем проверить»
отличается от «проверено и работает».

Схема теста: **два арендатора** (клиента) — у каждого своё пространство и свой пользователь.
Граница изоляции в БД — арендатор: это разные клиенты. Несколько брендов внутри одного
арендатора видны друг другу осознанно (человек владеет ими и переключается между ними),
а разделение внутри арендатора делает прикладной слой через `tenancy.scope_workspace()`.

Контекст запроса ставится так же, как это делает `persistence.Database`: четыре
переменные `app.tenant_id / app.workspace_id / app.user_id / app.actor_role`.
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DataError, IntegrityError, ProgrammingError

PG_URL = os.getenv("ORBITA_TEST_PG_URL", "")
REDIS_URL = os.getenv("ORBITA_TEST_REDIS_URL", "")

pytestmark = pytest.mark.skipif(
    not PG_URL, reason="ORBITA_TEST_PG_URL не задан: типы, enum и RLS проверяются только "
                       "на реальной PostgreSQL — на SQLite RLS отсутствует как механизм")


# ---------------------------------------------------------------- фикстуры
@pytest.fixture(scope="module")
def engine():
    engine = create_engine(PG_URL, future=True)
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def world(engine):
    """Два арендатора: клиент A и клиент B. У каждого пространство, владелец и канал."""
    tenant = str(uuid.uuid4())
    ws_a, ws_b = str(uuid.uuid4()), str(uuid.uuid4())
    user = str(uuid.uuid4())
    with engine.begin() as conn:
        _platform(conn)
        conn.execute(text("INSERT INTO tenants (id, name) VALUES (:i, :n)"),
                     {"i": tenant, "n": f"Тест {tenant[:8]}"})
        for ws, name in ((ws_a, "Бренд A"), (ws_b, "Бренд B")):
            conn.execute(text("INSERT INTO workspaces (id, tenant_id, name) VALUES (:i, :t, :n)"),
                         {"i": ws, "t": tenant, "n": name})
        conn.execute(text("INSERT INTO users (id, email) VALUES (:i, :e)"),
                     {"i": user, "e": f"owner-{user[:8]}@example.ru"})
        conn.execute(text(
            "INSERT INTO memberships (tenant_id, user_id, role) "
            "VALUES (:t, :u, 'owner')"), {"t": tenant, "u": user})
        for ws in (ws_a, ws_b):
            conn.execute(text(
                "INSERT INTO channel_accounts (id, workspace_id, channel, external_id) "
                "VALUES (:i, :w, 'vk', :e)"),
                {"i": str(uuid.uuid4()), "w": ws, "e": f"vk-{ws[:8]}"})

        # второй арендатор — отдельный клиент
        tenant_b, ws_c, user_b = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
        conn.execute(text("INSERT INTO tenants (id, name) VALUES (:i, :n)"),
                     {"i": tenant_b, "n": f"Клиент B {tenant_b[:8]}"})
        conn.execute(text("INSERT INTO workspaces (id, tenant_id, name) VALUES (:i, :t, :n)"),
                     {"i": ws_c, "t": tenant_b, "n": "Бренд клиента B"})
        conn.execute(text("INSERT INTO users (id, email) VALUES (:i, :e)"),
                     {"i": user_b, "e": f"owner-b-{user_b[:8]}@example.ru"})
        conn.execute(text("INSERT INTO memberships (tenant_id, user_id, role) "
                          "VALUES (:t, :u, 'owner')"), {"t": tenant_b, "u": user_b})
        conn.execute(text(
            "INSERT INTO channel_accounts (id, workspace_id, channel, external_id) "
            "VALUES (:i, :w, 'vk', :e)"),
            {"i": str(uuid.uuid4()), "w": ws_c, "e": f"vk-{ws_c[:8]}"})
    return {"tenant": tenant, "a": ws_a, "b": ws_b, "user": user,
            "tenant_b": tenant_b, "foreign_ws": ws_c, "foreign_user": user_b}


def _platform(conn) -> None:
    """Платформенный режим: роль платформы, контекст арендатора не задан."""
    conn.execute(text("SELECT set_config('app.actor_role', 'platform', true)"))


def _as_workspace(conn, ws: str, *, tenant: str, user: str, role: str = "owner") -> None:
    """Контекст запроса — как его выставляет persistence.Database на каждой транзакции."""
    for name, value in (("app.tenant_id", tenant), ("app.workspace_id", ws),
                        ("app.user_id", user), ("app.actor_role", role)):
        conn.execute(text("SELECT set_config(:n, :v, true)"), {"n": name, "v": value})


def _make_publication(conn, ws: str, status: str = "queued") -> str:
    """Полная цепочка «сигнал → вариант → публикация»: FK не даёт создать публикацию иначе."""
    signal, variant = str(uuid.uuid4()), str(uuid.uuid4())
    account = conn.scalar(text(
        "SELECT id::text FROM channel_accounts WHERE workspace_id = :w LIMIT 1"), {"w": ws})
    conn.execute(text(
        "INSERT INTO signals (id, workspace_id, kind, title) "
        "VALUES (:i, :w, 'manual', 'Проверка')"), {"i": signal, "w": ws})
    conn.execute(text(
        "INSERT INTO variants (id, workspace_id, signal_id, channel, format, body) "
        "VALUES (:i, :w, :s, 'vk', 'post', 'Текст варианта для интеграционного теста.')"),
        {"i": variant, "w": ws, "s": signal})
    pub = str(uuid.uuid4())
    conn.execute(text(
        "INSERT INTO publications (id, workspace_id, variant_id, channel_account_id, "
        "scheduled_at, status, idempotency_key) "
        "VALUES (:i, :w, :v, :a, now(), :st, :k)"),
        {"i": pub, "w": ws, "v": variant, "a": account, "st": status,
         "k": f"idem-{uuid.uuid4()}"})
    return pub


# ---------------------------------------------------------------- схема на живой базе
def test_migration_chain_is_at_head(engine):
    with engine.connect() as conn:
        version = conn.scalar(text("SELECT version_num FROM alembic_version"))
    assert version == "0006_rls_strict", f"база не на последней ревизии: {version}"


def test_required_extensions_are_installed(engine):
    with engine.connect() as conn:
        names = set(conn.scalars(text("SELECT extname FROM pg_extension")).all())
    for required in ("vector", "citext", "pgcrypto"):
        assert required in names, f"нет расширения {required}: схема не поднимется"


def test_publication_status_enum_contains_retry_wait(engine):
    """Тип в базе — не текст: ретрай возможен только если значение есть в enum."""
    with engine.connect() as conn:
        values = set(conn.scalars(text(
            "SELECT e.enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
            "WHERE t.typname = 'publication_status'")).all())
    assert "retry_wait" in values
    assert {"draft", "queued", "publishing", "published", "failed"} <= values


def test_email_column_is_citext(engine):
    """citext — регистронезависимая уникальность: иначе Ivan@ и ivan@ это разные клиенты."""
    with engine.connect() as conn:
        kind = conn.scalar(text(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name = 'users' AND column_name = 'email'"))
    assert kind == "USER-DEFINED"
    marker = uuid.uuid4().hex[:8]
    with engine.begin() as conn:                 # users закрыт политикой: пишем платформой
        _platform(conn)
        conn.execute(text("INSERT INTO users (id, email) VALUES (:i, :e)"),
                     {"i": str(uuid.uuid4()), "e": f"Citext.{marker}@Example.ru"})
        with pytest.raises(IntegrityError):
            conn.execute(text("INSERT INTO users (id, email) VALUES (:i, :e)"),
                         {"i": str(uuid.uuid4()), "e": f"citext.{marker}@example.RU"})


def test_invalid_status_is_rejected_by_database(engine, world):
    """Машина состояний подкреплена типом: «published_by_hand» в базу не запишется."""
    with engine.connect() as conn:
        _as_workspace(conn, world["a"], tenant=world["tenant"], user=world["user"])
        with pytest.raises((ProgrammingError, IntegrityError, DataError)):
            _make_publication(conn, world["a"], status="published_by_hand")
            conn.commit()


def test_baseline_is_idempotent(engine):
    """Повторный прогон schema.sql/rls/v3 на живой базе не меняет её и не падает."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    with engine.begin() as conn:
        for name in ("schema.sql", "sql/rls_rbac.sql", "sql/v3.sql"):
            conn.exec_driver_sql((root / name).read_text(encoding="utf-8"))
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM information_schema.tables "
                               "WHERE table_schema = 'public'")) > 30


# ---------------------------------------------------------------- RLS на живой базе
def test_rls_hides_rows_of_another_tenant(engine, world):
    """Главная проверка аудита: клиент A не видит строки клиента B на уровне базы."""
    with engine.begin() as conn:
        _platform(conn)
        _make_publication(conn, world["a"])
        _make_publication(conn, world["foreign_ws"])

    with engine.connect() as conn:
        _as_workspace(conn, world["a"], tenant=world["tenant"], user=world["user"])
        rows = set(conn.scalars(text("SELECT workspace_id::text FROM publications")).all())
    assert rows, "своя публикация не видна — проверка недостоверна"
    assert rows == {world["a"]}, f"видны строки чужого клиента: {rows - {world['a']}}"

    with engine.connect() as conn:            # и наоборот: клиент B не видит A
        _as_workspace(conn, world["foreign_ws"], tenant=world["tenant_b"],
                      user=world["foreign_user"])
        foreign = set(conn.scalars(text("SELECT workspace_id::text FROM publications")).all())
    assert foreign == {world["foreign_ws"]}, f"клиент B видит чужие данные: {foreign}"


def test_rls_blocks_write_into_foreign_tenant(engine, world):
    with engine.connect() as conn:
        _as_workspace(conn, world["a"], tenant=world["tenant"], user=world["user"])
        with pytest.raises(ProgrammingError):
            _make_publication(conn, world["foreign_ws"])
            conn.commit()


def test_rls_denies_without_context(engine, world):
    """Без контекста арендатора (и без роли platform) строк нет: «забыли привязать» ≠ «видно всё»."""
    with engine.connect() as conn:
        conn.execute(text("SELECT set_config('app.actor_role', 'editor', true)"))
        count = conn.scalar(text("SELECT count(*) FROM publications"))
    assert count == 0


def test_rls_policy_uses_request_context(engine):
    with engine.connect() as conn:
        body = conn.scalar(text(
            "SELECT pg_get_functiondef('can_access_workspace(uuid)'::regprocedure)"))
    assert "app_user_id" in body and "app_tenant_id" in body


def test_workspace_isolation_policy_exists_on_every_tenant_table(engine):
    """Проверка не «есть ли RLS», а «на всех ли таблицах клиента он включён»."""
    expected = {"publications", "signals", "variants", "briefs", "inbox_threads",
                "channel_accounts", "entity_docs", "assets", "knowledge_sources",
                "analytics_events", "publication_attempts"}
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind = 'r'")).all()
    flags = {name: (enabled, forced) for name, enabled, forced in rows}
    missing = [t for t in expected if t in flags and not flags[t][0]]
    assert not missing, f"RLS не включён: {missing}"
    not_forced = [t for t in ("publications", "signals", "variants") if flags.get(t, (0, 0))[1] == 0]
    assert not not_forced, f"RLS не принудительный (владелец таблицы обойдёт): {not_forced}"


def test_context_is_set_inside_transaction(engine, world):
    """persistence.Database обязан выставлять app.* в каждой транзакции — проверяем фактом."""
    from orbita import persistence, tenancy

    db = persistence.Database(PG_URL)
    with tenancy.bind(world["a"], tenant_id=world["tenant"], user_id=world["user"], role="owner"):
        with db.session() as session:
            values = {
                name: session.execute(
                    text(f"SELECT current_setting('{name}', true)")).scalar()
                for name in ("app.workspace_id", "app.tenant_id", "app.user_id", "app.actor_role")
            }
    assert values == {"app.workspace_id": world["a"], "app.tenant_id": world["tenant"],
                      "app.user_id": world["user"], "app.actor_role": "owner"}


def test_without_context_role_is_platform(engine):
    """Вне запроса контекст = платформа, а не пустые строки: политики различают эти случаи."""
    from orbita import persistence

    db = persistence.Database(PG_URL)
    with db.session() as session:
        role = session.execute(text("SELECT current_setting('app.actor_role', true)")).scalar()
    assert role == "platform"


def test_db_layer_roundtrip_through_orm(engine, world):
    """Полный путь приложения через ORM на живой базе: создать → прочитать → скрыть чужое."""
    from orbita import persistence, tenancy

    db = persistence.Database(PG_URL)
    variant = str(uuid.uuid4())
    signal = str(uuid.uuid4())
    with tenancy.bind(world["a"], tenant_id=world["tenant"], user_id=world["user"], role="owner"):
        with db.session() as session:
            session.execute(text("INSERT INTO signals (id, workspace_id, kind, title) "
                                 "VALUES (:i, :w, 'manual', 'ORM')"),
                            {"i": signal, "w": world["a"]})
            session.execute(text(
                "INSERT INTO variants (id, workspace_id, signal_id, channel, format, body) "
                "VALUES (:i, :w, :s, 'vk', 'post', 'Текст для проверки ORM-слоя на живой базе.')"),
                {"i": variant, "w": world["a"], "s": signal})
            session.commit()
        with db.session() as session:
            account = session.execute(text(
                "SELECT id::text FROM channel_accounts WHERE workspace_id = :w LIMIT 1"),
                {"w": world["a"]}).scalar()
        pub = db.pub_create({
            "id": str(uuid.uuid4()), "workspace_id": world["a"], "variant_id": variant,
            "channel_account_id": account, "channel": "vk",
            "scheduled_at": "2026-11-18T10:00:00+00:00", "status": "queued",
            "idempotency_key": f"orm-{uuid.uuid4()}", "is_ad": False,
        })
    assert pub["workspace_id"] == world["a"]
    assert db.pub_get(pub["id"]) is not None

    # клиент A продолжает видеть свой бренд A и бренд B (один арендатор — один клиент),
    # а клиент B (другой арендатор) не видит ничего
    with tenancy.bind(world["b"], tenant_id=world["tenant"], user_id=world["user"], role="owner"):
        assert db.pub_get(pub["id"]) is not None, "свой арендатор должен видеть оба бренда"
    with tenancy.bind(world["foreign_ws"], tenant_id=world["tenant_b"],
                      user_id=world["foreign_user"], role="owner"):
        assert db.pub_get(pub["id"]) is None, "публикация видна другому клиенту"


# ---------------------------------------------------------------- журнал попыток
def test_only_one_inflight_attempt_per_publication(engine, world):
    """Вторая открытая попытка невозможна: это и есть защита от двойной отправки."""
    with engine.begin() as conn:
        _platform(conn)
        pid = _make_publication(conn, world["a"], status="publishing")
        conn.execute(text("INSERT INTO publication_attempts (publication_id, workspace_id, attempt) "
                          "VALUES (:p, :w, 1)"), {"p": pid, "w": world["a"]})
    with engine.connect() as conn:
        _as_workspace(conn, world["a"], tenant=world["tenant"], user=world["user"])
        with pytest.raises(IntegrityError):
            conn.execute(text("INSERT INTO publication_attempts (publication_id, workspace_id, attempt) "
                              "VALUES (:p, :w, 2)"), {"p": pid, "w": world["a"]})
            conn.commit()


def test_attempt_journal_roundtrip_via_orm(engine, world):
    from orbita import persistence, tenancy

    db = persistence.Database(PG_URL)
    with tenancy.bind(world["a"], tenant_id=world["tenant"], user_id=world["user"], role="owner"):
        with db.session() as session:
            pid = _make_publication(session, world["a"])
            session.commit()
        db.attempt_begin(pid, 1, "worker-1", "idem-1", workspace_id=world["a"])
        opened = db.attempt_last(pid)
        db.attempt_finish(pid, "published", external_id="ext-1")
        closed = db.attempt_last(pid)
    assert opened and opened["attempt"] == 1 and opened["outcome"] is None
    assert closed["outcome"] == "published" and closed["external_id"] == "ext-1"


# ---------------------------------------------------------------- Redis
redis_only = pytest.mark.skipif(not REDIS_URL, reason="ORBITA_TEST_REDIS_URL не задан")


@redis_only
def test_refresh_rotation_is_atomic_on_redis(monkeypatch):
    """Восемь параллельных refresh → одна ротация. На памяти это делала блокировка,
    на Redis — скрипт compare-and-swap; проверяем именно Redis-путь."""
    import concurrent.futures as cf

    from orbita import sessions

    monkeypatch.setenv("REDIS_URL", REDIS_URL)
    monkeypatch.setattr(sessions, "_REDIS", None, raising=False)
    store = sessions._redis()
    assert store is not None, "Redis не подключился — тест недостоверен"
    # Чистим админским подключением (базовый пароль), а не ролью приложения: у роли
    # orbita_api нет FLUSHDB — и это правильно (см. infra.redis_acl в гейте).
    import redis as _redis_lib

    admin = (os.getenv("REDIS_ADMIN_URL") or os.getenv("REDIS_URL") or REDIS_URL)
    _redis_lib.Redis.from_url(admin).flushdb()

    pair = sessions.issue(f"u_{uuid.uuid4().hex[:8]}", "redis@example.ru")
    token = pair["refresh_token"]

    def attempt(_):
        return sessions.refresh(token).get("code") or "ok"

    with cf.ThreadPoolExecutor(8) as pool:
        results = list(pool.map(attempt, range(8)))
    assert results.count("ok") == 1, f"ротаций больше одной: {results}"
    assert "refresh_reuse" in results, f"повтор не распознан как кража: {results}"


@redis_only
def test_session_index_allows_logout_everywhere(monkeypatch):
    from orbita import sessions

    monkeypatch.setenv("REDIS_URL", REDIS_URL)
    monkeypatch.setattr(sessions, "_REDIS", None, raising=False)
    user = f"u_{uuid.uuid4().hex[:8]}"
    sessions.issue(user, "multi@example.ru")
    sessions.issue(user, "multi@example.ru")
    assert sessions.revoke_all(user)["revoked"] >= 1


@redis_only
def test_arq_accepts_jobs_on_real_redis():
    """Очередь — ARQ на Redis, а не таймер в процессе."""
    import asyncio

    from arq import create_pool
    from arq.connections import RedisSettings

    from orbita.workers import run

    async def scenario():                     # ArqRedis сам наследует redis-клиент
        pool = await create_pool(RedisSettings.from_dsn(REDIS_URL))
        before = await pool.zcard("arq:queue")
        job = await pool.enqueue_job("publish_due", _queue_name="arq:queue")
        after = await pool.zcard("arq:queue")
        await pool.aclose()
        return before, after, job

    before, after, job = asyncio.run(scenario())
    assert job is not None and after > before
    assert run.WorkerSettings.functions, "в WorkerSettings нет зарегистрированных задач"


# ---------------------------------------------------------------- сквозной путь на живой БД
def test_end_to_end_generation_and_publication_on_live_db(engine, world, monkeypatch):
    """Сквозной сценарий на РЕАЛЬНОЙ базе: сигнал → генерация → публикация → журнал.

    Это ответ на пункт аудита «сквозная генерация и публикация для произвольного
    пространства»: проверяется не наличие функций, а что пост доходит до адаптера и
    фиксируется в журнале попыток именно в пространстве клиента.
    """
    import asyncio
    from datetime import datetime, timedelta, timezone

    from orbita import generator, persistence, store, tenancy
    from orbita.adapters.base import PublishResult
    from orbita.workers import tasks

    monkeypatch.setenv("DATA_LAYER", "db")
    # слой данных подключаем напрямую: settings кэширует DATABASE_URL на импорте
    live_db = persistence.Database(PG_URL)
    persistence.set_database(live_db)
    monkeypatch.setattr(persistence, "data_layer", lambda: "db")

    sent: list[dict] = []

    class Stub:
        async def publish(self, payload, idempotency_key):
            sent.append({"text": payload.text, "key": idempotency_key})
            return PublishResult(external_id=f"live_{idempotency_key[:8]}",
                                 external_url="https://example.ru/post")

    monkeypatch.setattr(tasks, "get_adapter", lambda channel, token=None: Stub())

    ws, tenant, user = world["a"], world["tenant"], world["user"]
    with tenancy.bind(ws, tenant_id=tenant, user_id=user, role="owner"):
        signal = store.create_signal("manual", "Скидка 30% на годовое обслуживание",
                                     None, None, "lead", workspace_id=ws)
        generated = generator.run(signal["id"], ["vk"], workspace_id=ws)
        variant = generated["variants"][0]
        account = store.list_channel_accounts()[0]
        publication = store.create_publication(
            variant["id"], account["id"],
            datetime.now(timezone.utc) - timedelta(seconds=1), None,
            is_ad=False, require_approval=False)
        result = asyncio.run(tasks.publish_publication({"worker_id": "live"}, publication["id"]))
        stored = store.get_publication(publication["id"])
        attempt = store.attempt_last(publication["id"])

    assert sent, "адаптер не получил пост"
    assert result["status"] == "published", result
    assert stored["status"] == "published" and stored["external_id"].startswith("live_")
    assert attempt and attempt["outcome"] == "published" and attempt["external_id"]
    # чужой клиент не видит ни публикацию, ни её журнал
    with tenancy.bind(world["foreign_ws"], tenant_id=world["tenant_b"],
                      user_id=world["foreign_user"], role="owner"):
        assert store.get_publication(publication["id"]) is None


# ------------------------------------------- комплаенс и матрица ролей (0006_rls_strict)
def _ad_insert(engine, world, role: str, *, erid: str | None, status: str = "draft"):
    """Попытка создать рекламную публикацию от имени роли. Возвращает исход строкой."""
    ws = world["a"]
    tenant, user = world["tenant"], world["user"]
    with engine.connect() as conn:
        # подготовка (сигнал, вариант, канал) — от владельца: тестируем только
        # попытку создать рекламную публикацию от имени проверяемой роли.
        _as_workspace(conn, ws, tenant=tenant, user=user, role="owner")
        signal, variant = str(uuid.uuid4()), str(uuid.uuid4())
        account = conn.scalar(text(
            "SELECT id::text FROM channel_accounts WHERE workspace_id = :w LIMIT 1"), {"w": ws})
        conn.execute(text("INSERT INTO signals (id, workspace_id, kind, title) "
                          "VALUES (:i, :w, 'manual', 'Реклама')"), {"i": signal, "w": ws})
        conn.execute(text("INSERT INTO variants (id, workspace_id, signal_id, channel, format, body) "
                          "VALUES (:i, :w, :s, 'vk', 'post', 'Текст рекламного варианта.')"),
                     {"i": variant, "w": ws, "s": signal})
        _as_workspace(conn, ws, tenant=tenant, user=user, role=role)
        try:
            conn.execute(text(
                "INSERT INTO publications (id, workspace_id, variant_id, channel_account_id, "
                "scheduled_at, status, idempotency_key, is_ad, erid) "
                "VALUES (:i, :w, :v, :a, now(), :st, :k, true, :e)"),
                {"i": str(uuid.uuid4()), "w": ws, "v": variant, "a": account,
                 "st": status, "k": f"ad-{uuid.uuid4()}", "e": erid})
            conn.rollback()
            return "created"
        except ProgrammingError:
            conn.rollback()
            return "blocked"


def test_rls_enforces_compliance_and_role_matrix(engine, world):
    """Правила комплаенса держатся на уровне БД, а не только в API.

    Дефект (найденный третьим аудитом): политики были permissive-политиками и
    объединялись через OR, поэтому «широкая» `publications_tenant` (платформа ИЛИ
    свой workspace) отменяла ограничение «реклама требует erid». Copywriter создавал
    рекламную публикацию без маркировки, хотя политика `publications_insert` это
    запрещала. После 0006 рамка изоляции и комплаенс — RESTRICTIVE (AND).
    """
    assert _ad_insert(engine, world, "copywriter", erid=None) == "blocked"
    assert _ad_insert(engine, world, "owner", erid=None, status="queued") == "blocked"
    assert _ad_insert(engine, world, "owner", erid=None, status="draft") == "created"
    assert _ad_insert(engine, world, "owner", erid="erid: audit3") == "created"


def test_rls_role_matrix_blocks_read_only_roles(engine, world):
    """Роль без права записи не создаёт публикацию даже в своём пространстве."""
    assert _ad_insert(engine, world, "analyst", erid=None, status="draft") == "blocked"


def test_platform_role_still_works_for_worker(engine, world):
    """Сервисная политика не ломает платформенные процессы (воркер, cron)."""
    with engine.connect() as conn:
        _platform(conn)
        assert conn.scalar(text("SELECT count(*) FROM publications")) >= 0
        assert conn.scalar(text("SELECT count(*) FROM entity_docs")) >= 0


def test_policy_model_is_single_source_and_strict(engine):
    """Политики: одна модель, без «широких» дублей, рамка арендатора — RESTRICTIVE.

    Этот тест ловит возврат к прежнему состоянию: если baseline-файлы снова начнут
    создавать ``*_tenant`` (permissive OR) или кто-то добавит permissive-дубль,
    защита снова станет зависеть от порядка выполнения файлов.
    """
    with engine.connect() as conn:
        wide = conn.execute(text(
            "SELECT tablename, policyname FROM pg_policies "
            "WHERE schemaname = 'public' AND policyname LIKE '%\\_tenant'")).fetchall()
        assert wide == [], f"permissive-дубли *_tenant вернулись: {wide}"

        frames = conn.execute(text(
            "SELECT tablename FROM pg_policies "
            "WHERE schemaname = 'public' AND policyname LIKE '%\\_scope' "
            "AND permissive = 'RESTRICTIVE'")).fetchall()
        framed = {row[0] for row in frames}
        for table in ("publications", "signals", "variants", "entity_docs",
                      "channel_accounts", "publication_attempts"):
            assert table in framed, f"{table}: нет RESTRICTIVE-рамки арендатора"

        compliance = conn.execute(text(
            "SELECT permissive, cmd FROM pg_policies "
            "WHERE schemaname = 'public' AND policyname = 'publications_compliance'")).fetchone()
        assert compliance is not None, "комплаенс-политика отсутствует"
        assert compliance[0] == "RESTRICTIVE" and compliance[1] == "INSERT"


def test_rerunning_baseline_policies_keeps_strict_model(engine, world):
    """Повторное применение baseline не ослабляет защиту (регрессия аудита)."""
    sql = (Path(__file__).resolve().parents[1] / "sql" / "rls_policies.sql").read_text()
    with engine.begin() as conn:
        conn.exec_driver_sql(sql)
    assert _ad_insert(engine, world, "copywriter", erid=None) == "blocked"

    with engine.connect() as conn:
        assert conn.execute(text(
            "SELECT count(*) FROM pg_policies WHERE schemaname='public' "
            "AND policyname LIKE '%\\_tenant'")).scalar() == 0
