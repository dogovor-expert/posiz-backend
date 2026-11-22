"""Периметр: JWT, заголовки, CORS, лимиты, подписи вебхуков, шифрование токенов."""
from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from orbita import crypto
from orbita.config import demo_roles_enabled
from orbita.main import app
from orbita.security import issue_jwt

client = TestClient(app)


def H(role: str = "owner") -> dict:
    return {"X-Actor-Role": role}


# --------------------------------------------------------------------------
# 1. Демо-заголовок выключен по умолчанию, роль — только из JWT
# --------------------------------------------------------------------------
def test_demo_roles_are_off_by_default(monkeypatch):
    monkeypatch.delenv("RBAC_DEMO_ROLES", raising=False)
    assert demo_roles_enabled() is False
    r = client.get("/api/v1/me", headers=H("owner"))
    assert r.status_code == 401, "заголовок X-Actor-Role не должен давать доступ"
    assert r.json()["detail"]["kind"] == "unauthenticated"


def test_health_reports_auth_mode(monkeypatch):
    monkeypatch.delenv("RBAC_DEMO_ROLES", raising=False)
    body = client.get("/health").json()
    assert body["auth"] == "jwt"
    assert body["rate_limit"]["enabled"] is True


def test_jwt_grants_role_and_permissions(monkeypatch):
    monkeypatch.delenv("RBAC_DEMO_ROLES", raising=False)
    token = issue_jwt({"sub": "user_42", "role": "lawyer", "workspace_id": "ws_de"})
    r = client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["role"] == "lawyer"
    assert "publication:approve" in body["permissions"]
    assert "variant:generate" not in body["permissions"]


def test_jwt_with_bad_signature_is_rejected(monkeypatch):
    monkeypatch.delenv("RBAC_DEMO_ROLES", raising=False)
    token = issue_jwt({"sub": "u", "role": "owner"})
    head, payload, _ = token.split(".")
    forged = f"{head}.{payload}.{'a' * 43}"
    assert client.get("/api/v1/me", headers={"Authorization": f"Bearer {forged}"}).status_code == 401


def test_jwt_alg_none_is_rejected(monkeypatch):
    monkeypatch.delenv("RBAC_DEMO_ROLES", raising=False)
    import base64

    def b64(obj) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    token = f"{b64({'alg': 'none'})}.{b64({'role': 'owner', 'sub': 'x'})}."
    r = client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


def test_jwt_without_role_claim_is_rejected(monkeypatch):
    monkeypatch.delenv("RBAC_DEMO_ROLES", raising=False)
    token = issue_jwt({"sub": "u"})
    r = client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401
    assert "role" in r.json()["detail"]["message"]


def test_expired_jwt_is_rejected(monkeypatch):
    monkeypatch.delenv("RBAC_DEMO_ROLES", raising=False)
    token = issue_jwt({"sub": "u", "role": "owner"}, ttl=-3600)
    assert client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401


# --------------------------------------------------------------------------
# 2. Заголовки безопасности и CORS
# --------------------------------------------------------------------------
def test_security_headers_present():
    r = client.get("/health")
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors" in r.headers["Content-Security-Policy"]


def test_cors_allows_configured_origin_only():
    ok = client.options("/health", headers={
        "Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET",
    })
    assert ok.status_code in (200, 204)
    assert ok.headers.get("access-control-allow-origin") == "http://localhost:5173"

    bad = client.options("/health", headers={
        "Origin": "https://evil.example", "Access-Control-Request-Method": "GET",
    })
    assert bad.headers.get("access-control-allow-origin") != "https://evil.example"


# --------------------------------------------------------------------------
# 3. Rate limiting публичных эндпоинтов
# --------------------------------------------------------------------------
def _make_link() -> str:
    r = client.post("/api/v1/links", headers=H(),
                    json={"target_url": "https://dogovor.expert/checklist",
                          "channel": "vk", "campaign": "audit-test"})
    assert r.status_code == 201, r.text
    return r.json()["code"]


def test_link_redirect_is_rate_limited():
    code = _make_link()
    codes = [client.get(f"/l/{code}", follow_redirects=False).status_code for _ in range(65)]
    assert 302 in codes
    assert 429 in codes
    limited = client.get(f"/l/{code}", follow_redirects=False)
    assert limited.status_code == 429
    assert int(limited.headers["Retry-After"]) >= 1
    assert limited.json()["detail"]["kind"] == "rate_limited"


def test_bio_page_is_rate_limited():
    slug = client.get("/api/v1/bio/pages", headers=H()).json()
    pages = slug["pages"] if isinstance(slug, dict) and "pages" in slug else slug
    target = (pages[0]["slug"] if isinstance(pages, list) and pages else "dogovor")
    codes = [client.get(f"/bio/{target}").status_code for _ in range(125)]
    assert 429 in codes, "публичная страница без лимита превращается в канал для спама"


# --------------------------------------------------------------------------
# 4. Подписи вебхуков
# --------------------------------------------------------------------------
def _comment_payload() -> dict:
    return {"author": "Иван", "text": "Напишите ДОГОВОР и пришлите чек-лист"}


