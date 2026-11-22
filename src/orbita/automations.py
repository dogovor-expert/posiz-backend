"""Комментарий → личка (comment-to-DM).

Самый конверсионный сценарий 2025–2026: в посте просим написать кодовое слово,
воркер инбокса ловит комментарий и отправляет лид-магнит в личку с UTM-меткой.
"""
from __future__ import annotations

from datetime import datetime, timezone

from .docs import Collection

RULES: dict[str, dict] = {
    "au_1": {
        "id": "au_1", "keyword": "ДОГОВОР", "channels": ["vk", "telegram", "max"],
        "magnet": "Чек-лист «12 пунктов договора аренды»",
        "link_campaign": "comment-dogovor", "active": True, "sends": 128, "conversion": 0.34,
        "dm_template": ("Привет! Вы написали «{keyword}» — вот {magnet}: {link}\n"
                        "Если нужно проверить свой договор, ответьте на это сообщение."),
    },
    "au_2": {
        "id": "au_2", "keyword": "ЗАЛОГ", "channels": ["vk", "telegram"],
        "magnet": "Памятка «Как вернуть залог»",
        "link_campaign": "comment-zalog", "active": True, "sends": 61, "conversion": 0.28,
        "dm_template": "Привет! Держите {magnet}: {link}",
    },
}
EVENTS: list[dict] = []

RULES_DOCS = Collection("automation_rules", seed=list(RULES.values()), id_field="id")
EVENTS_DOCS = Collection("automation_events", id_field="id")


def list_rules() -> list[dict]:
    return RULES_DOCS.all()


def upsert_rule(payload: dict) -> dict:
    rid = payload.get("id") or f"au_{len(RULES) + 1}"
    rule = {"id": rid, "keyword": payload["keyword"].upper(), "channels": payload.get("channels", ["vk"]),
            "magnet": payload.get("magnet", ""), "link_campaign": payload.get("link_campaign", "comment"),
            "active": payload.get("active", True), "sends": 0, "conversion": 0.0,
            "dm_template": payload.get("dm_template") or
            "Вы написали «{keyword}» — вот {magnet}: {link}"}
    RULES[rid] = {**RULES.get(rid, {}), **rule}
    return RULES_DOCS.put(RULES[rid])


def _build_link(channel: str, campaign: str) -> str:
    from . import links

    return links.build("https://dogovor.expert/checklist", channel, campaign)["short_url"]


def handle_comment(channel: str, author: str, text: str) -> list[dict]:
    """Воркер инбокса передаёт сюда каждый новый комментарий. Возвращает отправленные DM."""
    sent = []
    upper = text.upper()
    for rule in RULES_DOCS.all():
        if not rule["active"] or rule["keyword"] not in upper:
            continue
        if channel not in rule["channels"]:
            continue
        link = _build_link(channel, rule["link_campaign"])
        message = rule["dm_template"].format(keyword=rule["keyword"], magnet=rule["magnet"], link=link)
        event = {
            "id": f"ev_{len(EVENTS) + 1}", "rule_id": rule["id"], "channel": channel,
            "author": author, "comment": text, "dm": message, "link": link,
            "status": "sent", "ts": datetime.now(timezone.utc),
        }
        EVENTS.append(event)
        EVENTS_DOCS.put(event)
        rule["sends"] += 1
        RULES_DOCS.put(rule)
        sent.append(event)
    return sent


def list_events(limit: int = 50) -> list[dict]:
    events = EVENTS_DOCS.all() or EVENTS
    return list(reversed(events))[:limit]
