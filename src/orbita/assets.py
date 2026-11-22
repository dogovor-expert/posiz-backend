"""Медиа-банк (DAM): папки бренда, шаблоны, лицензии и сток-интеграция.

Пользователь не загружает логотип с компьютера каждый раз: он выбирает ассет из папки бренда,
а рендерер подставляет его в карточку.
"""
from __future__ import annotations

from datetime import datetime, timezone

FOLDERS = ["Логотипы", "Подложки", "Шаблоны карточек", "Иконки", "Фото (сток)", "Документы"]

ASSETS: list[dict] = [
    {"id": "as_1", "name": "logo-dogovor.svg", "folder": "Логотипы", "kind": "svg", "size_mb": 0.02,
     "tags": ["лого", "светлый фон"], "license": "own", "url": "/media/logo-dogovor.svg"},
    {"id": "as_2", "name": "logo-inverse.svg", "folder": "Логотипы", "kind": "svg", "size_mb": 0.02,
     "tags": ["лого", "тёмный фон"], "license": "own", "url": "/media/logo-inverse.svg"},
    {"id": "as_3", "name": "card-check-list.800x1200.svg", "folder": "Шаблоны карточек", "kind": "svg",
     "size_mb": 0.05, "tags": ["чек-лист", "вертикаль"], "license": "own", "url": "/media/card-1.svg"},
    {"id": "as_4", "name": "card-quote.1080x1080.svg", "folder": "Шаблоны карточек", "kind": "svg",
     "size_mb": 0.04, "tags": ["цитата", "квадрат"], "license": "own", "url": "/media/card-2.svg"},
    {"id": "as_5", "name": "paper-texture.jpg", "folder": "Подложки", "kind": "jpg", "size_mb": 1.2,
     "tags": ["текстура", "бумага"], "license": "stock", "url": "/media/paper.jpg"},
    {"id": "as_6", "name": "handshake-office.jpg", "folder": "Фото (сток)", "kind": "jpg", "size_mb": 2.4,
     "tags": ["сделка", "офис"], "license": "stock:unsplash", "url": "/media/handshake.jpg"},
    {"id": "as_7", "name": "dogovor-checklist.pdf", "folder": "Документы", "kind": "pdf", "size_mb": 0.8,
     "tags": ["лид-магнит"], "license": "own", "url": "/media/checklist.pdf"},
]
STORAGE_LIMIT_GB = 10.0


def list_assets(folder: str | None = None, tag: str | None = None) -> list[dict]:
    items = ASSETS
    if folder:
        items = [a for a in items if a["folder"] == folder]
    if tag:
        items = [a for a in items if tag in a["tags"]]
    return items


def add_asset(name: str, folder: str, kind: str = "jpg", size_mb: float = 0.5,
              license: str = "own", tags: list[str] | None = None) -> dict:
    asset = {"id": f"as_{len(ASSETS) + 1}", "name": name, "folder": folder, "kind": kind,
             "size_mb": size_mb, "tags": tags or [], "license": license,
             "url": f"/media/{name}", "added_at": datetime.now(timezone.utc)}
    ASSETS.append(asset)
    return asset


def storage_usage() -> dict:
    used = round(sum(a["size_mb"] for a in ASSETS) / 1024, 2)
    return {"used_gb": used, "limit_gb": STORAGE_LIMIT_GB,
            "percent": round(used / STORAGE_LIMIT_GB * 100, 1), "files": len(ASSETS)}


def folders() -> list[dict]:
    return [{"name": f, "count": sum(1 for a in ASSETS if a["folder"] == f)} for f in FOLDERS]
