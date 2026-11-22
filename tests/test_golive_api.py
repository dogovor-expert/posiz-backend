"""Go-Live: ручки знаний, Инбокса, ЭДО, ленты Дзена, сессий и платформенных прав."""
from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from orbita import edo, knowledge, llm, sessions
from orbita.main import app

client = TestClient(app)


def H(role: str = "owner") -> dict:
    return {"X-Actor-Role": role}


def HA() -> dict:
    return {"X-Actor-Role": "owner", "X-Platform-Admin": "1"}


# ---------------------------------------------------------------- platform admin
def test_owner_has_no_platform_permissions():
    me = client.get("/api/v1/me", headers=H("owner")).json()
    assert me["is_platform_admin"] is False
    assert not [p for p in me["permissions"] if p.startswith("platform:")]


def test_platform_admin_header_grants_permissions():
    me = client.get("/api/v1/me", headers=HA()).json()
    assert me["is_platform_admin"] is True
    assert {"platform:admin", "platform:ops", "platform:impersonate"} <= set(me["permissions"])


def test_admin_endpoint_requires_platform_flag():
    assert client.get("/api/v1/admin/metrics", headers=H("owner")).status_code == 403
    body = client.get("/api/v1/admin/metrics", headers=H("owner")).json()
    assert body["detail"]["type"] == "forbidden" and "hint" in body["detail"]
    assert client.get("/api/v1/admin/metrics", headers=HA()).status_code == 200


