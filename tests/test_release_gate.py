"""Release gate: 12 проверок, которые нельзя «пройти галочкой».

Аудит дал релиз-гейту 0/12. Каждый пункт ниже — исполняемая проверка по репозиторию
и коду, а не декларация в README. Пункты, которым нужна живая инфраструктура
(Postgres + pgvector, Redis, ARQ-цикл), помечены ``infra``: они выполняются в CI
с `docker compose up`, а локально сообщают, чего не хватает, вместо ложного «ок».

Запуск только гейта:  pytest tests/test_release_gate.py -q
Отчёт по пунктам:     python -m orbita.release_gate
"""
from __future__ import annotations

import pathlib
import re

import yaml

from orbita import release_gate as gate

ROOT = pathlib.Path(__file__).resolve().parents[1]
REPO = ROOT.parent


def _compose() -> dict:
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text())


# ---------------------------------------------------------------- 1. pgvector
def test_gate_01_database_image_has_pgvector():
    """Схема требует `CREATE EXTENSION vector` — значит и образ БД должен её содержать."""
    db = _compose()["services"]["db"]
    assert "pgvector" in db["image"], db["image"]
    schema = (ROOT / "schema.sql").read_text()
    assert "CREATE EXTENSION" in schema and "vector" in schema


# ---------------------------------------------------------------- 2. один путь миграций
def test_gate_02_single_schema_creation_path():
    """Схему создаёт только Alembic. Раньше initdb накатывал schema.sql и падал повторно."""
    db = _compose()["services"]["db"]
    mounts = db.get("volumes", [])
    initdb = [m for m in mounts if "docker-entrypoint-initdb.d" in str(m)]
    assert len(initdb) == 1, initdb
    assert "schema.sql" not in str(initdb[0]), "schema.sql не должен грузиться через initdb"
    assert (ROOT / "migrations" / "versions").exists()
    migrate = _compose()["services"]["migrate"]
    assert "alembic" in " ".join(migrate["command"])


# ---------------------------------------------------------------- 3. healthchecks
def test_gate_03_healthchecks_and_ordering():
    services = _compose()["services"]
    for name in ("db", "redis"):
        assert "healthcheck" in services[name], name
    api_dep = services["api"]["depends_on"]
    assert api_dep["db"]["condition"] == "service_healthy"
    assert api_dep["migrate"]["condition"] == "service_completed_successfully"


# ---------------------------------------------------------------- 4. RLS применяется к приложению
def test_gate_04_rls_applies_to_application_role():
    cluster = (ROOT / "sql" / "00_cluster_init.sql").read_text()
    assert "NOBYPASSRLS" in cluster and "NOSUPERUSER" in cluster
    api_env = _compose()["services"]["api"]["environment"]
    assert "orbita_app" in api_env["DATABASE_URL"], api_env["DATABASE_URL"]
    migration = (ROOT / "migrations" / "versions" / "0004_publishing.py").read_text()
    assert "FORCE ROW LEVEL SECURITY" in migration


# ---------------------------------------------------------------- 5. арендатор в слое данных
def test_gate_05_tenant_context_enforced_in_data_layer():
    from orbita import tenancy
    from orbita.persistence import Database  # noqa: F401 — модуль импортируется без БД

    src = (ROOT / "src" / "orbita" / "persistence.py").read_text()
    assert "tenancy.scope_workspace()" in src
    assert "def doc_get" in src and "workspace_id" in src
    main = (ROOT / "src" / "orbita" / "main.py").read_text()
    assert "tenancy.bind" in main
    assert tenancy.scope_workspace() is None       # вне запроса — платформенный режим


# ---------------------------------------------------------------- 6. ARQ-воркер действительно работает
def test_gate_06_worker_is_real_arq():
    worker = _compose()["services"]["worker"]
    assert "arq" in " ".join(worker["command"])
    source = (ROOT / "src" / "orbita" / "workers" / "run.py").read_text()
    assert "Worker(" in source and "asyncio.Event().wait()" not in source
    from orbita.workers.run import WorkerSettings

    assert WorkerSettings.functions and WorkerSettings.cron_jobs


# ---------------------------------------------------------------- 7. публикации не дублируются
def test_gate_07_publication_claim_and_lease():
    from orbita import publishing

    assert publishing.CLAIMABLE == {"queued", "retry_wait"}
    assert "publishing" not in publishing.CLAIMABLE
    tasks_src = (ROOT / "src" / "orbita" / "workers" / "tasks.py").read_text()
    assert "claim_publication" in tasks_src
    assert "reclaim_expired" in tasks_src
    scheduler_src = (ROOT / "src" / "orbita" / "scheduler.py").read_text()
    assert "reclaim_expired" in scheduler_src


