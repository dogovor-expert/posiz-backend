"""Link-in-Bio: микро-лендинг по адресу posiz.ru/@<slug>.

Зачем: в X ссылка в теле поста стоит $0,20 вместо $0,015 (в 13 раз дороже), в Instagram и TikTok
ссылки в тексте не кликабельны. Одна ссылка в шапке профиля → страница, где сами обновляются
последние посты, лид-магниты и кнопки мессенджеров.
"""
from __future__ import annotations

import html
from datetime import datetime, timezone

from .docs import Collection

PAGES: dict[str, dict] = {}          # кэш процесса; источник правды — PostgreSQL при DATA_LAYER=db

BLOCK_KINDS = ("link", "post", "magnet", "messenger", "subscribe")

_SEED = {
    "dogovor": {
        "slug": "dogovor",
        "title": "Договор Эксперт",
        "bio": "Конструктор договоров для малого бизнеса. Проверьте свой договор за 15 минут.",
        "theme": "graphite",
        "workspace_id": "ws_de",
        "blocks": [
            {"id": "b1", "kind": "magnet", "label": "Чек-лист: 12 пунктов договора аренды",
             "url": "https://dogovor.expert/checklist", "badge": "PDF"},
            {"id": "b2", "kind": "post", "label": "Как защитить залог при аренде",
             "url": "https://dogovor.expert/blog/zalog", "channel": "max", "when": "вчера"},
            {"id": "b3", "kind": "messenger", "channel": "telegram", "label": "Задать вопрос юристу",
             "url": "https://t.me/dogovor_expert_bot"},
            {"id": "b4", "kind": "subscribe", "label": "Дайджест раз в неделю",
             "url": "https://dogovor.expert/subscribe"},
        ],
        "created_at": datetime.now(timezone.utc),
        "views": 4128,
        "clicks": 917,
    }
}
PAGES.update(_SEED)

# Персистентность: коллекция называется так же, как целевая таблица bio_pages.
DOCS = Collection("bio_pages", seed=list(_SEED.values()), id_field="slug")


def get(slug: str) -> dict | None:
    page = DOCS.get(slug)
    if page is not None:
        PAGES[slug] = page
    return page


def list_pages() -> list[dict]:
    return DOCS.all()


def create(slug: str, title: str, bio: str, workspace_id: str = "ws_de") -> dict:
    page = {"slug": slug, "title": title, "bio": bio, "theme": "graphite",
            "workspace_id": workspace_id, "blocks": [], "created_at": datetime.now(timezone.utc),
            "views": 0, "clicks": 0}
    PAGES[slug] = page
    return DOCS.put(page)


def add_block(slug: str, kind: str, label: str, url: str, **extra) -> dict:
    page = get(slug)
    if page is None:
        raise KeyError(slug)
    block = {"id": f"b{len(page['blocks']) + 1}", "kind": kind, "label": label, "url": url, **extra}
    page["blocks"].append(block)
    DOCS.put(page)
    return block


def sync_posts(slug: str, posts: list[dict]) -> dict:
    """Автообновление карточек: последние публикации подтягиваются без ручной правки страницы."""
    page = get(slug)
    if page is None:
        raise KeyError(slug)
    page["blocks"] = [b for b in page["blocks"] if b["kind"] != "post"]
    for p in posts[:3]:
        page["blocks"].insert(0, {"id": f"p_{p['id']}", "kind": "post", "label": p["title"],
                                  "url": f"https://dogovor.expert/blog/{p['id']}",
                                  "channel": p.get("channel", "max"), "when": "авто"})
    return DOCS.put(page)


def render_html(page: dict) -> str:
    """Публичная страница: один самодостаточный HTML, без внешних ресурсов."""
    e = html.escape
    rows = []
    for b in page["blocks"]:
        tag = {"post": "пост", "magnet": "лид-магнит", "messenger": "мессенджер",
               "subscribe": "подписка", "link": "ссылка"}.get(b["kind"], b["kind"])
        badge = f'<span class="badge">{e(b.get("badge") or tag)}</span>'
        rows.append(
            f'<a class="row" href="{e(b["url"])}"><span class="lbl">{e(b["label"])}</span>{badge}</a>'
        )
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(page["title"])}</title>
<style>
body{{margin:0;background:#0a0a0c;color:#f5f5f7;font:16px/1.5 -apple-system,Segoe UI,Roboto,sans-serif}}
.wrap{{max-width:520px;margin:0 auto;padding:40px 18px 60px}}
h1{{font-size:22px;margin:14px 0 6px}}p{{color:#a9a9b3;margin:0 0 22px;font-size:14px}}
.row{{display:flex;align-items:center;gap:10px;background:#15151b;border:1px solid #26262f;border-radius:12px;
padding:14px 16px;margin-bottom:10px;color:#f5f5f7;text-decoration:none}}
.row:hover{{border-color:#7c5cff}}.lbl{{flex:1;font-size:14px}}
.badge{{font-size:11px;color:#c9bcff;background:#241d3f;border:1px solid #3a2f66;border-radius:6px;padding:2px 6px}}
.foot{{color:#6d6d78;font-size:12px;margin-top:22px;text-align:center}}
.avatar{{width:56px;height:56px;border-radius:16px;background:linear-gradient(150deg,#2b1f5e,#7c5cff);
display:grid;place-items:center;font-weight:700}}
</style></head><body><div class="wrap">
<div class="avatar">{e(page["title"][:1])}</div>
<h1>{e(page["title"])}</h1><p>{e(page["bio"])}</p>
{''.join(rows)}
<div class="foot">posiz.ru/@{e(page["slug"])} · страница обновляется автоматически</div>
</div></body></html>"""


def track(slug: str, kind: str = "view") -> dict:
    page = get(slug)
    if page is None:
        raise KeyError(slug)
    page["views" if kind == "view" else "clicks"] += 1
    DOCS.put(page)
    return {"slug": slug, kind: page["views" if kind == "view" else "clicks"]}
