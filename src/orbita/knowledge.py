"""Brand Brain: источники → карточки знаний → чанки с эмбеддингами.

Пайплайн:
1. ``add_source(kind, location)`` — регистрация источника (site/pdf/video/archive/manual);
2. ``ingest(source_id, cards)`` — карточки знаний (product/usp/audience/objection/case/…);
3. ``index_source(source_id)`` — нарезка карточек на чанки 300–600 символов по смысловым
   границам (абзацы, заголовки) и эмбеддинги 1024 (`llm.embed`);
4. ``search(query, workspace_id, top_k)`` — гибридный поиск (BM25 + косинус, RRF-fusion).

Хранение: ``entity_docs`` (коллекции ``knowledge_sources`` / ``knowledge_cards`` /
``knowledge_chunks``) через ``docs.Collection``. Имена коллекций совпадают с именами
таблиц из ``schema.sql`` (``tsv`` + ``embedding vector(1024)`` + HNSW-индекс), поэтому
переход на pgvector-SQL — замена одного метода ``search`` без изменения вызывающего кода.
"""
from __future__ import annotations

import math
import re
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from . import llm
from .docs import Collection

CHUNK_MIN = 300
CHUNK_MAX = 600
TOP_K = 6

SOURCES = Collection("knowledge_sources", id_field="id")
CARDS = Collection("knowledge_cards", id_field="id")
CHUNKS = Collection("knowledge_chunks", id_field="id")

_SENT_SPLIT = re.compile(r"(?<=[.!?…])\s+")
_TOKEN = re.compile(r"[а-яёa-z0-9]+", re.I)