def test_platform_admin_via_jwt_claim():
    from orbita import security

    import os

    previous = os.environ.get("JWT_SECRET")
    os.environ["JWT_SECRET"] = "test-secret-1234567890"
    try:
        token = security.issue_jwt({"sub": "u1", "role": "owner", "workspace_id": "ws_de",
                                    "platform_admin": True}, ttl=600)
        resp = client.get("/api/v1/admin/metrics",
                          headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        # без claim — снова 403, даже с валидным JWT владельца
        plain = security.issue_jwt({"sub": "u1", "role": "owner", "workspace_id": "ws_de"},
                                   ttl=600)
        assert client.get("/api/v1/admin/metrics",
                          headers={"Authorization": f"Bearer {plain}"}).status_code == 403
    finally:
        if previous is None:
            os.environ.pop("JWT_SECRET", None)
        else:
            os.environ["JWT_SECRET"] = previous


def test_impersonation_requires_platform_permission():
    assert client.post("/api/v1/admin/impersonate", headers=H("owner"),
                       json={"tenant_id": "t_1", "reason": "проверка"}).status_code == 403
    ok = client.post("/api/v1/admin/impersonate", headers=HA(),
                     json={"tenant_id": "t_1", "reason": "проверка доступа"})
    assert ok.status_code == 200


# ---------------------------------------------------------------- knowledge / RAG
def test_knowledge_endpoints_roundtrip():
    knowledge.seed_demo()
    stats = client.get("/api/v1/knowledge", headers=H()).json()
    assert stats["stats"]["chunks"] > 0

    src = client.post("/api/v1/knowledge/sources", headers=H(),
                      json={"kind": "manual", "location": "https://dogovor.expert/price"})
    assert src.status_code == 201
    card = client.post("/api/v1/knowledge/cards", headers=H(), json={
        "kind": "product", "title": "Тариф «Агентство»",
        "body": "14 900 ₽ в месяц, без ограничений, командный доступ и SLA 99,5%.",
        "source_id": src.json()["id"]})
    assert card.status_code == 201
    indexed = client.post(f"/api/v1/knowledge/sources/{src.json()['id']}/index", headers=H()).json()
    assert indexed["status"] == "ready" and indexed["chunks"] >= 1

    found = client.get("/api/v1/knowledge/search", headers=H(),
                       params={"q": "тариф агентство"}).json()
    assert found["hits"] and found["citations"]
    assert client.get("/api/v1/knowledge/sources/none/index", headers=H()).status_code in (404, 405)


def test_knowledge_write_requires_manage_permission():
    """Читать может аналитик, писать в базу знаний — только менеджер воркспейса."""
    assert client.get("/api/v1/knowledge", headers=H("analyst")).status_code == 200
    assert client.post("/api/v1/knowledge/cards", headers=H("analyst"),
                       json={"title": "x", "body": "y"}).status_code == 403


# ---------------------------------------------------------------- Инбокс
def test_inbox_sync_masks_pii(monkeypatch):
    from orbita.workers import sync as sync_workers

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/wall.get"):
            return httpx.Response(200, json={"response": {"items": [
                {"id": 1, "views": {"count": 500}, "likes": {"count": 20},
                 "reposts": {"count": 3}, "comments": {"count": 2}}]}})
        if url.endswith("/wall.getComments"):
            return httpx.Response(200, json={"response": {"items": [
                {"id": 11, "from_id": 777, "date": 1_700_000_000,
                 "text": "ДОГОВОР, звоните +7 999 123-45-67, это Иван Петров"}]}})
        return httpx.Response(404, json={})

    monkeypatch.setenv("VK_COMMUNITY_TOKEN", "vk")
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        report = sync_workers.run_once(vk_owner="-42", client=http)
    assert report["inbox"]["stored"] >= 1
    assert report["inbox"]["pii_masked"] >= 1

    items = client.get("/api/v1/inbox", headers=H()).json()["items"]
    row = next(i for i in items if i["channel"] == "vk")
    assert "+7 999 123-45-67" not in row["text"]
    assert "[PHONE_1]" in row["text"]
    assert row["pii"]["masked"] is True


def test_max_webhook_goes_through_inbox_queue(monkeypatch):
    """Вебхук MAX → очередь воркера → нормализация + маскирование PII."""
    monkeypatch.delenv("MAX_WEBHOOK_SECRET", raising=False)
    resp = client.post("/webhooks/max", json={
        "update_type": "message_created",
        "message": {"mid": "mx-1", "body": {"text": "ДОГОВОР, тел 8 999 111-22-33"},
                    "sender": {"user_id": 5}}})
    assert resp.status_code == 200 and resp.json()["queued"] >= 1
    report = client.post("/api/v1/inbox/sync", headers=H()).json()
    assert report["inbox"]["stored"] >= 1
    rows = client.get("/api/v1/inbox", headers=H()).json()["items"]
    assert any(r["id"] == "max_mx-1" and "[PHONE_1]" in r["text"] for r in rows)


def test_max_webhook_checks_signature(monkeypatch):
    import hashlib
    import hmac
    import json

    monkeypatch.setenv("MAX_WEBHOOK_SECRET", "mx-secret")
    body = json.dumps({"update_type": "message_created", "message": {"mid": "mx-2"}}).encode()
    bad = client.post("/webhooks/max", content=body,
                      headers={"X-Max-Signature": "deadbeef",
                               "Content-Type": "application/json"})
    assert bad.status_code == 401
    good = client.post("/webhooks/max", content=body,
                       headers={"X-Max-Signature": hmac.new(b"mx-secret", body,
                                                            hashlib.sha256).hexdigest(),
                                "Content-Type": "application/json"})
    assert good.status_code == 200


def test_inbox_pii_check_endpoint():
    res = client.get("/api/v1/inbox/pii-check", headers=H(),
                     params={"text": "ИНН 7707083893, тел 8 999 000-11-22"}).json()
    assert res["report"]["found"] == 2
    assert "7707083893" not in res["masked"]


def test_inbox_sync_requires_manage_permission():
    assert client.post("/api/v1/inbox/sync", headers=H("analyst")).status_code == 403


# ---------------------------------------------------------------- ЭДО / УПД
def test_upd_sample_is_valid_xml():
    resp = client.get("/api/v1/billing/upd/sample", headers=H())
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/xml")
    assert "<yandex" not in resp.text and "ТаблСчФакт" in resp.text
    assert edo.validate(resp.text)["ok"]


def test_edo_status_lists_providers():
    status = client.get("/api/v1/billing/edo/status", headers=H()).json()
    assert set(status["providers"]) == {"diadoc", "sbis"}
    assert "УПД" in status["format"]


def test_edo_send_manual_queue_without_keys(monkeypatch):
    monkeypatch.delenv("DIADOC_TOKEN", raising=False)
    monkeypatch.delenv("DIADOC_API_KEY", raising=False)
    result = edo.get_provider("diadoc").send(edo.sample_upd())
    assert result["sent"] is False and result["mode"] == "manual"


def test_edo_diadoc_live_call(monkeypatch):
    monkeypatch.setenv("DIADOC_TOKEN", "tok")
    monkeypatch.setenv("DIADOC_BOX_ID", "box-1")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"message": {"MessageId": "msg-1",
                                                     "Entities": [{"EntityId": "ent-1"}]}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        result = edo.get_provider("diadoc", client=http).send(edo.sample_upd())
    assert result["sent"] is True and result["message_id"] == "msg-1"
    assert "PostMessage" in seen["url"] and "ddauth_token=tok" in seen["auth"]
    assert result["upd_url"].startswith("https://diadoc-api.kontur.ru")


# ---------------------------------------------------------------- RSS-лента
def test_public_feed_endpoint_is_dzen_compatible():
    resp = client.get("/feed/dogovor.xml")
    assert resp.status_code == 200
    assert "yandex:full-text" in resp.text or "<channel>" in resp.text
    assert resp.headers["content-type"].startswith("application/rss+xml")


# ---------------------------------------------------------------- сессии
def test_magic_link_issues_access_and_refresh():
    req = client.post("/api/v1/auth/magic-link", json={"email": "anna@dogovor.expert"}).json()
    assert req["sent"] is False and req["delivery"]["transport"] == "none"   # честный статус
    assert req["demo_token"]                                   # демо-стенд работает
    pair = client.post(f"/api/v1/auth/verify?token={req['demo_token']}").json()
    assert pair["access_token"] and pair["refresh_token"]
    assert pair["expires_in"] == sessions.ACCESS_TTL

    rotated = client.post("/api/v1/auth/refresh",
                          json={"refresh_token": pair["refresh_token"]}).json()
    assert rotated["rotated"] is True and rotated["refresh_token"] != pair["refresh_token"]

    # повторное использование старого refresh — отзыв
    reuse = client.post("/api/v1/auth/refresh", json={"refresh_token": pair["refresh_token"]})
    # повтор использованного refresh = кража: помеченный токен живёт 5 минут и даёт
    # явный сигнал refresh_reuse, после чего отзываются все сессии пользователя
    assert reuse.status_code == 401
    assert reuse.json()["detail"]["code"] == "refresh_reuse"

    # после детекта кражи живой пары не осталось — logout честно это и сообщает
    assert client.post("/api/v1/auth/logout",
                       json={"refresh_token": rotated["refresh_token"]}).json()["revoked"] is False


def test_logout_revokes_own_session():
    """Выход в обычном сценарии (без детекта кражи) отзывает refresh."""
    tokens = sessions.issue("u_logout", "logout@test.ru")
    assert sessions.revoke(tokens["refresh_token"])["revoked"] is True
    assert sessions.refresh(tokens["refresh_token"])["code"] == "invalid_refresh"


def test_refresh_reuse_detection_revokes_chain():
    tokens = sessions.issue("u_test", "u@test.ru")
    first = sessions.refresh(tokens["refresh_token"])
    assert first["rotated"] is True
    # третий вызов старого токена после детекта кражи: все сессии уже отозваны
    assert sessions.refresh(tokens["refresh_token"])["code"] in {"invalid_refresh", "refresh_reuse"}


def test_oauth_callback_requires_configuration():
    res = client.post("/api/v1/auth/oauth/vk_id/callback", json={"code": "abc"})
    assert res.status_code == 400
    assert res.json()["detail"]["code"] == "oauth_not_configured"


def test_oauth_callback_exchanges_code(monkeypatch):
    monkeypatch.setenv("VK_ID_CLIENT_ID", "cid")
    monkeypatch.setenv("VK_ID_CLIENT_SECRET", "csecret")

    def handler(request: httpx.Request) -> httpx.Response:
        if "oauth2/auth" in str(request.url):
            return httpx.Response(200, json={"access_token": "vk-access", "user_id": 42})
        return httpx.Response(200, json={"user": {"user_id": 42, "emails": ["vk@user.ru"]}})

    from orbita import auth

    # state выдаётся ручкой /auth/oauth/{provider} и обязателен при обмене кода:
    # без него чужой code можно подсунуть в вашу сессию (CSRF на входе)
    state = auth.oauth_url("vk_id")["state"]
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        # без state обмен невозможен
        assert auth.exchange_code("vk_id", "code-1", client=http)["code"] == "invalid_state"
        # с выданным state — работает
        result = auth.exchange_code("vk_id", "code-1", state=state, client=http)
    assert result["access_token"] and result["external_id"] == "42"
    # state одноразовый: повторный обмен с тем же значением отклоняется
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        assert auth.exchange_code("vk_id", "code-1", state=state,
                                  client=http)["code"] == "invalid_state"


# ---------------------------------------------------------------- health
def test_health_reports_every_contour():
    health = client.get("/health").json()
    assert health["status"] == "ok"
    integ = health["integrations"]
    for key in ("llm", "mailer", "sessions", "edo", "ord", "headless", "rag"):
        assert key in integ
    assert integ["llm"]["mode"] in {"live", "simulated"}
    assert integ["headless"]["channels"] == ["rutube", "tenchat"]
    assert health["llm"] == llm.status()["mode"]
