"""Smoke-тесты контракта и ключевых сценариев."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from orbita.main import app

client = TestClient(app)


def _signal(title: str = "Как защитить залог при аренде") -> str:
    r = client.post("/api/v1/signals", json={"kind": "manual", "title": title, "goal": "lead"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _generate(signal_id: str, channels=("max", "vk", "x")) -> dict:
    r = client.post(f"/api/v1/signals/{signal_id}/generate", json={"channels": list(channels)})
    assert r.status_code == 200, r.text
    return r.json()


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_channels():
    r = client.get("/api/v1/channels")
    assert r.status_code == 200
    channels = {c["channel"] for c in r.json()}
    assert {"max", "vk", "telegram", "x"} <= channels


def test_telegram_marked_as_high_risk():
    channels = {c["channel"]: c for c in client.get("/api/v1/channels").json()}
    assert channels["telegram"]["health"]["delivery_risk"] == "high"


def test_signal_generate_publish_flow():
    sid = _signal()
    out = _generate(sid)
    assert len(out["variants"]) == 3
    assert out["cost_rub"] > 0

    x_variant = next(v for v in out["variants"] if v["channel"] == "x")
    assert len(x_variant["body"]) <= 280
    assert x_variant["link_in_reply"] is True          # экономия: ссылка в ответе

    accounts = {c["channel"]: c["id"] for c in client.get("/api/v1/channels").json()}
    r = client.post("/api/v1/publications", json={
        "variant_id": x_variant["id"],
        "channel_account_id": accounts["x"],
        "scheduled_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    })
    assert r.status_code == 201, r.text
    pub = r.json()
    assert pub["status"] == "queued"
    assert pub["cost_estimate_rub"] == pytest.approx(1.2)   # $0,015

    # идемпотентность: повторный запрос не создаёт дубль
    r2 = client.post("/api/v1/publications", json={
        "variant_id": x_variant["id"],
        "channel_account_id": accounts["x"],
        "scheduled_at": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
    })
    assert r2.json()["id"] == pub["id"]

    # публикация в dry-run
    r3 = client.post(f"/api/v1/publications/{pub['id']}/publish-now")
    assert r3.status_code == 200
    assert r3.json()["status"] == "published"


def test_x_link_in_text_costs_16_rub():
    sid = _signal("Акция: скидка 30%")
    out = _generate(sid, channels=("x",))
    variant = out["variants"][0]
    # принудительно «ссылка в тексте»
    from orbita import store
    store.VARIANTS[variant["id"]]["link_in_reply"] = False
    store.VARIANTS[variant["id"]]["link_url"] = "https://dogovor.expert"
    accounts = {c["channel"]: c["id"] for c in client.get("/api/v1/channels").json()}
    r = client.post("/api/v1/publications", json={
        "variant_id": variant["id"],
        "channel_account_id": accounts["x"],
        "scheduled_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "erid": "2VtzqwXyZ1A9",
    })
    assert r.status_code == 201, r.text
    assert r.json()["cost_estimate_rub"] == pytest.approx(16.0)  # $0,20


def test_compliance_blocks_ad_without_erid():
    sid = _signal("Скидка 30% на комплект документов")
    out = _generate(sid, channels=("vk",))
    variant = out["variants"][0]
    accounts = {c["channel"]: c["id"] for c in client.get("/api/v1/channels").json()}

    r = client.post("/api/v1/publications", json={
        "variant_id": variant["id"],
        "channel_account_id": accounts["vk"],
        "scheduled_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    })
    assert r.status_code == 409
    report = r.json()["detail"]["compliance"]
    assert report["verdict"] == "ad_requires_marking"
    assert any(x["rule_code"] == "AD_MARKING_REQUIRED" for x in report["reasons"])


def test_compliance_passes_with_erid():
    sid = _signal("Скидка 30% на комплект документов")
    out = _generate(sid, channels=("vk",))
    variant = out["variants"][0]
    accounts = {c["channel"]: c["id"] for c in client.get("/api/v1/channels").json()}
    r = client.post("/api/v1/publications", json={
        "variant_id": variant["id"],
        "channel_account_id": accounts["vk"],
        "scheduled_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "erid": "2VtzqwXyZ1A9",
    })
    assert r.status_code == 201, r.text


def test_informational_post_needs_no_marking():
    sid = _signal("Что изменилось в требованиях к ЭЦП")
    out = _generate(sid, channels=("max",))
    r = client.post(f"/api/v1/variants/{out['variants'][0]['id']}/compliance-check")
    assert r.status_code == 200
    assert r.json()["verdict"] == "informational"


def test_analytics_summary_shape():
    r = client.get("/api/v1/analytics/summary")
    assert r.status_code == 200
    body = r.json()
    for key in ("impressions", "reactions", "clicks", "cost_rub", "by_channel"):
        assert key in body