STOP = {
    "и", "в", "во", "не", "что", "он", "на", "я", "с", "со", "как", "а", "то", "все", "она",
    "так", "его", "но", "да", "ты", "к", "у", "же", "вы", "за", "бы", "по", "только", "ее",
    "мне", "было", "вот", "от", "меня", "еще", "нет", "о", "из", "ему", "теперь", "когда",
    "даже", "ну", "вдруг", "ли", "если", "уже", "или", "ни", "быть", "был", "него", "до",
    "вас", "нибудь", "опять", "уж", "вам", "ведь", "там", "потом", "себя", "ничего", "ей",
    "может", "они", "тут", "где", "есть", "надо", "ней", "для", "мы", "тебя", "их", "чем",
    "была", "сам", "чтоб", "без", "будто", "чего", "раз", "тоже", "себе", "под", "будет",
    "ж", "тогда", "кто", "этот", "того", "потому", "этого", "какой", "совсем", "ним", "здесь",
    "этом", "один", "почти", "мой", "тем", "чтобы", "нее", "были", "куда", "зачем", "всех",
    "никогда", "можно", "при", "наконец", "два", "об", "другой", "хоть", "после", "над",
    "больше", "тот", "через", "эти", "нас", "про", "всего", "них", "какая", "много", "разве",
    "три", "эту", "моя", "впрочем", "хорошо", "свою", "этой", "перед", "иногда", "лучше",
    "чуть", "том", "нельзя", "такой", "им", "более", "всегда", "конечно", "всю", "между",
    "это", "the", "a", "of", "and", "to", "in", "for", "with", "on", "is", "are",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def tokenize(text: str) -> list[str]:
    return [t for t in (w.lower() for w in _TOKEN.findall(text)) if len(t) > 2 and t not in STOP]


def _stem(word: str) -> str:
    """Грубая нормализация русских окончаний — достаточно для совпадения «договор/договора»."""
    for suffix in ("ами", "ями", "ого", "его", "ому", "ему", "ыми", "ими", "ах", "ях",
                   "ов", "ев", "ий", "ый", "ая", "яя", "ое", "ее", "ые", "ие", "ой", "ей",
                   "ам", "ям", "ом", "ем", "а", "я", "ы", "и", "у", "ю", "е", "о"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[: -len(suffix)]
    return word


def terms(text: str) -> list[str]:
    return [_stem(t) for t in tokenize(text)]


# ---------------------------------------------------------------- источники и карточки
def add_source(kind: str, location: str, workspace_id: str = "ws_de") -> dict[str, Any]:
    source = {
        "id": f"src_{uuid.uuid4().hex[:8]}", "workspace_id": workspace_id, "kind": kind,
        "location": location, "status": "pending", "pages_total": 0, "pages_done": 0,
        "created_at": _now(), "updated_at": _now(),
    }
    return SOURCES.put(source)


def list_sources(workspace_id: str | None = None) -> list[dict]:
    items = SOURCES.all()
    return [s for s in items if workspace_id in (None, s.get("workspace_id"))]


def add_card(source_id: str | None, kind: str, title: str, body: str, *,
             facts: list[str] | None = None, source_url: str | None = None,
             workspace_id: str = "ws_de", confidence: float = 0.8) -> dict[str, Any]:
    card = {
        "id": f"card_{uuid.uuid4().hex[:8]}", "workspace_id": workspace_id, "source_id": source_id,
        "kind": kind, "title": title, "body": body, "facts": facts or [],
        "source_url": source_url, "confidence": confidence, "created_at": _now(),
    }
    return CARDS.put(card)


def list_cards(workspace_id: str | None = None, kind: str | None = None) -> list[dict]:
    items = CARDS.all()
    return [c for c in items
            if workspace_id in (None, c.get("workspace_id")) and kind in (None, c.get("kind"))]


def chunk_text(text: str, *, size_min: int = CHUNK_MIN, size_max: int = CHUNK_MAX) -> list[str]:
    """Нарезка по абзацам и границам предложений; чанк не длиннее ``size_max``."""
    paragraphs = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
    chunks: list[str] = []
    buf = ""
    for para in paragraphs:
        if len(buf) + len(para) + 2 <= size_max:
            buf = f"{buf}\n\n{para}".strip()
            continue
        if buf:
            chunks.append(buf)
        if len(para) <= size_max:
            buf = para
            continue
        # длинный абзац режем по предложениям
        buf = ""
        for sent in _SENT_SPLIT.split(para):
            if len(buf) + len(sent) + 1 <= size_max:
                buf = f"{buf} {sent}".strip()
            else:
                if buf:
                    chunks.append(buf)
                buf = sent[:size_max]
        # буфер длинного абзаца отдаём в общий поток
    if buf:
        chunks.append(buf)
    return [c for c in chunks if len(c) >= 40]


def index_source(source_id: str) -> dict[str, Any]:
    """Пересобрать чанки источника и посчитать эмбеддинги."""
    source = SOURCES.get(source_id)
    if source is None:
        raise KeyError(source_id)
    cards = [c for c in CARDS.all() if c.get("source_id") == source_id]
    if not cards:
        # источник без карточек: индексируем хотя бы его локацию как факт
        cards = [c for c in CARDS.all() if c.get("source_url") == source.get("location")]

    for old in [c for c in CHUNKS.all() if c.get("source_id") == source_id]:
        CHUNKS.delete(old["id"])

    texts: list[str] = []
    metas: list[dict] = []
    for card in cards:
        payload = f"{card['title']}\n\n{card['body']}"
        for i, piece in enumerate(chunk_text(payload)):
            texts.append(piece)
            metas.append({"card_id": card["id"], "workspace_id": card["workspace_id"],
                          "source_id": source_id, "kind": card["kind"],
                          "title": card["title"], "index": i})
    vectors = llm.embed(texts) if texts else []
    created = []
    for meta, text, vec in zip(metas, texts, vectors):
        chunk = {**meta, "id": f"ch_{uuid.uuid4().hex[:10]}", "content": text,
                 "embedding": vec, "tokens": len(tokenize(text)), "created_at": _now()}
        CHUNKS.put(chunk)
        created.append(chunk)

    source.update(status="ready", pages_total=len(cards), pages_done=len(cards),
                  updated_at=_now(), chunks=len(created))
    SOURCES.put(source)
    return {"source_id": source_id, "status": "ready", "cards": len(cards), "chunks": len(created)}


def reindex_all(workspace_id: str | None = None) -> dict[str, Any]:
    total = 0
    for src in list_sources(workspace_id):
        total += index_source(src["id"])["chunks"]
    return {"sources": len(list_sources(workspace_id)), "chunks": total}


# ---------------------------------------------------------------- поиск
def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def bm25(query_terms: list[str], docs_terms: list[list[str]], k1: float = 1.5,
         b: float = 0.75) -> list[float]:
    n = len(docs_terms)
    if not n:
        return []
    avgdl = sum(len(d) for d in docs_terms) / n or 1.0
    df = Counter()
    for d in docs_terms:
        for t in set(d):
            df[t] += 1
    scores = []
    for d in docs_terms:
        tf = Counter(d)
        score = 0.0
        for term in query_terms:
            if tf[term] == 0:
                continue
            idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
            denom = tf[term] + k1 * (1 - b + b * len(d) / avgdl)
            score += idf * tf[term] * (k1 + 1) / denom
        scores.append(score)
    return scores


def search(query: str, *, workspace_id: str | None = "ws_de", top_k: int = TOP_K,
           kinds: list[str] | None = None, use_vectors: bool = True) -> list[dict]:
    """Гибридный поиск: BM25 (лексика) + pgvector-совместимый косинус, объединение по RRF."""
    q_terms = terms(query)
    if not q_terms:
        return []
    pool = [c for c in CHUNKS.all()
            if workspace_id in (None, c.get("workspace_id")) and
            (kinds is None or c.get("kind") in kinds)]
    if not pool:
        return []

    lex = bm25(q_terms, [terms(c["content"]) for c in pool])
    vec_scores = [0.0] * len(pool)
    if use_vectors:
        q_vec = llm.embed([query])[0]
        vec_scores = [_cosine(q_vec, c.get("embedding") or []) for c in pool]

    # Reciprocal Rank Fusion: устойчиво к разной калибровке двух скореров
    k = 60.0
    lex_rank = sorted(range(len(pool)), key=lambda i: -lex[i])
    vec_rank = sorted(range(len(pool)), key=lambda i: -vec_scores[i])
    fused = [0.0] * len(pool)
    for rank, idx in enumerate(lex_rank):
        if lex[idx] > 0:
            fused[idx] += 1 / (k + rank + 1)
    for rank, idx in enumerate(vec_rank):
        if vec_scores[idx] > 0:
            fused[idx] += 1 / (k + rank + 1)

    order = sorted(range(len(pool)), key=lambda i: -fused[i])[:top_k]
    return [
        {
            "id": pool[i]["id"], "content": pool[i]["content"], "title": pool[i]["title"],
            "kind": pool[i]["kind"], "card_id": pool[i]["card_id"],
            "score": round(fused[i], 5), "lex": round(lex[i], 3),
            "vec": round(vec_scores[i], 3),
        }
        for i in order
        if fused[i] > 0
    ]


def context_block(query: str, *, workspace_id: str | None = "ws_de", top_k: int = TOP_K,
                  max_chars: int = 2000) -> dict[str, Any]:
    """Готовый контекст для промпта + список цитат (card_id) для проверяемости."""
    hits = search(query, workspace_id=workspace_id, top_k=top_k)
    used, total = [], 0
    for hit in hits:
        if total + len(hit["content"]) > max_chars:
            break
        used.append(hit)
        total += len(hit["content"])
    text = "\n\n---\n\n".join(f"[{h['title']}]\n{h['content']}" for h in used)
    return {
        "text": text or "В Brand Brain нет релевантных карточек — не выдумывай факты.",
        "citations": [{"card_id": h["card_id"], "title": h["title"], "kind": h["kind"],
                       "score": h["score"]} for h in used],
        "hits": len(used),
    }


def stats(workspace_id: str | None = None) -> dict[str, Any]:
    srcs = list_sources(workspace_id)
    cards = list_cards(workspace_id)
    chunks = [c for c in CHUNKS.all() if workspace_id in (None, c.get("workspace_id"))]
    return {
        "sources": len(srcs), "ready": len([s for s in srcs if s.get("status") == "ready"]),
        "cards": len(cards), "chunks": len(chunks),
        "with_embeddings": len([c for c in chunks if c.get("embedding")]),
        "embed_dim": llm.EMBED_DIM,
        "last_indexed_at": max((s.get("updated_at", "") for s in srcs), default=None),
        "backends": {"lexical": "bm25", "vector": "cosine над 1024 (pgvector-ready)"},
    }


_SEED_CARDS = [
    ("product", "Что делает конструктор договоров",
     "Сервис проверяет договор аренды за 15 минут: подсвечивает опасные формулировки, "
     "предлагает безопасные редакции и собирает протокол разногласий.\n\n"
     "Тарифы: «Старт» — 990 ₽ за документ, «Бизнес» — 4 900 ₽ в месяц до 20 документов, "
     "«Агентство» — 14 900 ₽ в месяц без ограничений и с командным доступом."),
    ("usp", "Почему нам доверяют",
     "Каждая формулировка подтверждена судебной практикой: в карточке риска указано дело и "
     "итог. Юристы обновляют базу раз в две недели, изменения применяются к уже проверенным "
     "документам бесплатно."),
    ("audience", "Кому это нужно",
     "Малый бизнес и самозанятые без юриста в штате: аренда помещений, подряд, поставка, "
     "услуги. Типичный клиент — ИП с 1–15 сотрудниками и 3–10 договорами в месяц."),
    ("objection", "«Дорого» — что отвечаем",
     "Час юриста в Москве — 4 500–7 000 ₽, одна пропущенная формулировка о залоге стоит "
     "десятки тысяч. Подписка «Бизнес» окупается на втором документе."),
    ("case", "Кейс: вернули залог 240 000 ₽",
     "ИП из Казани арендовал помещение 80 м². В договоре был пункт о «нормальном износе» без "
     "перечня. Конструктор предложил закрытый перечень и акт приёма-передачи с фотофиксацией. "
     "При выезде арендодатель удержал 240 000 ₽; суд взыскал сумму полностью."),
    ("tone", "Тон голоса",
     "Спокойно и по делу, без юридического сленга и запугивания. Короткие абзацы, конкретика, "
     "никаких «гарантируем победу в суде». Обращение — на «вы»."),
    ("stoplist", "Чего не говорим",
     "Не обещаем результат суда, не сравниваем с конкурентами по названию, не используем "
     "слово «бесплатно» в рекламных текстах и не публикуем чужие персональные данные."),
]


def seed_demo(workspace_id: str = "ws_de") -> dict[str, Any]:
    """Демо-наполнение Brand Brain (идемпотентно: по одному источнику на воркспейс)."""
    if any(s.get("workspace_id") == workspace_id and s.get("location") == "seed://dogovor"
           for s in SOURCES.all()):
        return stats(workspace_id)
    source = add_source("manual", "seed://dogovor", workspace_id)
    for kind, title, body in _SEED_CARDS:
        add_card(source["id"], kind, title, body, workspace_id=workspace_id, confidence=0.9)
    index_source(source["id"])
    return stats(workspace_id)
