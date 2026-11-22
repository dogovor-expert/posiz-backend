"""Тесты зон роста MVP: ОРД, короткие ссылки, онбординг MAX, серверный RBAC."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from orbita.main import app

client = TestClient(app)


def H(role: str) -> dict:
    """Демо-заголовок роли: в проде его заменяет JWT."""
    return {"X-Actor-Role": role}


def _variant(channel: str = "max") -> str:
    sid = client.post("/api/v1/signals", json={"kind": "manual", "title": "Залог при аренде",
                                              "goal": "lead"}).json()["id"]
    out = client.post(f"/api/v1/signals/{sid}/generate", json={"channels": [channel]}).json()
    return out["variants"][0]["id"]


def _scheduled() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()


def _erid(role: str = "copywriter") -> str:
    r = client.post("/api/v1/ord/erid", headers=H(role), json={
        "channel": "max", "advertiser_name": "ООО «Договор Эксперт»",
        "advertiser_inn": "7701234567", "text": "Скидка 20% на комплект документов",
    })
    assert r.status_code == 201, r.text
    return r.json()["erid"]


# ---------------------------- RBAC ----------------------------
def test_me_returns_permissions():
    r = client.get("/api/v1/me", headers=H("copywriter"))
    assert r.status_code == 200
    body = r.json()
    assert body["role_title"] == "Копирайтер"
    assert "publication:create" in body["permissions"]
    assert "ad:approve" not in body["permissions"]


def test_unknown_role_rejected():
    r = client.get("/api/v1/me", headers=H("hacker"))
    assert r.status_code == 403
    assert r.json()["detail"]["kind"] == "unknown_role"


def test_analyst_cannot_create_signal():
    r = client.post("/api/v1/signals", headers=H("analyst"),
                    json={"kind": "manual", "title": "Попытка аналитика", "goal": "trust"})
    assert r.status_code == 403
    detail = r.json()["detail"]
    assert detail["permission"] == "signal:create"
    assert "owner" in detail["allowed_roles"]


def test_copywriter_cannot_publish():
    vid = _variant()
    pub = client.post("/api/v1/publications", headers=H("copywriter"),
                      json={"variant_id": vid, "channel_account_id": "ma_1",
                            "scheduled_at": _scheduled()}).json()
    r = client.post(f"/api/v1/publications/{pub['id']}/publish-now", headers=H("copywriter"))
    assert r.status_code == 403
    assert r.json()["detail"]["permission"] == "publication:publish"


def test_lawyer_cannot_create_publication():
    vid = _variant()
    r = client.post("/api/v1/publications", headers=H("lawyer"),
                    json={"variant_id": vid, "channel_account_id": "ma_1",
                          "scheduled_at": _scheduled()})
    assert r.status_code == 403
    assert r.json()["detail"]["permission"] == "publication:create"


def test_roles_endpoint_lists_matrix():
    r = client.get("/api/v1/team/roles", headers=H("owner"))
    assert r.status_code == 200
    roles = {x["role"]: x for x in r.json()["roles"]}
    assert set(roles) == {"owner", "copywriter", "lawyer", "analyst"}
    assert "ad:approve" in roles["lawyer"]["permissions"]


def test_audit_log_records_decisions():
    client.get("/api/v1/channels", headers=H("analyst"))
    r = client.get("/api/v1/audit?limit=20", headers=H("owner"))
    assert r.status_code == 200
    assert r.json()["total"] >= 1
    assert any(item["role"] == "analyst" for item in r.json()["items"])


def test_audit_requires_owner():
    assert client.get("/api/v1/audit", headers=H("analyst")).status_code == 403


# ---------------------------- ОРД ----------------------------
def test_ord_providers_listed():
    r = client.get("/api/v1/ord/providers", headers=H("copywriter"))
    assert r.status_code == 200
    names = {p["name"] for p in r.json()}
    assert {"mock", "yandex_ord", "vk_ord"} <= names


def test_erid_is_issued_and_marking_block_complete():
    erid = _erid()
    r = client.post("/api/v1/ord/erid", headers=H("copywriter"), json={
        "channel": "vk", "advertiser_name": "ООО «Договор Эксперт»",
        "advertiser_inn": "7701234567", "text": "Скидка 20%"})
    body = r.json()
    assert body["status"] == "registered"
    assert "Реклама." in body["marking_block"] and "erid:" in body["marking_block"]
    assert erid.startswith("2Vtz")


def test_ord_submissions_visible_to_lawyer_only():
    _erid()
    assert client.get("/api/v1/ord/submissions", headers=H("copywriter")).status_code == 403
    r = client.get("/api/v1/ord/submissions", headers=H("lawyer"))
    assert r.status_code == 200 and r.json()


# --------------------- реклама: erid + подпись ---------------------
def test_ad_without_erid_not_created():
    vid = _variant()
    r = client.post("/api/v1/publications", headers=H("copywriter"),
                    json={"variant_id": vid, "channel_account_id": "ma_1",
                          "scheduled_at": _scheduled(), "is_ad": True})
    assert r.status_code == 409
    assert r.json()["detail"]["kind"] == "ad_marking_required"


def test_ad_requires_lawyer_approval_before_queue():
    vid, erid = _variant(), _erid()
    pub = client.post("/api/v1/publications", headers=H("copywriter"),
                      json={"variant_id": vid, "channel_account_id": "ma_1",
                            "scheduled_at": _scheduled(), "is_ad": True, "erid": erid}).json()
    assert pub["status"] == "draft"                    # в очередь не попала
    early = client.post(f"/api/v1/publications/{pub['id']}/publish-now", headers=H("owner"))
    assert early.status_code == 409
    assert early.json()["detail"]["kind"] == "approval_required"

    denied = client.post(f"/api/v1/publications/{pub['id']}/approve", headers=H("copywriter"))
    assert denied.status_code == 403                   # подписать рекламу копирайтер не может

    ok = client.post(f"/api/v1/publications/{pub['id']}/approve", headers=H("lawyer"))
    assert ok.status_code == 200
    assert ok.json()["status"] == "queued"
    assert ok.json()["approved_by_role"] == "lawyer"

    published = client.post(f"/api/v1/publications/{pub['id']}/publish-now", headers=H("owner"))
    assert published.status_code == 200
    assert published.json()["status"] == "published"


def test_informational_post_publishes_without_approval():
    vid = _variant()
    pub = client.post("/api/v1/publications", headers=H("copywriter"),
                      json={"variant_id": vid, "channel_account_id": "ma_1",
                            "scheduled_at": _scheduled()}).json()
    assert pub["status"] == "queued"
    r = client.post(f"/api/v1/publications/{pub['id']}/publish-now", headers=H("owner"))
    assert r.status_code == 200


# --------------------- короткие ссылки ---------------------
def test_short_link_created_with_utm():
    r = client.post("/api/v1/links", headers=H("copywriter"),
                    json={"target_url": "https://dogovor.expert/constructor",
                          "channel": "vk", "campaign": "zalor-arenda"})
    assert r.status_code == 201
    body = r.json()
    assert body["short_url"].startswith("https://posiz.ru/l/")
    assert "utm_source=vk" in body["target_url"]
    assert "utm_campaign=zalor-arenda" in body["target_url"]


def test_short_link_redirect_and_click_count():
    code = client.post("/api/v1/links", headers=H("owner"),
                       json={"target_url": "dogovor.expert/constructor", "channel": "max",
                             "campaign": "click-test"}).json()["code"]
    r = client.get(f"/l/{code}", follow_redirects=False)
    assert r.status_code == 302
    assert "utm_source=max" in r.headers["location"]
    client.get(f"/l/{code}", follow_redirects=False)

    stats = client.get(f"/api/v1/links/{code}/stats", headers=H("analyst")).json()
    assert stats["clicks"] == 2 and stats["clicks_7d"] == 2


def test_unknown_link_404():
    assert client.get("/l/doesnotexist", follow_redirects=False).status_code == 404


def test_analytics_includes_link_clicks():
    client.post("/api/v1/links", headers=H("owner"),
                json={"target_url": "dogovor.expert", "channel": "tg", "campaign": "an"})
    summary = client.get("/api/v1/analytics/summary", headers=H("analyst")).json()
    assert "link_clicks" in summary and summary["links_total"] >= 1


# --------------------- онбординг MAX ---------------------
def test_quickstart_puts_max_last_and_non_blocking():
    r = client.get("/api/v1/onboarding/quickstart", headers=H("owner"))
    assert r.status_code == 200
    body = r.json()
    assert body["blocking_steps"] == []
    order = [s["channel"] for s in body["steps"]]
    assert order[0] in {"vk", "telegram"} and "max" in order


def test_connect_vk_immediately():
    r = client.post("/api/v1/channels/vk/connect", headers=H("owner"))
    assert r.status_code == 200
    assert r.json()["state"] == "connected"
    assert r.json()["sandbox"] is False


def test_connect_max_goes_to_sandbox():
    r = client.post("/api/v1/channels/max/connect", headers=H("owner"))
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "pending_verification"
    assert body["sandbox"] is True
    assert "ИП или ООО" in body["requirements"]


def test_connect_requires_channel_manage():
    assert client.post("/api/v1/channels/max/connect", headers=H("copywriter")).status_code == 403


def test_verify_promotes_max_from_sandbox():
    client.post("/api/v1/channels/max/connect", headers=H("owner"))
    r = client.post("/api/v1/onboarding/max/verify", headers=H("owner"))
    assert r.status_code == 200
    assert r.json()["state"] == "connected" and r.json()["sandbox"] is False


def test_channels_expose_connection_state():
    client.post("/api/v1/channels/max/connect", headers=H("owner"))
    items = client.get("/api/v1/channels", headers=H("analyst")).json()
    max_acc = next(c for c in items if c["channel"] == "max")
    assert max_acc["connection_state"] == "pending_verification"
    assert max_acc["sandbox"] is True
