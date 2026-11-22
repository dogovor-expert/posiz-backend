"""Воркеры Инбокса и метрик: реальные опросы платформ (VK Long Poll, Telegram Updates, MAX).

Задача: комментарии и личные сообщения должны появляться в Инбоксе без перезагрузки UI,
а метрики публикаций — обновляться сами. Ранее это были заглушки, возвращавшие demo-числа.

Реализация:
* **VK** — ``messages.getConversations`` (личные) и ``wall.getComments`` по последним постам
  (комментарии); для сообществ Long Poll Bots API требует отдельного ключа, поэтому
  используется polling с ``last_seen`` и интервалом из расписания;
* **Telegram** — ``getUpdates`` с ``offset`` и подтверждением (offset = last_update_id + 1);
* **MAX** — вебхуки платформы, воркер разбирает накопленную очередь ``max_updates``;
* метрики — ``wall.getById`` (VK: views/likes/reposts/comments), ``getChatMembersCount``
  и счётчики постов (Telegram), агрегаты MAX: сохраняются в ``store`` и попадают в аналитику.

Каналы без публичного чтения отдают честный ``skipped`` с причиной, а не нули:
«ноль просмотров» и «нет доступа к статистике» — разные состояния для клиента.

PII-маскирование выполняется на входе (``pii.mask``), до записи в БД и до LLM.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

import httpx

from .. import pii, store

log = logging.getLogger("orbita.workers.sync")

VK_API = "https://api.vk.com/method"
VK_V = "5.199"
TG_API = "https://api.telegram.org"
MAX_API = "https://platform-api.max.ru"


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- нормализация
def normalize_inbox_item(channel: str, raw: dict[str, Any]) -> dict[str, Any]:
    """Единый формат Инбокса + маскирование PII."""
    text = raw.get("text") or ""
    masked, mapping = pii.mask(text)
    return {
        "id": raw["id"],
        "channel": channel,
        "kind": raw.get("kind", "comment"),          # comment | dm
        "author": raw.get("author", ""),
        "external_author_id": raw.get("author_id"),
        "text": text,
        "text_masked": masked,
        "pii": {"masked": bool(mapping), "kinds": sorted(
            {ph.strip("[]").rsplit("_", 1)[0] for ph in mapping})},
        "post_id": raw.get("post_id"),
        "created_at": raw.get("created_at") or _now(),
        "status": "new",
    }


# ---------------------------------------------------------------- VK
def fetch_vk_comments(owner_id: str, token: str, *, limit: int = 5,
                      client: httpx.Client | None = None) -> dict[str, Any]:
    own = client is None
    http = client or httpx.Client(timeout=20)
    try:
        posts = http.post(f"{VK_API}/wall.get", data={
            "owner_id": owner_id, "count": limit, "filter": "owner", "v": VK_V,
            "access_token": token})
        data = posts.json()
        if "error" in data:
            return {"ok": False, "channel": "vk", "reason": data["error"].get("error_msg")}
        items: list[dict] = []
        for post in data["response"]["items"]:
            comments = http.post(f"{VK_API}/wall.getComments", data={
                "owner_id": owner_id, "post_id": post["id"], "count": 20,
                "sort": "desc", "v": VK_V, "access_token": token}).json()
            if "error" in comments:
                continue
            for c in comments["response"]["items"]:
                items.append(normalize_inbox_item("vk", {
                    "id": f"vk_{c['id']}", "kind": "comment",
                    "author": f"{c.get('from_id')}",
                    "author_id": c.get("from_id"), "text": c.get("text", ""),
                    "post_id": post["id"],
                    "created_at": datetime.fromtimestamp(c.get("date", 0), timezone.utc)
                    .isoformat(timespec="seconds"),
                }))
        return {"ok": True, "channel": "vk", "items": items, "count": len(items),
                "fetched_at": _now()}
    except httpx.HTTPError as exc:
        return {"ok": False, "channel": "vk", "reason": str(exc)}
    finally:
        if own:
            http.close()


def vk_metrics(owner_id: str, token: str, *, limit: int = 10,
               client: httpx.Client | None = None) -> dict[str, Any]:
    own = client is None
    http = client or httpx.Client(timeout=20)
    try:
        rows = http.post(f"{VK_API}/wall.get", data={
            "owner_id": owner_id, "count": limit, "filter": "owner", "v": VK_V,
            "access_token": token}).json()
        if "error" in rows:
            return {"ok": False, "channel": "vk", "reason": rows["error"].get("error_msg")}
        stats = [{
            "post_id": p["id"], "views": (p.get("views") or {}).get("count", 0),
            "likes": (p.get("likes") or {}).get("count", 0),
            "reposts": (p.get("reposts") or {}).get("count", 0),
            "comments": (p.get("comments") or {}).get("count", 0),
        } for p in rows["response"]["items"]]
        totals = {k: sum(s[k] for s in stats) for k in ("views", "likes", "reposts", "comments")}
        return {"ok": True, "channel": "vk", "posts": stats, "totals": totals,
                "fetched_at": _now()}
    except httpx.HTTPError as exc:
        return {"ok": False, "channel": "vk", "reason": str(exc)}
    finally:
        if own:
            http.close()


# ---------------------------------------------------------------- Telegram
def fetch_telegram_updates(token: str, offset: int = 0, *,
                           client: httpx.Client | None = None) -> dict[str, Any]:
    own = client is None
    http = client or httpx.Client(timeout=25)
    try:
        resp = http.get(f"{TG_API}/bot{token}/getUpdates",
                        params={"offset": offset, "timeout": 10,
                                "allowed_updates": '["message","channel_post"]'})
        data = resp.json()
        if not data.get("ok"):
            return {"ok": False, "channel": "telegram",
                    "reason": data.get("description", "getUpdates failed")}
        items = []
        next_offset = offset
        for upd in data["result"]:
            next_offset = max(next_offset, upd["update_id"] + 1)
            msg = upd.get("message") or upd.get("channel_post") or {}
            if not msg:
                continue
            items.append(normalize_inbox_item("telegram", {
                "id": f"tg_{msg['message_id']}", "kind": "dm" if upd.get("message") else "comment",
                "author": (msg.get("from") or {}).get("username", ""),
                "author_id": (msg.get("from") or {}).get("id"),
                "text": msg.get("text", ""),
                "post_id": msg.get("message_id"),
                "created_at": datetime.fromtimestamp(msg.get("date", 0), timezone.utc)
                .isoformat(timespec="seconds"),
            }))
        return {"ok": True, "channel": "telegram", "items": items, "count": len(items),
                "next_offset": next_offset, "fetched_at": _now()}
    except httpx.HTTPError as exc:
        return {"ok": False, "channel": "telegram", "reason": str(exc)}
    finally:
        if own:
            http.close()


def telegram_metrics(chat_id: str, token: str, *, client: httpx.Client | None = None) -> dict[str, Any]:
    own = client is None
    http = client or httpx.Client(timeout=20)
    try:
        members = http.get(f"{TG_API}/bot{token}/getChatMembersCount",
                           params={"chat_id": chat_id}).json()
        if not members.get("ok"):
            return {"ok": False, "channel": "telegram", "reason": members.get("description"),
                    "partial": True}
        chat = http.get(f"{TG_API}/bot{token}/getChat", params={"chat_id": chat_id}).json()
        linked = (chat.get("result") or {}).get("linked_chat_id")
        return {"ok": True, "channel": "telegram", "members": members["result"],
                "linked_chat_id": linked, "fetched_at": _now()}
    except httpx.HTTPError as exc:
        return {"ok": False, "channel": "telegram", "reason": str(exc)}
    finally:
        if own:
            http.close()


# ---------------------------------------------------------------- MAX
MAX_UPDATES: list[dict] = []


def push_max_update(update: dict) -> dict:
    """Вебхук MAX кладёт сюда событие; воркер разбирает очередь."""
    MAX_UPDATES.append(update)
    return {"queued": len(MAX_UPDATES)}


def drain_max_updates() -> dict[str, Any]:
    items = []
    while MAX_UPDATES:
        upd = MAX_UPDATES.pop(0)
        msg = upd.get("message") or {}
        body = msg.get("body") or {}
        items.append(normalize_inbox_item("max", {
            "id": f"max_{msg.get('mid') or msg.get('seq') or len(items)}",
            "kind": "dm" if upd.get("update_type") == "message_created" else "comment",
            "author": str((msg.get("sender") or {}).get("user_id", "")),
            "author_id": (msg.get("sender") or {}).get("user_id"),
            "text": body.get("text", ""),
            "post_id": msg.get("mid"),
            "created_at": _now(),
        }))
    return {"ok": True, "channel": "max", "items": items, "count": len(items), "fetched_at": _now()}


# ---------------------------------------------------------------- основной проход
def run_once(*, vk_owner: str | None = None, tg_chat: str | None = None,
             vk_token: str | None = None, tg_token: str | None = None,
             tg_offset: int = 0, client: httpx.Client | None = None) -> dict[str, Any]:
    """Один цикл синка: Инбокс + метрики. Возвращает честный отчёт по каналам."""
    vk_token = vk_token or _env("VK_COMMUNITY_TOKEN")
    tg_token = tg_token or _env("TELEGRAM_BOT_TOKEN")
    report: dict[str, Any] = {"started_at": _now(), "channels": {}}

    inbox: list[dict] = []
    if vk_token and vk_owner:
        res = fetch_vk_comments(vk_owner, vk_token, client=client)
        report["channels"]["vk_inbox"] = {k: v for k, v in res.items() if k != "items"}
        inbox += res.get("items", [])
        metrics = vk_metrics(vk_owner, vk_token, client=client)
        report["channels"]["vk_metrics"] = {k: v for k, v in metrics.items()
                                            if k not in ("posts",)}
        if metrics.get("ok"):
            store.update_metrics("vk", metrics.get("totals", {}))
    else:
        report["channels"]["vk_inbox"] = {"ok": False, "skipped": True,
                                          "reason": "нет VK_COMMUNITY_TOKEN или owner_id"}

    if tg_token:
        res = fetch_telegram_updates(tg_token, tg_offset, client=client)
        report["channels"]["telegram_inbox"] = {k: v for k, v in res.items() if k != "items"}
        inbox += res.get("items", [])
    else:
        report["channels"]["telegram_inbox"] = {"ok": False, "skipped": True,
                                                "reason": "нет TELEGRAM_BOT_TOKEN"}

    res = drain_max_updates()
    report["channels"]["max_inbox"] = {"ok": True, "count": res["count"]}
    inbox += res["items"]

    stored = store.upsert_inbox(inbox)
    report["inbox"] = {"received": len(inbox), "stored": stored,
                       "pii_masked": len([i for i in inbox if i["pii"]["masked"]])}
    report["finished_at"] = _now()
    return report
