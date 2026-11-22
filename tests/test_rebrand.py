"""Ребрендинг «ОРБИТА/orbita» → Posiz (posiz.ru): контракты, домены, ключи.

Тесты фиксируют публичные имена и адреса, чтобы рабочее название не вернулось
в интерфейс, документацию или API случайной правкой.
Важно: внутренние имена (`src/orbita`, БД `orbita`) намеренно остаются — их
переименование сломало бы импорты и историю миграций.
"""
from __future__ import annotations

import pathlib
import re

from fastapi.testclient import TestClient

from orbita import apikeys
from orbita.main import app

client = TestClient(app)
ROOT = pathlib.Path(__file__).resolve().parents[2]
H = {"X-Actor-Role": "owner"}


# ---------------------------------------------------------------- API-заголовки
def test_openapi_title_is_posiz():
    schema = client.get("/openapi.json").json()
    assert schema["info"]["title"] == "Posiz Core API"
    assert "posiz.ru" in schema["info"]["description"]


def test_openapi_file_has_no_legacy_brand():
    text = (ROOT / "10-ARCHITECTURE" / "openapi.yaml").read_text(encoding="utf-8")
    assert "Posiz Core API" in text
    assert "orb.link" not in text
    assert not re.search(r"orbita\.example", text)
    assert "ОРБИТА" not in text and "Orbita" not in text


# ---------------------------------------------------------------- домены
def test_short_links_use_posiz_domain():
    body = client.post("/api/v1/links", headers=H,
                       json={"target_url": "https://dogovor.expert/zalog", "channel": "vk",
                             "campaign": "zalog"}).json()
    assert body["short_url"].startswith("https://posiz.ru/l/")
    assert body["target_url"].startswith("https://dogovor.expert/zalog")
    assert "utm_source=vk" in body["target_url"]


def test_billing_invoice_on_api_subdomain(monkeypatch):
    from orbita import billing

    payment = billing.create_invoice(4900, plan="business", inn="7801462122")
    assert payment["invoice_pdf"].startswith("https://api.posiz.ru/v1/billing/")


def test_health_and_links_config_defaults():
    from orbita.config import settings

    assert settings.link_domain == "posiz.ru"


# ---------------------------------------------------------------- API-ключи
def test_api_key_prefix_is_psz_live():
    created = apikeys.issue("Тестовая интеграция", ["read"])
    assert created["key"].startswith("psz_live_")
    assert len(created["key"]) == len("psz_live_") + 32
    assert created["hint"].startswith("psz_live_") and created["key"] not in created["hint"]
    assert "hash" not in created


def test_api_key_endpoints_and_scopes():
    created = client.post("/api/v1/keys", headers=H,
                          json={"name": "CRM", "scopes": ["read", "write"]})
    assert created.status_code == 201
    key = created.json()["key"]
    assert key.startswith("psz_live_")

    ok = client.post("/api/v1/keys/verify", params={"key": key, "scope": "write"}).json()
    assert ok["ok"] is True and "write" in ok["scopes"]

    denied = client.post("/api/v1/keys/verify", params={"key": key, "scope": "publish"}).json()
    assert denied["ok"] is False and denied["code"] == "scope"

    listing = client.get("/api/v1/keys", headers=H).json()
    assert listing["describe"]["prefix"] == "psz_live_"
    assert all(row["hint"].startswith("psz_live_") for row in listing["items"])
    assert not any("hash" in row for row in listing["items"])


def test_legacy_orb_live_keys_are_rejected():
    legacy = "orb_live_" + "a" * 32
    assert apikeys.verify(legacy)["code"] == "legacy_prefix"
    assert client.post("/api/v1/keys/verify", params={"key": legacy}).json()["ok"] is False


def test_api_key_revoke_and_bad_format():
    created = apikeys.issue("Отзываемый", ["read"])
    assert apikeys.revoke(created["id"])["revoked"] is True
    assert apikeys.verify(created["key"])["code"] == "revoked"
    assert apikeys.verify("psz_live_short")["code"] == "bad_format"
    assert apikeys.describe()["prefix"] == "psz_live_"


# ---------------------------------------------------------------- документация и код
def test_public_docs_have_no_legacy_name():
    """Публичные документы и код: старого имени и старого домена быть не должно."""
    files = [
        ROOT / "README.md",
        ROOT / "00-STRATEGY" / "02-competitors.md",
        ROOT / "00-STRATEGY" / "03-product-concept.md",
        ROOT / "10-ARCHITECTURE" / "README.md",
        ROOT / "10-ARCHITECTURE" / "saas-contours.md",
        ROOT / "10-ARCHITECTURE" / "adr" / "0010-link-in-bio.md",
        ROOT / "20-DESIGN" / "design-system.md",
        ROOT / "40-BACKEND" / "README.md",
        ROOT / "40-BACKEND" / ".env.example",
        ROOT / "30-APP" / "index.html",
    ]
    problems = []
    # README/чейнджлог вправе упоминать рабочее название в разделе о миграции
    allowed_context = ("рабоч", "legacy", "отклон", "выведено", "ребрендинг", "orb_live_`")
    for path in files:
        for line in path.read_text(encoding="utf-8").splitlines():
            for needle in ("ОРБИТА", "orb.link", "orbita.example", "orbita.app"):
                if needle in line and not any(ctx in line for ctx in allowed_context):
                    problems.append(f"{path.name}: {needle} → {line.strip()[:90]}")
    assert not problems, problems


def test_frontend_brand_surface():
    """Title, логотип, домены — в прототипе уже Posiz."""
    html = (ROOT / "30-APP" / "index.html").read_text(encoding="utf-8")
    assert "<title>Posiz —" in html
    assert 'class="logo" title="Posiz' in html
    assert "const LINK_DOMAIN = 'posiz.ru'" in html
    assert "/l/${" in html and "/@${" in html


def test_internal_package_name_unchanged():
    """Пакет и схема БД не переименованы — иначе ломаются импорты и alembic."""
    assert (ROOT / "40-BACKEND" / "src" / "orbita" / "__init__.py").exists()
    migrations = sorted(p.name for p in (ROOT / "40-BACKEND" / "migrations" / "versions").glob("*.py"))
    # цепочка миграций непрерывна: 0001 → последняя, без «дыр» в номерах
    numbers = [int(name[:4]) for name in migrations]
    assert numbers[0] == 1 and numbers == list(range(1, len(numbers) + 1)), migrations


def test_domain_map_documented():
    """Домены и их роли зафиксированы в README и стратегии."""
    for rel in ("README.md", "00-STRATEGY/03-product-concept.md", "10-ARCHITECTURE/README.md"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "posiz.ru" in text and "contentforge.fun" in text, rel
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for host in ("app.posiz.ru", "api.posiz.ru", "posiz.ru/l/", "posiz.ru/@"):
        assert host in readme, host