def _comment_body(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()


def test_comment_webhook_without_signature_is_rejected():
    r = client.post("/webhooks/comments/vk", json=_comment_payload())
    assert r.status_code == 403
    assert r.json()["detail"]["kind"] == "webhook_rejected"


def test_comment_webhook_with_bad_signature_is_rejected():
    r = client.post("/webhooks/comments/vk", json=_comment_payload(),
                    headers={"X-Webhook-Signature": "sha256=" + "0" * 64})
    assert r.status_code == 403


def test_comment_webhook_with_valid_signature_is_accepted():
    payload = _comment_payload()
    signature = hmac.new(b"test-shared-secret", _comment_body(payload),
                         hashlib.sha256).hexdigest()
    r = client.post("/webhooks/comments/vk", json=payload,
                    headers={"X-Webhook-Signature": f"sha256={signature}"})
    assert r.status_code == 200, r.text
    assert r.json()["matched"] >= 1


def test_telegram_platform_secret_is_accepted():
    payload = _comment_payload()
    r = client.post("/webhooks/comments/telegram", json=payload,
                    headers={"X-Telegram-Bot-Api-Secret-Token": "test-tg-secret"})
    assert r.status_code == 200, r.text


def test_yookassa_webhook_rejects_forged_payment():
    inv = client.post("/api/v1/billing/invoices", headers=H(),
                      json={"amount_rub": 2490, "plan": "business", "payer": "ООО «Ромашка»",
                            "inn": "7701234567"}).json()
    forged = client.post(f"/api/v1/billing/webhook?payment_id={inv['payment_id']}", headers=H())
    assert forged.status_code == 403
    assert client.get("/api/v1/billing/subscription", headers=H()).json()["price_rub"] != 0

    good = client.post(f"/api/v1/billing/webhook?payment_id={inv['payment_id']}", headers={
        **H(), "X-Signature": hmac.new(b"test-yookassa-secret", b"", hashlib.sha256).hexdigest(),
    })
    assert good.status_code == 200 and good.json()["status"] == "succeeded"


def test_yookassa_webhook_accepts_official_network(monkeypatch):
    monkeypatch.delenv("YOOKASSA_WEBHOOK_SECRET", raising=False)
    from orbita.security import verify_yookassa

    verify_yookassa({}, raw_body=b"", signature=None, client_ip="185.71.76.10")
    with pytest.raises(Exception) as exc:
        verify_yookassa({}, raw_body=b"", signature=None, client_ip="8.8.8.8")
    assert getattr(exc.value, "status_code", None) == 403


# --------------------------------------------------------------------------
# 5. Шифрование токенов каналов
# --------------------------------------------------------------------------
def test_token_vault_roundtrip_and_randomness():
    token = "vk1.a.SUPER-SECRET-TOKEN"
    one, two = crypto.encrypt(token), crypto.encrypt(token)
    assert one != two, "nonce должен быть случайным"
    assert token not in one
    assert crypto.decrypt(one) == token and crypto.decrypt(two) == token
    assert one.startswith("v1:")


def test_token_vault_rejects_foreign_ciphertext():
    with pytest.raises(crypto.TokenVaultError):
        crypto.decrypt("v1:" + "A" * 40)
    with pytest.raises(crypto.TokenVaultError):
        crypto.decrypt("plain-token-without-version")


def test_channel_token_is_stored_encrypted():
    from orbita import store

    store.set_channel_token("vk_1", "secret-community-token", channel="vk")
    ciphertext = store.CHANNEL_TOKENS["vk_1"]
    assert "secret-community-token" not in ciphertext
    assert store.get_channel_token("vk_1") == "secret-community-token"


def test_channels_api_never_returns_tokens():
    body = client.get("/api/v1/channels", headers=H()).text
    assert "secret-community-token" not in body
    assert "token" not in body.lower() or "token_ciphertext" not in body


def test_prod_without_key_refuses_to_store_tokens(monkeypatch):
    monkeypatch.delenv("TOKEN_ENCRYPTION_KEY", raising=False)
    monkeypatch.setenv("APP_ENV", "prod")
    crypto._ephemeral = None
    with pytest.raises(crypto.TokenVaultError):
        crypto.encrypt("token")
    monkeypatch.setenv("APP_ENV", "dev")


def test_channel_token_endpoint_returns_only_hint():
    r = client.post("/api/v1/channels/vk_1/token", headers=H("owner"),
                    json={"token": "vk1.a.very-secret-community-token"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["encrypted"] is True
    assert "very-secret-community-token" not in r.text
    assert body["token_hint"].startswith("…")
    from orbita import store

    assert store.get_channel_token("vk_1") == "vk1.a.very-secret-community-token"


def test_channel_token_endpoint_requires_permission():
    r = client.post("/api/v1/channels/vk_1/token", headers=H("analyst"),
                    json={"token": "vk1.a.very-secret-community-token"})
    assert r.status_code == 403
    short = client.post("/api/v1/channels/vk_1/token", headers=H("owner"), json={"token": "abc"})
    assert short.status_code == 422
