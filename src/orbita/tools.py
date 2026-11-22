"""Инструменты контента: нарезка лонгрида в тред и варианты заголовков для A/B.

Нарезка детерминированная (без LLM): сначала предложения, потом абзацы, потом жёсткий рез.
Хук всегда в первом посте, CTA — в последнем; нумерация «1/6» вычитается из лимита канала.
"""
from __future__ import annotations

import re

LIMITS = {"x": 280, "threads": 500, "telegram": 4096, "max": 4000, "vk": 4000, "bluesky": 300}


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?…])\s+", text.strip())
    return [p.strip() for p in parts if p.strip()]


def split_thread(text: str, channel: str = "x", limit: int | None = None,
                 hook: str | None = None, cta: str | None = None,
                 with_numbering: bool = True) -> dict:
    limit = limit or LIMITS.get(channel, 280)
    sentences = _sentences(text)
    if not sentences:
        return {"channel": channel, "limit": limit, "count": 0, "parts": [], "fits": False}

    # оцениваем число частей по грубому объёму, чтобы зарезервировать место под «n/N »
    approx = max(1, round(len(text) / max(40, limit - 12)))
    prefix = lambda i: f"{i}/{approx} " if with_numbering else ""
    budget = limit - (len(prefix(approx)) if with_numbering else 0)

    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        piece = sentence if len(sentence) <= budget else sentence[: budget - 1].rstrip() + "…"
        candidate = f"{current} {piece}".strip() if current else piece
        if len(candidate) <= budget:
            current = candidate
        else:
            chunks.append(current)
            current = piece
    if current:
        chunks.append(current)

    if hook:
        first = chunks[0]
        room = budget - len(hook) - 1
        chunks[0] = (hook + "\n" + first[:max(0, room)].rstrip()) if room > 20 else hook
    if cta:
        last = chunks[-1]
        if len(last) + len(cta) + 2 <= budget:
            chunks[-1] = f"{last}\n\n{cta}"
        else:
            chunks.append(cta)

    total = len(chunks)
    parts = [{"index": i + 1,
              "text": (f"{i + 1}/{total} " if with_numbering else "") + chunk,
              "chars": len(chunk) + (len(f"{i + 1}/{total} ") if with_numbering else 0)}
             for i, chunk in enumerate(chunks)]
    return {
        "channel": channel, "limit": limit, "count": total, "fits": all(p["chars"] <= limit for p in parts),
        "hook": hook or "", "cta": cta or "", "parts": parts,
    }


def headline_variants(title: str) -> list[dict]:
    """Два варианта заголовка для A/B: цифровой/выгодный и вопросительный/интригующий."""
    base = title.strip().rstrip(".?!")
    number = re.search(r"\d+", base)
    variant_a = base if number else f"5 пунктов: {base[0].lower() + base[1:]}" if base else base
    variant_b = f"{base} — что проверить первым?" if not base.endswith("?") else base
    return [{"key": "a", "headline": variant_a[:120], "style": "конкретика"},
            {"key": "b", "headline": variant_b[:120], "style": "вопрос"}]
