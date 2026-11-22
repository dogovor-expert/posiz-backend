"""Перелинковка по смыслу и обновление вечнозелёных статей (этап I5).

Что проверяется — и почему именно это:

1. **Близость считается, а не подбирается.** Две статьи об одном дают косинус около 0.7–0.8,
   статьи о разном — около нуля. Тест фиксирует и обратное: хэш-эмбеддинг из `llm` этого
   различия не даёт, поэтому перелинковка на нём была бы шумом (документированный урок).
2. **Порог и лимит — параметры.** Ниже порога ссылка не ставится, и отказ называется
   (`no_candidates`), а не превращается в пустой блок «Читайте также».
3. **Уже связанное не дублируется** (`already_linked`), ссылка на себя и без текста ловится
   проверками (`link_self`, `link_anchor_missing`).
4. **Обновление меняет только подтверждённое.** Число без материала остаётся прежним и попадает
   в `skipped`; нет изменений — `no_changes`, и версия статьи не растёт.
5. **Срок ревью — из окружения**, а не «когда-нибудь»: просрочка видна в отчёте.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from orbita import article_links, articles
from orbita.main import app

client = TestClient(app)
H = {"X-Actor-Role": "owner"}

TEXT = ("Разбираем по существу, как считается охват и что в нём важно, чтобы не платить "
        "за пустой показ. ") * 12

NOW_LATE = "2031-06-01T00:00:00+00:00"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in ("ARTICLE_LINK_MIN_SCORE", "ARTICLE_MAX_LINKS", "ARTICLE_REVIEW_DAYS"):
        monkeypatch.delenv(name, raising=False)
    articles.reset()
    article_links.reset()
    yield
    articles.reset()
    article_links.reset()


def _spec(topic: str, heads: list[str], keywords: list[str] | None = None, **extra) -> dict:
    outline = [{"heading": head, "text": TEXT} for head in heads]
    for override in extra.pop("sections_data", []):
        index = override.pop("index")
        outline[index].update(override)
    brief = {"topic": topic, "keywords": keywords or [topic.split(":")[0].lower()],
             "sections": len(heads), "outline": outline, **extra}
    result = articles.compose(brief)
    assert result["ok"], result
    return result["spec"]


def _corpus() -> dict[str, dict]:
    """Три статьи: две об охватах, одна о ценах — как в живом блоге."""
    docs = {
        "ohvaty": _spec("Как читать отчёт по охватам и переходам",
                        ["Охваты и переходы", "Отчёт в кабинете", "Цена действия"],
                        ["отчёт по охватам", "переходы"]),
        "cifry": _spec("Что показывает отчёт по охватам в цифрах",
                       ["Охваты и переходы", "Цена действия", "Как менять канал"],
                       ["отчёт по охватам", "цена действия"]),
        "prays": _spec("Прайс на ведение канала: пакеты и сроки",
                       ["Пакеты", "Сроки", "Оплата"],
                       ["прайс", "ведение канала"]),
    }
    for spec in docs.values():
        articles.publish(spec)
    article_links.index_all(articles.articles(20))
    return docs


# ------------------------------------------------------------------ близость
def test_same_topic_is_closer_than_other_topic():
    same = article_links.cosine(
        article_links.embed("Как читать отчёт по охватам: переходы и цена действия"),
        article_links.embed("Отчёт по охватам: охваты, переходы и цена действия в цифрах"))
    other = article_links.cosine(
        article_links.embed("Как читать отчёт по охватам: переходы и цена действия"),
        article_links.embed("Прайс на ведение канала: пакеты и сроки"))
    assert same > 0.5, same
    assert other < 0.14, other
    assert same > other * 4


def test_hash_embedding_cannot_tell_topics_apart():
    """Урок: косинус у хэш-эмбеддинга случаен, поэтому близость считается своим вектором."""
    gap = article_links.random_embed_gap()
    assert gap["ours"]["related"] > 0.5
    assert gap["ours"]["unrelated"] < 0.14
    assert abs(gap["hash"]["related"] - gap["hash"]["unrelated"]) < 0.05
    assert gap["ours"]["related"] - gap["ours"]["unrelated"] > 5 * abs(
        gap["hash"]["related"] - gap["hash"]["unrelated"])


def test_embedding_ignores_case_and_service_words():
    left = article_links.embed("Как читать отчёт по охватам")
    right = article_links.embed("как читать отчёт по охватам")
    assert article_links.cosine(left, right) > 0.99


# ------------------------------------------------------------------ кандидаты
def test_candidates_are_ranked_by_similarity():
    docs = _corpus()
    rows = article_links.candidates(docs["ohvaty"], spec=docs["ohvaty"])
    assert rows, rows
    assert rows[0]["slug"] == articles.article(
        next(doc["id"] for doc in articles.articles(10)
             if doc["slug"] == "chto-pokazyvaet-otchet-po-ohvatam-v-tsifrah"))["slug"]
    assert rows[0]["score"] > rows[-1]["score"]
    assert rows[0]["anchor"].startswith("Что показывает")


def test_link_plan_explains_empty_corpus():
    spec = _spec("Как читать отчёт по охватам", ["Охваты", "Переходы", "Итог"])
    plan = article_links.link_plan(spec)
    assert plan["ok"] is False
    assert plan["code"] == "corpus_empty"
    assert "перелинковывать не с чем" in plan["reason"]


def test_link_plan_respects_threshold():
    docs = _corpus()
    plan = article_links.link_plan(docs["prays"], min_score=0.9)
    assert plan["ok"] is False
    assert plan["code"] == "no_candidates"
    assert plan["best_score"] < 0.9
    assert "ниже порога" in plan["reason"]


def test_link_plan_limit_comes_from_env(monkeypatch):
    docs = _corpus()
    monkeypatch.setenv("ARTICLE_MAX_LINKS", "1")
    plan = article_links.link_plan(docs["ohvaty"])
    assert plan["ok"] is True
    assert len(plan["links"]) == 1
    assert plan["limit"] == 1


def test_link_plan_does_not_repeat_existing_links():
    docs = _corpus()
    plan = article_links.link_plan(docs["ohvaty"])
    linked = article_links.apply_links(docs["ohvaty"], plan["links"])
    assert linked["ok"] is True
    again = article_links.link_plan(linked["spec"])
    assert again["ok"] is False
    assert again["code"] == "already_linked"
    assert "уже связаны" in again["reason"]


# ------------------------------------------------------------------ блок и разметка
def test_apply_links_puts_block_before_cta_with_history():
    docs = _corpus()
    plan = article_links.link_plan(docs["ohvaty"])
    linked = article_links.apply_links(docs["ohvaty"], plan["links"])
    spec = linked["spec"]
    types = [block["type"] for block in spec["blocks"]]
    block = spec["blocks"][types.index("related")]
    assert types.index("related") < types.index("cta")
    assert block["reason"], "у блока должна быть причина появления"
    assert spec["history"][-1]["action"] == "links"
    assert spec["history"][-1]["reason"]
    assert linked["added"] == len(block["props"]["items"])


def test_related_block_renders_anchors_and_no_bare_read_more():
    docs = _corpus()
    plan = article_links.link_plan(docs["ohvaty"])
    linked = article_links.apply_links(docs["ohvaty"], plan["links"])
    html = articles.render_html(linked["spec"], standalone=False)
    assert '<nav class="related">' in html
    assert "<h2>Читайте также</h2>" in html
    nav = html.split('<nav class="related">')[1].split("</nav>")[0]
    assert nav.count("<a href=") == len(plan["links"])
    assert "читать далее" not in html.lower()
    assert 'src="http' not in html


def test_check_links_catches_broken_duplicate_self_and_anchor():
    spec = _spec("Как читать отчёт по охватам", ["Охваты", "Переходы", "Итог"])
    canonical = "https://example.com/blog/otchet/"
    spec["seo"]["canonical"] = canonical
    spec["blocks"].insert(len(spec["blocks"]) - 1, {
        "id": "blk_rel_test", "type": "related", "reason": "проверка",
        "props": {"items": [
            {"url": "", "anchor": "Пустая ссылка"},
            {"url": canonical, "anchor": "На себя"},
            {"url": "/blog/ohvaty", "anchor": ""},
            {"url": "/blog/ohvaty", "anchor": "Дубль"},
        ]}})
    codes = {issue["code"] for issue in articles.check(spec)["issues"]}
    assert {"link_broken", "link_self", "link_anchor_missing", "link_duplicate"} <= codes


def test_empty_related_block_is_reported():
    spec = _spec("Как читать отчёт по охватам", ["Охваты", "Переходы", "Итог"])
    spec["blocks"].insert(1, {"id": "blk_rel_empty", "type": "related", "reason": "пусто",
                              "props": {"items": []}})
    codes = {issue["code"] for issue in articles.check(spec)["issues"]}
    assert "related_empty" in codes


# ------------------------------------------------------------------ вечнозелёные
def test_evergreen_mark_has_review_window(monkeypatch):
    monkeypatch.setenv("ARTICLE_REVIEW_DAYS", "90")
    spec = _spec("Как читать отчёт по охватам", ["Охваты", "Переходы", "Итог"], evergreen=True)
    assert spec["evergreen"]["review_days"] == 90
    state = article_links.freshness(spec)
    assert state["evergreen"] is True
    assert state["due"] is False
    assert state["age_days"] == 0


def test_freshness_becomes_due_after_window():
    spec = _spec("Как читать отчёт по охватам", ["Охваты", "Переходы", "Итог"], evergreen=True)
    late = article_links.freshness(spec, now=NOW_LATE)
    assert late["due"] is True
    assert late["age_days"] > late["review_days"]
    assert "ревью было" in late["reason"]


def test_article_without_mark_is_not_evergreen():
    spec = _spec("Как читать отчёт по охватам", ["Охваты", "Переходы", "Итог"])
    state = article_links.freshness(spec, now=NOW_LATE)
    assert state["evergreen"] is False
    assert "не помечена вечнозелёной" in state["reason"]


def _data_spec() -> dict:
    return _spec("Как читать отчёт по охватам и переходам",
                 ["Охваты и переходы", "Цена действия", "Отчёт в кабинете", "Что дальше"],
                 ["отчёт по охватам"], evergreen=True,
                 sections_data=[{"index": 0, "columns": ["Пакет", "Цена"],
                                 "rows": [["Базовый", "9 000 ₽"]], "source": "прайс клиента"}])


def test_refresh_updates_only_numbers_with_source():
    spec = _data_spec()
    table = next(block for block in spec["blocks"] if block["type"] == "table")
    assert table["source"] == "прайс клиента"
    result = article_links.refresh(spec, [{"key": "прайс клиента",
                                           "rows": [["Базовый", "6 900 ₽"]]}], now=NOW_LATE)
    assert result["ok"] is True
    assert result["version"] == 2
    updated = next(block for block in result["spec"]["blocks"] if block["type"] == "table")
    assert updated["props"]["rows"] == [["Базовый", "6 900 ₽"]]
    assert result["spec"]["updated_at"] == result["updated_at"]
    history = result["spec"]["history"][-1]
    assert history["action"] == "refresh" and history["reason"]


def test_refresh_keeps_number_when_material_missing():
    spec = _data_spec()
    plan = article_links.refresh_plan(spec, [{"key": "другой ряд", "rows": [["X", "1"]]}],
                                      now=NOW_LATE)
    assert plan["ok"] is False
    assert plan["code"] == "no_materials"
    assert plan["skipped"], "пропуск обязан быть назван, а не проглочен"
    assert plan["skipped"][0]["code"] in {"material_missing", "source_missing"}
    kept = article_links.refresh(spec, [{"key": "другой ряд", "rows": [["X", "1"]]}],
                                 now=NOW_LATE)
    assert kept["ok"] is False
    table = next(block for block in kept["spec"]["blocks"] if block["type"] == "table")
    assert table["props"]["rows"] == [["Базовый", "9 000 ₽"]]


def test_refresh_without_changes_does_not_bump_version():
    spec = _data_spec()
    first = article_links.refresh(spec, [{"key": "прайс клиента",
                                          "rows": [["Базовый", "6 900 ₽"]]}], now=NOW_LATE)
    same = article_links.refresh(first["spec"], [{"key": "прайс клиента",
                                                  "rows": [["Базовый", "6 900 ₽"]]}],
                                 now="2032-06-01T00:00:00+00:00")
    assert same["ok"] is False
    assert same["code"] == "no_changes"
    assert same["spec"]["version"] == first["version"]


def test_refresh_refuses_while_fresh():
    spec = _data_spec()
    result = article_links.refresh(spec, [{"key": "прайс клиента",
                                           "rows": [["Базовый", "6 900 ₽"]]}])
    assert result["ok"] is False
    assert result["code"] == "fresh"
    assert result["spec"]["version"] if "version" in result["spec"] else True


def test_evergreen_report_counts_due():
    _corpus()
    spec = _spec("Как читать отчёт по охватам: срок", ["Охваты", "Переходы", "Итог"],
                 evergreen=True)
    articles.publish(spec)
    report = article_links.evergreen_report(articles.articles(20), now=NOW_LATE)
    assert report["total"] >= 4
    assert report["evergreen"] == 1
    assert report["due"] == 1
    assert report["review_days"] == 180


# ------------------------------------------------------------------ API
def test_api_links_endpoint_applies_and_reports_version():
    docs = _corpus()
    target = next(doc for doc in articles.articles(20) if doc["slug"] == "kak-chitat-otchet-po-ohvatam-i-perehodam")
    preview = client.get(f"/api/v1/articles/{target['id']}/links", headers=H)
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["ok"] is True
    assert body["links"][0]["score"] > body["threshold"]
    applied = client.post(f"/api/v1/articles/{target['id']}/links", json={}, headers=H)
    assert applied.status_code == 200, applied.text
    payload = applied.json()
    assert payload["added"] >= 1
    assert payload["version"] == 2
    assert payload["stats"]["links"] == payload["added"]
    again = client.post(f"/api/v1/articles/{target['id']}/links", json={}, headers=H)
    assert again.status_code == 409
    assert again.json()["detail"]["kind"] == "already_linked"
    assert docs  # корпус собран заранее и не пострадал


def test_api_refresh_and_evergreen_report():
    spec = _data_spec()
    published = articles.publish(spec)["article"]
    refused = client.post(f"/api/v1/articles/{published['id']}/refresh",
                          json={"materials": [{"key": "прайс клиента",
                                               "rows": [["Базовый", "6 900 ₽"]]}]},
                          headers=H)
    assert refused.status_code == 409
    assert refused.json()["detail"]["kind"] == "fresh"
    report = client.get("/api/v1/articles/evergreen", params={"now": NOW_LATE}, headers=H)
    assert report.status_code == 200, report.text
    assert report.json()["due"] == 1
    refreshed = client.post(f"/api/v1/articles/{published['id']}/refresh",
                            json={"materials": [{"key": "прайс клиента",
                                                 "rows": [["Базовый", "6 900 ₽"]]}],
                                  "now": NOW_LATE, "reason": "прайс обновлён в ноябре"},
                            headers=H)
    assert refreshed.status_code == 200, refreshed.text
    body = refreshed.json()
    assert body["version"] == 2
    assert body["changes"][0]["field"].endswith(".rows")
    assert body["freshness"]["due"] is False
    assert body["updated_at"] == NOW_LATE


# ------------------------------------------------------------------ canonical
def test_canonical_appears_only_when_platform_domain_is_known(monkeypatch):
    monkeypatch.delenv("ARTICLE_CANONICAL_BASE", raising=False)
    spec = _spec("Как читать отчёт по охватам", ["Охваты", "Переходы", "Итог"])
    assert spec["seo"]["canonical"] is None
    assert 'rel="canonical"' not in articles.render_html(spec)
    monkeypatch.setenv("ARTICLE_CANONICAL_BASE", "https://posiz.ru/blog")
    spec = _spec("Как читать отчёт по охватам", ["Охваты", "Переходы", "Итог"])
    assert spec["seo"]["canonical"] == "https://posiz.ru/blog/kak-chitat-otchet-po-ohvatam/"
    assert 'rel="canonical"' in articles.render_html(spec)


def test_own_canonical_wins_over_platform_domain(monkeypatch):
    monkeypatch.setenv("ARTICLE_CANONICAL_BASE", "https://posiz.ru/blog")
    spec = _spec("Как читать отчёт по охватам", ["Охваты", "Переходы", "Итог"],
                 canonical="https://client.example.com/otchet/")
    assert spec["seo"]["canonical"] == "https://client.example.com/otchet/"