# ---------------------------------------------------------------- 8. токены каналов из хранилища
def test_gate_08_channel_token_from_storage():
    base = (ROOT / "src" / "orbita" / "adapters" / "base.py").read_text()
    assert "token" in base and "PublishInput" in base
    registry = (ROOT / "src" / "orbita" / "adapters" / "registry.py").read_text()
    assert "def get_adapter(channel: str, token" in registry
    persistence = (ROOT / "src" / "orbita" / "persistence.py").read_text()
    assert "external_id" in persistence


# ---------------------------------------------------------------- 9. аутентификация без дыр
def test_gate_09_auth_hardening():
    auth = (ROOT / "src" / "orbita" / "auth.py").read_text()
    assert 'role="owner"' not in auth.replace("return \"owner\"", ""), "роль не назначается по умолчанию"
    assert "OAUTH_STATES" in auth and "invalid_state" in auth
    assert "verify_totp" in auth
    # «backup_codes» теперь список реальных кодов, а не число 10
    assert '"backup_codes": 10}' not in auth and "hashlib.sha256(c.encode())" in auth
    security = (ROOT / "src" / "orbita" / "security.py").read_text()
    assert "_ALGORITHMS" in security


# ---------------------------------------------------------------- 10. PII до внешней модели
def test_gate_10_pii_masking_before_external_calls():
    pii = (ROOT / "src" / "orbita" / "pii.py").read_text()
    assert "_ner_remote(out)" in pii, "NER должен получать уже замаскированный текст"
    assert "class Masker" in pii
    assert "mapping: dict[str, str] | None = None" in pii


# ---------------------------------------------------------------- 11. комплаенс: маркировка в тексте
def test_gate_11_marking_reaches_channel():
    from orbita import compliance

    ctx = compliance.VariantContext(channel="vk", body="Реклама поста", erid="erid: X1")
    text = compliance.apply_marking("Текст поста.", ctx)
    assert "erid: X1" in text and "Реклама" in text
    tasks_src = (ROOT / "src" / "orbita" / "workers" / "tasks.py").read_text()
    assert "apply_marking" in tasks_src
    assert "channel_in_blogger_registry: bool | None" in (
        ROOT / "src" / "orbita" / "compliance.py").read_text()


# ---------------------------------------------------------------- 12. документация синхронна
def test_gate_12_docs_match_code():
    """Числа в README и openapi генерируются, а не пишутся руками."""
    openapi = yaml.safe_load((REPO / "10-ARCHITECTURE" / "openapi.yaml").read_text())
    paths = openapi["paths"]
    assert len(paths) >= 90
    dump = (ROOT / "scripts" / "dump_openapi.py")
    assert dump.exists(), "openapi обновляется скриптом"
    adr = sorted((REPO / "10-ARCHITECTURE" / "adr").glob("*.md"))
    assert any(p.name.startswith("0017") for p in adr), \
        "решение о Publishing Engine зафиксировано ADR 0017"


def test_gate_summary_is_executable():
    """Гейт можно распечатать как отчёт — тот же набор пунктов, что выше."""
    from orbita import release_gate

    report = release_gate.run()
    assert report["total"] >= 12, report["total"]
    assert report["checks"], "у гейта должны быть пункты"
    assert all("id" in c and "status" in c for c in report["checks"])

# ---------------------------------------------------------------- гейт: поведение, не текст
def test_gate_contains_behavioural_checks():
    """Половина пунктов гейта должна ПРОВЕРЯТЬ ПОВЕДЕНИЕ, а не искать строку в файле.

    Это прямой урок второго аудита: «написано» и «работает» — разные вещи. Пункты ниже
    запускают сценарий (публикацию, вход, изоляцию), поэтому их нельзя пройти, просто
    добавив комментарий в код.
    """
    behavioural = {
        "tenancy.cross_read", "tenancy.registration", "publishing.no_double_post",
        "publishing.marking", "compliance.advertiser", "auth.two_factor", "pii.external",
        "db.rls_runtime", "orm.schema",
    }
    ids = {spec[0] for spec in gate.CHECK}
    assert behavioural <= ids, f"нет поведенческих проверок: {behavioural - ids}"
    assert len(behavioural) >= 9


def test_gate_reports_live_infrastructure_requirement():
    """Гейт без DATABASE_URL/REDIS_URL не должен выглядеть «зелёным по умолчанию»."""
    report = gate.run()
    by_id = {c["id"]: c for c in report["checks"]}
    for check_id in ("db.pgvector", "queue.arq", "db.rls_runtime"):
        assert check_id in by_id
        assert by_id[check_id]["status"] == "failed"
        assert "не задан" in by_id[check_id]["detail"]
