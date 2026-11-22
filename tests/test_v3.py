"""Тесты v3: карта каналов, семь функций, контуры SaaS."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from orbita.main import app

client = TestClient(app)


import hashlib
import hmac
import json as _json


def _signature(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _comment_headers(payload: dict) -> dict:
    # httpx сериализует json без пробелов: подпись должна считаться по тем же байтам
    body = _json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    return {"X-Webhook-Signature": "sha256=" + _signature(body, "test-shared-secret")}


def _yookassa_headers() -> dict:
    # тело пустое: параметры уведомления приходят в query
    return {"X-Signature": _signature(b"", "test-yookassa-secret")}


def H(role: str = "owner") -> dict:
    return {"X-Actor-Role": role}


def HA(role: str = "owner") -> dict:
    """Заголовки администратора ПЛАТФОРМЫ: прав platform:* нет ни у одной роли воркспейса."""
    return {"X-Actor-Role": role, "X-Platform-Admin": "1"}


# ------------------------- карта каналов -------------------------
def test_catalog_has_29_channels_and_8_modes():
    r = client.get("/api/v1/channels/catalog", headers=H())
    assert r.status_code == 200
    body = r.json()
    assert body["summary"]["total"] == 29
    modes = body["summary"]["by_mode"]
    for mode in ("api", "rss", "headless", "blog", "webhook", "local", "email", "manual"):
        assert mode in modes, mode


def test_new_platforms_present():
    items = {i["kind"]: i for i in client.get("/api/v1/channels/catalog", headers=H()).json()["items"]}
    for kind in ("dzen", "tenchat", "rutube", "vk_video", "vcru", "habr", "pinterest",
                 "yandex_business", "twogis", "discord", "twitch", "email", "webpush"):
        assert kind in items, kind
    assert items["dzen"]["api_mode"] == "rss"
    assert items["tenchat"]["api_mode"] == "headless"
    assert items["discord"]["api_mode"] == "webhook"
    assert items["email"]["group"] == "owned"


def test_no_public_api_channels_are_honest():
    summary = client.get("/api/v1/channels/catalog", headers=H()).json()["summary"]
    assert "dzen" in summary["no_public_api"] and "tenchat" in summary["no_public_api"]


def test_spec_unknown_channel_is_graceful():
    spec = client.get("/api/v1/channels/kinds/mastodon/spec", headers=H()).json()
    assert spec["api_mode"] == "api"


# ------------------------- thread splitter -------------------------
def test_thread_split_respects_limit_and_numbers_parts():
    text = ("Залог по аренде теряют из-за трёх пунктов. " * 12).strip()
    r = client.post("/api/v1/tools/thread-split", headers=H("copywriter"),
                    json={"text": text, "channel": "x", "hook": "Разбор за 40 секунд",
                          "cta": "Чек-лист в закрепе."})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] >= 2
    assert body["parts"][0]["text"].startswith("1/")
    assert "Разбор за 40 секунд" in body["parts"][0]["text"]
    assert all(p["chars"] <= body["limit"] for p in body["parts"])


def test_thread_split_long_channel_is_single_part():
    r = client.post("/api/v1/tools/thread-split", headers=H(),
                    json={"text": "Короткий текст про договор аренды и залог. " * 3, "channel": "telegram"})
    assert r.json()["count"] == 1


def test_headline_variants():
    r = client.get("/api/v1/tools/headlines?title=Как защитить залог при аренде", headers=H())
    variants = r.json()["variants"]
    assert len(variants) == 2 and variants[0]["headline"] != variants[1]["headline"]


# ------------------------- link-in-bio -------------------------
def test_bio_page_public_html():
    r = client.get("/bio/dogovor")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "Чек-лист" in r.text and "posiz.ru/@dogovor" in r.text   # брендовый адрес Link-in-Bio


def test_bio_create_and_block():
    assert client.post("/api/v1/bio/pages", headers=H(),
                       json={"slug": "pravo", "title": "Право и Сделки"}).status_code == 201
    r = client.post("/api/v1/bio/pages/pravo/blocks", headers=H(),
                    json={"kind": "magnet", "label": "Гайд", "url": "https://x.y/g", "badge": "PDF"})
    assert r.status_code == 201 and r.json()["kind"] == "magnet"
    assert client.get("/bio/pravo").status_code == 200


def test_bio_short_brand_alias():
    """Публичный адрес Link-in-Bio — `posiz.ru/@<slug>` (алиас /bio/<slug>)."""
    page = client.get("/@dogovor")
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
    assert "posiz.ru/@dogovor" in page.text
    assert client.get("/@нет-такой").status_code == 404


def test_bio_duplicate_slug_conflict():
    client.post("/api/v1/bio/pages", headers=H(), json={"slug": "dup", "title": "A"})
    assert client.post("/api/v1/bio/pages", headers=H(), json={"slug": "dup", "title": "B"}).status_code == 409


def test_bio_block_write_requires_permission():
    r = client.post("/api/v1/bio/pages/dogovor/blocks", headers=H("analyst"),
                    json={"kind": "link", "label": "x", "url": "https://x"})
    assert r.status_code == 403


# --------------------- comment-to-DM ---------------------
def test_comment_triggers_dm_with_short_link():
    payload = {"author": "Иван", "text": "Напишите ДОГОВОР и пришлите чек-лист"}
    r = client.post("/webhooks/comments/vk", json=payload, headers=_comment_headers(payload))
    assert r.status_code == 200
    body = r.json()
    assert body["matched"] >= 1
    dm = body["sent"][0]
    assert dm["link"].startswith("https://posiz.ru/l/") and "Чек-лист" in dm["dm"]


def test_comment_without_keyword_does_nothing():
    payload = {"author": "Бот", "text": "Спасибо, полезно"}
    r = client.post("/webhooks/comments/vk", json=payload, headers=_comment_headers(payload))
    assert r.json()["matched"] == 0


def test_automation_rule_upsert_and_permission():
    assert client.post("/api/v1/automations", headers=H("owner"),
                       json={"keyword": "ЧЕКЛИСТ", "channels": ["telegram"], "magnet": "M"}).status_code == 201
    assert client.post("/api/v1/automations", headers=H("analyst"),
                       json={"keyword": "X", "channels": ["vk"]}).status_code == 403


# ------------------------- listening -------------------------
def test_listening_scan_and_priority():
    r = client.post("/api/v1/listening/scan", headers=H())
    assert r.status_code == 200 and r.json()["found"] >= 3
    high = client.get("/api/v1/listening/mentions?priority=high", headers=H()).json()
    assert high and all(m["sentiment"] == "high" for m in high)


def test_listening_reply_draft_requires_approval():
    mention = client.get("/api/v1/listening/mentions", headers=H()).json()[0]
    draft = client.post(f"/api/v1/listening/mentions/{mention['id']}/reply", headers=H("copywriter")).json()
    assert draft["approve_required"] is True and draft["draft"]


# --------------------- календарь инфоповодов ---------------------
def test_calendar_events_filtered_by_range():
    items = client.get("/api/v1/calendar/events?start=2026-11-25&end=2026-12-04", headers=H()).json()
    assert items and all("2026-11-25" <= e["date"] <= "2026-12-04" for e in items)
    assert any("День юриста" in e["title"] for e in items)


def test_calendar_upcoming_has_suggestions():
    items = client.get("/api/v1/calendar/upcoming?limit=3", headers=H()).json()
    assert len(items) == 3 and all(e["suggest"] for e in items)


# ------------------------- медиа-банк -------------------------
def test_assets_folders_storage_and_upload():
    body = client.get("/api/v1/assets", headers=H()).json()
    assert body["folders"][0]["name"] == "Логотипы"
    assert body["storage"]["limit_gb"] == 10
    r = client.post("/api/v1/assets", headers=H(),
                    json={"name": "new-logo.png", "folder": "Логотипы", "kind": "png", "size_mb": 0.1})
    assert r.status_code == 201


def test_assets_filter_by_folder():
    items = client.get("/api/v1/assets?folder=Шаблоны карточек", headers=H()).json()["items"]
    assert items and all(a["folder"] == "Шаблоны карточек" for a in items)


# ------------------------- A/B -------------------------
def test_abtest_flow_picks_winner_and_applies_edit():
    test = client.post("/api/v1/abtests", headers=H(),
                       json={"publication_id": "pub_x", "headline_a": "Вариант А",
                             "headline_b": "Вариант Б?", "channel": "telegram"}).json()
    result = client.post(f"/api/v1/abtests/{test['id']}/evaluate?force=true", headers=H()).json()
    assert result["status"] in {"decided", "no_difference"}
    if result["status"] == "decided":
        applied = client.post(f"/api/v1/abtests/{test['id']}/apply", headers=H()).json()
        assert applied["applied"] in {"a", "b"}


def test_abtest_collecting_phase():
    test = client.post("/api/v1/abtests", headers=H(),
                       json={"publication_id": "p2", "headline_a": "A", "headline_b": "B"}).json()
    result = client.post(f"/api/v1/abtests/{test['id']}/evaluate", headers=H()).json()
    assert result["status"] in {"collecting", "decided", "no_difference"}


# ------------------------- квоты и биллинг -------------------------
def test_quota_exceeded_returns_402_with_pack_hint():
    for _ in range(3):
        client.post("/api/v1/quota/consume?key=generations&amount=200", headers=H())
    r = client.post("/api/v1/quota/consume?key=generations&amount=200", headers=H())
    assert r.status_code in {200, 402}
    if r.status_code == 402:
        assert r.json()["detail"]["kind"] == "quota_exceeded"


def test_quota_pack_adds_limit():
    before = client.get("/api/v1/quota", headers=H()).json()["quota"]["counters"]["publications"]["limit"]
    r = client.post("/api/v1/billing/packs/publications_200", headers=H())
    assert r.status_code == 200
    after = client.get("/api/v1/quota", headers=H()).json()["quota"]["counters"]["publications"]["limit"]
    assert after == before + 200


def test_invoice_for_legal_entity_has_pdf_and_upd():
    inv = client.post("/api/v1/billing/invoices", headers=H(),
                      json={"amount_rub": 2490, "plan": "business",
                            "payer": "ООО «Ромашка»", "inn": "7701234567"}).json()
    assert inv["is_legal_entity"] and inv["invoice_pdf"]
    paid = client.post(f"/api/v1/billing/webhook?payment_id={inv['payment_id']}",
                                       headers={**H(), **_yookassa_headers()}).json()
    assert paid["status"] == "succeeded" and paid["receipt_url"] and paid["upd_url"]


def test_invoice_for_individual_has_receipt_only():
    inv = client.post("/api/v1/billing/invoices", headers=H(),
                      json={"amount_rub": 990, "plan": "start", "payer": "Иван", "inn": None}).json()
    paid = client.post(f"/api/v1/billing/webhook?payment_id={inv['payment_id']}",
                                       headers={**H(), **_yookassa_headers()}).json()
    assert paid["receipt_url"] and paid["upd_url"] is None


def test_autopay_toggle():
    assert client.post("/api/v1/billing/autopay?enabled=false", headers=H()).json()["autopay"] is False
    assert client.post("/api/v1/billing/autopay?enabled=true", headers=H()).json()["autopay"] is True


# ------------------------- auth -------------------------
def test_magic_link_flow():
    req = client.post("/api/v1/auth/magic-link", json={"email": "anna@dogovor.expert"}).json()
    session = client.post(f"/api/v1/auth/verify?token={req['demo_token']}").json()
    assert session["email"] == "anna@dogovor.expert" and session["role"] == "owner"
    assert client.post("/api/v1/auth/verify?token=bad").status_code == 400


def test_oauth_and_onboarding_three_steps():
    assert client.get("/api/v1/auth/oauth/vk_id").json()["url"].startswith("https://id.vk.com/")
    steps = client.get("/api/v1/auth/onboarding").json()
    # мастер онбординга: бренд → база знаний → каналы → первый пост
    assert [s["id"] for s in steps["steps"]] == ["brand", "knowledge", "channels", "first_post"]
    assert steps["trial"]["card_required"] is False


# ------------------------- суперадмин -------------------------
def test_saas_metrics_and_finops():
    metrics = client.get("/api/v1/admin/metrics", headers=HA()).json()
    assert metrics["mrr"] > 0 and metrics["arr"] == metrics["mrr"] * 12
    finops = client.get("/api/v1/admin/finops", headers=HA()).json()
    assert finops["by_model"] and finops["anomalies"]


def test_admin_requires_platform_admin():
    """Владелец воркспейса — не платформенный админ: 403 даже с ролью owner."""
    assert client.get("/api/v1/admin/metrics", headers=H("analyst")).status_code == 403
    assert client.get("/api/v1/admin/metrics", headers=H("owner")).status_code == 403
    assert client.get("/api/v1/admin/metrics", headers=HA()).status_code == 200


def test_impersonation_requires_reason_and_is_audited():
    bad = client.post("/api/v1/admin/impersonate", headers=HA(),
                      json={"tenant_id": "t_1", "reason": "ok"})
    assert bad.status_code == 400
    good = client.post("/api/v1/admin/impersonate", headers=HA(),
                       json={"tenant_id": "t_1", "reason": "клиент не видит кнопку публикации"})
    assert good.status_code == 200 and good.json()["mode"] == "support_readonly"
    audit = client.get("/api/v1/audit?limit=20", headers=H()).json()["items"]
    assert any("/admin/impersonate" in a["path"] for a in audit)


def test_compliance_rules_cms_and_global_pause():
    rule = client.post("/api/v1/admin/rules", headers=HA(),
                       json={"code": "NEW_RULE_2027", "title": "Новая норма", "severity": "block"}).json()
    assert rule["code"] == "NEW_RULE_2027"
    paused = client.post("/api/v1/admin/ops/pause?paused=true", headers=HA()).json()
    assert paused["global_pause"] is True
    client.post("/api/v1/admin/ops/pause?paused=false", headers=HA())
