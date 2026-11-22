"""Go-Live: LLM-клиент, PII, RAG и документная персистентность.

Тесты не ходят в сеть: транспорт LLM подменяется ``httpx.MockTransport``.
"""
from __future__ import annotations

import json

import httpx
import pytest

from orbita import docs, knowledge, llm, pii


# ------------------------------------------------------------------ LLM
def _transport(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), timeout=5)


def test_llm_simulated_without_key(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("LLM_ROUTER_URL", raising=False)
    monkeypatch.setenv("DRY_RUN", "1")
    assert llm.enabled() is False
    assert llm.status()["mode"] == "simulated"
    res = llm.get_client().complete("editor", [{"role": "user", "content": "Текст"}],
                                   json_mode=True)
    assert res["simulated"] is True
    assert json.loads(res["text"])["score"] > 0


def test_llm_live_call_uses_heavy_model_and_counts_cost(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["payload"] = json.loads(request.content)
        return httpx.Response(200, json={
            "model": "deepseek-v4-pro",
            "choices": [{"message": {"content": "Готовый текст"}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 500},
        })

    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setenv("LLM_ROUTER_URL", "https://llm.test/v1")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    client = llm.LLM(client=_transport(handler), retries=0)

    before = len(llm.USAGE)
    res = client.complete("copywriter", [{"role": "user", "content": "Текст"}])
    assert res["text"] == "Готовый текст"
    assert res["simulated"] is False
    assert seen["url"].endswith("/chat/completions")
    assert seen["auth"] == "Bearer sk-test"
    assert seen["payload"]["model"] == "deepseek-v4-pro"

    usage = llm.USAGE[before]
    # 1000 входных / 500 выходных при цене 0,10 / 0,40 ₽ за 1K
    assert usage.cost_rub == pytest.approx(0.1 + 0.2, rel=1e-6)
    assert llm.cost_report()["calls"] >= 1
    client.close()


def test_llm_retries_on_429_then_succeeds(monkeypatch):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, text="slow down")
        return httpx.Response(200, json={"model": "deepseek-flash",
                                         "choices": [{"message": {"content": "ok"}}],
                                         "usage": {"prompt_tokens": 10, "completion_tokens": 5}})

    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setenv("LLM_ROUTER_URL", "https://llm.test/v1")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk")
    client = llm.LLM(client=_transport(handler), retries=2, backoff=0.01)
    res = client.complete("adapter", [{"role": "user", "content": "x"}])
    assert res["text"] == "ok" and calls["n"] == 2


def test_llm_raises_after_exhausted_retries(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setenv("LLM_ROUTER_URL", "https://llm.test/v1")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk")
    client = llm.LLM(client=_transport(lambda r: httpx.Response(503, text="down")),
                     retries=1, backoff=0.01)
    with pytest.raises(llm.LLMError):
        client.complete("copywriter", [{"role": "user", "content": "x"}])


def test_llm_masks_pii_before_sending(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"model": "deepseek-flash",
                                         "choices": [{"message": {"content": "Клиент [PHONE_1] получил ответ"}}],
                                         "usage": {"prompt_tokens": 5, "completion_tokens": 5}})

    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setenv("LLM_ROUTER_URL", "https://llm.test/v1")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk")
    monkeypatch.setenv("PII_MASKING", "1")
    client = llm.LLM(client=_transport(handler), retries=0)
    res = client.complete("copywriter", [{"role": "user",
                                         "content": "Позвоните +7 999 123-45-67, это Иван Петров"}])
    sent = captured["messages"][0]["content"]
    assert "+7 999 123-45-67" not in sent and "[PHONE_1]" in sent
    # ответ клиенту — с восстановленными данными
    assert "+7 999 123-45-67" in res["text"]


def test_embeddings_dim_and_similarity(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    vectors = llm.get_client().embed(["договор аренды залог", "договор аренды залог", "погода"])
    assert all(len(v) == llm.EMBED_DIM for v in vectors)
    assert vectors[0] == vectors[1]
    assert vectors[0] != vectors[2]


# ------------------------------------------------------------------ PII
@pytest.mark.parametrize("text,kind", [
    ("Меня зовут Иван Петров", "PERSON"),
    ("телефон +7 (999) 123-45-67", "PHONE"),
    ("ИНН 7707083893", "INN"),
    ("почта ivan@example.ru", "EMAIL"),
    ("СНИЛС 123-456-789 01", "SNILS"),
    ("карта 4276 1234 5678 9012", "CARD"),
])
def test_pii_masks_and_restores(text, kind):
    masked, mapping = pii.mask(text)
    assert f"[{kind}_1]" in masked
    assert pii.restore(masked, mapping) == text


def test_pii_keeps_neutral_text_untouched():
    text = "Договор аренды на 15 минут, тариф 4900 рублей, скидка 3%"
    masked, mapping = pii.mask(text)
    assert masked == text and mapping == {}


def test_pii_audit_report_has_no_values():
    report = pii.audit_report("Иван Петров, +7 999 123-45-67")
    assert report["found"] == 2
    assert "Петров" not in json.dumps(report, ensure_ascii=False)


def test_pii_can_be_disabled(monkeypatch):
    monkeypatch.setenv("PII_MASKING", "0")
    assert pii.enabled() is False


# ------------------------------------------------------------------ RAG
def test_knowledge_seed_and_hybrid_search():
    stats = knowledge.seed_demo()
    assert stats["chunks"] >= 5 and stats["with_embeddings"] == stats["chunks"]
    hits = knowledge.search("сколько стоит подписка тарифы")
    assert hits, "гибридный поиск ничего не нашёл"
    assert hits[0]["title"] in {"Что делает конструктор договоров", "«Дорого» — что отвечаем"}
    assert hits[0]["lex"] > 0 or hits[0]["vec"] != 0


def test_knowledge_lexical_match_wins_on_exact_term():
    knowledge.seed_demo()
    hits = knowledge.search("залог")
    assert hits
    assert "залог" in hits[0]["content"].lower()


def test_knowledge_context_block_returns_citations():
    knowledge.seed_demo()
    ctx = knowledge.context_block("кейс с залогом")
    assert ctx["citations"] and ctx["hits"] >= 1
    assert "card_id" in ctx["citations"][0]


def test_knowledge_chunking_respects_size():
    text = "\n\n".join(["Абзац про договор аренды и залог. " * 6] * 5)
    chunks = knowledge.chunk_text(text)
    assert all(len(c) <= knowledge.CHUNK_MAX for c in chunks)
    assert len(chunks) >= 2


def test_knowledge_index_is_idempotent():
    knowledge.seed_demo()
    src = next(s for s in knowledge.list_sources() if s["location"] == "seed://dogovor")
    first = knowledge.index_source(src["id"])["chunks"]
    second = knowledge.index_source(src["id"])["chunks"]
    assert first == second and first >= 5
    assert knowledge.CHUNKS.count() >= first


# ------------------------------------------------------------------ персистентность доменов
def test_collection_memory_mode_is_honest():
    """DATA_LAYER=memory: объекты процесса не переживают сброс — так и задумано для демо."""
    col = docs.Collection("test_items", seed=[{"id": "a", "v": 1}])
    col.put({"id": "b", "v": 2})
    assert [d["id"] for d in col.all()] == ["a", "b"]
    col.clear_memory()
    assert [d["id"] for d in col.all()] == ["a"]          # остался только сид


def test_collection_persists_in_db_layer(tmp_path, monkeypatch):
    """DATA_LAYER=db: запись переживает «перезапуск» (новый Collection на той же БД)."""
    from orbita import persistence
    from orbita.persistence import Database

    db = Database(f"sqlite:///{tmp_path / 'docs.db'}")
    db.create_all()
    monkeypatch.setenv("DATA_LAYER", "db")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'docs.db'}")
    persistence.set_database(db)
    try:
        col = docs.Collection("bio_pages", seed=[{"id": "seed", "title": "Сид"}], id_field="slug")
        col.put({"id": "saved", "title": "После рестарта", "slug": "saved"})
        fresh = docs.Collection("bio_pages", id_field="slug")   # новый процесс
        titles = {d.get("title") for d in fresh.all()}
        assert "После рестарта" in titles and "Сид" in titles
        assert db.doc_get("bio_pages", "saved")["title"] == "После рестарта"
        fresh.delete("saved")
        assert db.doc_get("bio_pages", "saved") is None
    finally:
        persistence.set_database(None)


def test_domains_use_collections():
    from orbita import abtests, automations, bio, listening

    page = bio.create("golive", "Проверка", "Описание")
    assert bio.get("golive")["title"] == page["title"]
    assert bio.DOCS.name == "bio_pages"

    rule = automations.upsert_rule({"keyword": "тест", "channels": ["vk"]})
    assert rule["id"] in [r["id"] for r in automations.list_rules()]
    assert automations.RULES_DOCS.count() >= 3

    kw = listening.add_keyword("Проверка фразы")
    assert kw["id"] in [k["id"] for k in listening.list_keywords()]

    test = abtests.create("pub_1", "A", "B")
    assert test["id"] in [t["id"] for t in abtests.list_tests()]
