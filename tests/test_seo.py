"""SEO-контур Posiz: robots, sitemap, JSON-LD, noindex-заголовки и бесплатные утилиты.

Стратегия продвижения проверяется как код: карта URL не расходится с каталогом каналов,
служебные страницы закрыты от индексации, разметка содержит тарифы и FAQ,
а `/tools/*` работают без авторизации и не хранят данные пользователя.
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET

import pytest
from fastapi.testclient import TestClient

from orbita import catalog, publictools, ratelimit, seo
from orbita.main import app

client = TestClient(app)


# ---------------------------------------------------------------- robots
def test_robots_closes_service_zones_and_points_to_sitemap():
    text = client.get("/robots.txt").text
    assert "User-agent: *" in text and "Allow: /" in text
    for blocked in ("Disallow: /l/", "Disallow: /@", "Disallow: /api/", "Disallow: /auth/"):
        assert blocked in text, blocked
    assert f"Sitemap: {seo.SITE}/sitemap.xml" in text


def test_app_robots_disallows_everything():
    assert client.get("/app-robots.txt").text.strip().endswith("Disallow: /")


# ---------------------------------------------------------------- sitemap
def test_sitemap_is_valid_xml_with_clusters():
    resp = client.get("/sitemap.xml")
    assert resp.status_code == 200 and "xml" in resp.headers["content-type"]
    root = ET.fromstring(resp.text)
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    locs = [node.text for node in root.findall("sm:url/sm:loc", ns)]
    assert locs, "sitemap пуст"
    urls = " ".join(locs)
    assert f"{seo.SITE}/pricing" in locs
    assert f"{seo.SITE}/integrations/max" in locs
    assert f"{seo.SITE}/sync/telegram-to-max" in locs
    assert f"{seo.SITE}/solutions/lawyers" in locs
    assert f"{seo.SITE}/tools/erid-checker" in locs
    assert any("/blog/compliance/" in url for url in locs)
    # пользовательские и служебные адреса в индекс не попадают
    assert "/l/" not in urls and "/@" not in urls


def test_sitemap_covers_every_catalog_channel():
    locs = {entry["loc"] for entry in seo.sitemap_entries()}
    for item in catalog.CATALOG:
        assert f"{seo.SITE}/integrations/{item.kind}" in locs, item.kind


def test_inventory_matches_plan_targets():
    inv = client.get("/seo/inventory").json()["inventory"]
    assert inv["integrations"] == len(catalog.CATALOG) == 29
    assert inv["sync"] >= 55
    assert inv["solutions"] >= 24
    assert inv["tools"] == 4
    assert inv["blog_articles"] == 21
    # 350+ коммерческих страниц: интеграции + связки + решения + утилиты
    commercial = inv["integrations"] + inv["sync"] + inv["solutions"] + inv["tools"]
    assert commercial >= 100


# ---------------------------------------------------------------- JSON-LD
def test_software_application_markup_has_tariffs_in_rub():
    data = seo.software_application()
    assert data["@type"] == "SoftwareApplication"
    assert data["applicationCategory"] == "BusinessApplication"
    offers = data["offers"]["itemListElement"]
    assert [o["price"] for o in offers] == [0, 990, 2490, 4990, 12900]
    assert all(o["priceCurrency"] == "RUB" for o in offers)
    assert data["inLanguage"] == "ru-RU"


def test_faq_howto_article_breadcrumbs_markup():
    faq = seo.faq_page([("Нужен ли erid?", "Да, если в тексте есть цена.")])
    assert faq["@type"] == "FAQPage"
    assert faq["mainEntity"][0]["acceptedAnswer"]["text"].startswith("Да")

    how = seo.how_to("Подключить MAX", [("Верификация", "Госуслуги"), ("Бот", "@MasterBot")])
    assert how["@type"] == "HowTo" and len(how["step"]) == 2
    assert how["step"][0]["position"] == 1

    art = seo.article("Маркировка в MAX", author="Юрист", section="Законы",
                      published="2026-11-17", url=f"{seo.SITE}/blog/compliance/a")
    assert art["@type"] == "Article" and art["author"]["@type"] == "Person"
    assert art["headline"] and art["datePublished"] == "2026-11-17"

    crumbs = seo.breadcrumbs([("Главная", seo.SITE), ("Блог", f"{seo.SITE}/blog")])
    assert [i["position"] for i in crumbs["itemListElement"]] == [1, 2]


def test_jsonld_script_is_valid_json():
    script = seo.head_tags("software")
    assert script.startswith('<script type="application/ld+json">')
    payload = script.split(">", 1)[1].rsplit("</script>", 1)[0]
    assert json.loads(payload)["@type"] == "SoftwareApplication"


def test_schema_endpoint_serves_all_kinds():
    for kind in ("software", "faq", "howto", "article", "breadcrumbs"):
        resp = client.get(f"/seo/schema/{kind}")
        assert resp.status_code == 200, kind
        assert "@context" in resp.json()
    assert client.get("/seo/schema/unknown").status_code == 404


def test_tldr_block_for_neurosearch():
    block = seo.tldr(["С 10 000 подписчиков — реестр РКН",
                      "erid нужен, если в тексте есть цена"])
    assert "Коротко" in block and block.count("<li>") == 2
    assert "<script" not in block and block.startswith("<aside")


# ---------------------------------------------------------------- noindex
def test_short_links_and_bio_are_noindex():
    link = client.post("/api/v1/links", headers={"X-Actor-Role": "owner"},
                       json={"target_url": "https://dogovor.expert/zalog",
                             "channel": "vk", "campaign": "zalog"}).json()
    redirect = client.get(f"/l/{link['code']}", follow_redirects=False)
    assert redirect.headers["x-robots-tag"] == "noindex, nofollow"
    bio = client.get("/@dogovor")
    assert bio.headers["x-robots-tag"] == "noindex, nofollow"
    bio_alias = client.get("/bio/dogovor")
    assert bio_alias.headers["x-robots-tag"] == "noindex, nofollow"
    # обычные страницы API не закрываются
    assert "x-robots-tag" not in client.get("/api/v1/bio/pages").headers


# ---------------------------------------------------------------- утилиты
def test_tools_index_is_public():
    resp = client.get("/tools/")
    assert resp.status_code == 200
    slugs = [tool["slug"] for tool in resp.json()["tools"]]
    assert slugs == ["erid-checker", "risk-calculator", "thread-splitter", "utm-builder"]
    for tool in resp.json()["tools"]:
        assert tool["url"].startswith(f"{seo.SITE}/tools/")


def test_erid_checker_detects_ad_and_builds_marking():
    ad = client.post("/tools/erid-checker", json={
        "text": "Скидка 30%! Закажите проверку договора за 4 900 ₽: https://dogovor.expert",
        "advertiser": "ООО «Договор Эксперт»", "inn": "7701234567"}).json()
    assert ad["needs_marking"] is True
    assert ad["risk"] == "high"
    assert "Реклама. ООО «Договор Эксперт», ИНН 7701234567" in ad["marking_block"]
    assert any("ЕРИР" in item for item in ad["checklist"])

    info = client.post("/tools/erid-checker",
                       json={"text": "Вышел новый разбор изменений в ГК РФ"}).json()
    assert info["needs_marking"] is False and info["risk"] == "low"

    marked = client.post("/tools/erid-checker", json={
        "text": "Скидка 30%, закажите со сметой за 4900 ₽", "erid": "2VtzqwXyZ1A9"}).json()
    assert marked["needs_marking"] is False   # erid уже указан


def test_risk_calculator_shows_channel_loss():
    data = client.post("/tools/risk-calculator",
                       json={"channels": ["max", "telegram", "whatsapp"], "audience": 100_000}).json()
    assert data["reach_total"] > 0
    assert 0 < data["at_risk_share"] < 1
    assert data["recommendation"].startswith("Продублировать")
    assert "max" in data["failover"]
    statuses = {row["channel"]: row["status"] for row in data["channels"]}
    assert statuses["whatsapp"] == "blocked" and statuses["max"] == "ok"


def test_utm_builder_makes_link_and_estimates_delivery():
    data = client.post("/tools/utm-builder",
                       json={"target_url": "https://dogovor.expert/zalog", "channel": "x",
                             "campaign": "zalog"}).json()
    assert "utm_source=x" in data["url"] and "utm_campaign=zalog" in data["url"]
    assert data["delivery_rub"]["x"] > data["delivery_rub"]["max"]
    assert data["chosen_channel_cost"] == data["delivery_rub"]["x"]
    assert data["cheapest_channel"] in {"max", "vk", "telegram"}


def test_thread_splitter_respects_channel_limit():
    long_text = ("Первое предложение про залог. " * 40)
    data = client.post("/tools/thread-splitter", json={"text": long_text, "channel": "x"}).json()
    assert data["count"] > 1
    assert all(length <= 280 + 8 for length in data["lengths"])   # + нумерация твита


def test_tools_are_rate_limited(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "1")
    ratelimit.limiter.reset()
    limit, _window = ratelimit.LIMITS["public_tool"]
    payload = {"text": "Скидка 10%, закажите за 1000 ₽"}
    codes = [client.post("/tools/erid-checker", json=payload).status_code
             for _ in range(limit + 1)]
    assert codes[-1] == 429
    ratelimit.limiter.reset()


def test_tools_do_not_touch_client_data():
    """Утилиты не пишут в БД доменов и не требуют авторизации."""
    from orbita import store

    before = (len(store.INBOX), len(store.PUBLICATIONS), len(store.METRICS))
    client.post("/tools/erid-checker", json={"text": "Скидка 5%, заказ за 990 ₽"})
    client.post("/tools/thread-splitter", json={"text": "Текст " * 100, "channel": "x"})
    after = (len(store.INBOX), len(store.PUBLICATIONS), len(store.METRICS))
    assert before == after


@pytest.mark.parametrize("path", ["/tools/erid-checker", "/tools/risk-calculator",
                                  "/tools/utm-builder", "/tools/thread-splitter"])
def test_tools_reject_empty_payload(path):
    assert client.post(path, json={}).status_code == 422


def test_marking_template_documents_fine():
    assert "500 000 ₽" in publictools.erid_check("Скидка 10%")["fine_note"]
    assert "38-ФЗ" in publictools.erid_check("Скидка 10%")["formula"]
