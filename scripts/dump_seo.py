"""Экспорт SEO-каталога в JSON для маркетингового сайта `posiz.ru` (Next.js).

Сайт не переписывает карту URL руками: `seo.py` остаётся одной точкой правды,
а этот скрипт выгружает из неё данные для сборки (`50-SITE/src/data/seo.json`).

Запуск::

    PYTHONPATH=src python scripts/dump_seo.py            # записать в 50-SITE
    PYTHONPATH=src python scripts/dump_seo.py --check    # только проверить актуальность
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TARGET = REPO / "50-SITE" / "src" / "data" / "seo.json"


def payload() -> dict:
    from orbita import catalog, publictools, seo

    return {
        "generated": date.today().isoformat(),
        "site": {"name": seo.SITE_NAME, "url": seo.SITE, "app": seo.APP, "api": seo.API,
                 "legal": seo.LEGAL_NAME},
        "inventory": seo.inventory(),
        "plans": [{"code": code, "name": name, "price": price, "hint": hint}
                  for code, name, price, hint in seo.PLANS],
        "channels": [
            {"kind": item.kind, "label": item.label, "group": item.group,
             "api_mode": item.api_mode, "priority": item.priority,
             "publish": item.publish, "read": item.read, "media": list(item.media),
             "limit": item.limit, "connect": item.connect, "risk": item.risk,
             "note": item.note, "tags": list(item.tags),
             "url": f"{seo.SITE}/integrations/{item.kind}"}
            for item in catalog.CATALOG
        ],
        "sync": [{"source": source, "target": target,
                  "url": f"{seo.SITE}/sync/{source}-to-{target}"}
                 for source in seo.SYNC_SOURCES for target in seo.SYNC_TARGETS
                 if source != target],
        "solutions": [{"slug": slug, "title": title, "url": f"{seo.SITE}/solutions/{slug}"}
                      for slug, title in seo.NICHES],
        "tools": seo.tool_meta(),
        "delivery_rub": publictools.DELIVERY_RUB,
        "channel_risk": {kind: {"status": status, "factor": factor,
                                "note": note}
                         for kind, (status, factor, note) in publictools.CHANNEL_RISK.items()},
        "blog": [{"category": category, "title": title,
                  "articles": [{"slug": slug, "headline": headline,
                                "url": f"{seo.SITE}/blog/{category}/{slug}"}
                               for slug, headline in articles]}
                 for category, (title, articles) in seo.BLOG.items()],
        "robots": seo.robots_txt(),
        "urls": seo.sitemap_entries(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="не писать файл, только сравнить")
    args = parser.parse_args()

    data = payload()
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"

    if args.check:
        if not TARGET.exists() or TARGET.read_text(encoding="utf-8") != text:
            print(f"{TARGET} устарел — запустите без --check", file=sys.stderr)
            return 1
        print(f"{TARGET}: актуален ({len(data['urls'])} URL)")
        return 0

    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(text, encoding="utf-8")
    print(f"{TARGET}: {len(data['urls'])} URL, "
          f"{len(data['channels'])} каналов, {len(data['sync'])} связок, "
          f"{len(data['solutions'])} решений")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
