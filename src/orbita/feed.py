"""RSS-лента для Дзена: реальный XML с ``<yandex:full-text>``.

Дзен не даёт API публикации: он забирает RSS и индексирует его. Требования Дзена к ленте:
* канал с ``<link>``, ``<title>``, ``<description>``, ``<language>ru`` и ``<image>``;
* у каждого ``<item>`` уникальный ``<guid>`` и дата ``<pubDate>`` в RFC-822;
* **полный текст** элемента — в ``<yandex:full-text>`` (обёртка CDATA с HTML),
  а ``<description>`` — короткий анонс;
* размер файла до 10 МБ, до 50 последних публикаций.

Здесь — чистый генератор без зависимостей: результат детерминирован, легко тестируется
и отдаётся по ``GET /feed/{workspace}.xml`` (см. api_v3).
"""
from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from email.utils import format_datetime

MAX_ITEMS = 50
NS = ('xmlns:yandex="http://news.yandex.ru" '
      'xmlns:media="http://search.yahoo.com/mrss/" '
      'xmlns:atom="http://www.w3.org/2005/Atom"')


def _cdata(text: str) -> str:
    """CDATA с защитой от закрывающего тега внутри текста."""
    return "<![CDATA[" + text.replace("]]>", "]]]]><![CDATA[>") + "]]>"


def _rfc822(value: datetime | str | None) -> str:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            value = datetime.now(timezone.utc)
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return format_datetime(value)


def _plain_to_html(text: str) -> str:
    """Абзацы → <p>, «Подробнее: url» → ссылка. Никакой внешней разметки не сохраняем."""
    blocks = [b.strip() for b in re.split(r"\n{2,}", text) if b.strip()]
    out = []
    for block in blocks:
        link = re.fullmatch(r"Подробнее:\s*(\S+)", block)
        if link:
            url = html.escape(link.group(1))
            out.append(f'<p><a href="{url}">{url}</a></p>')
        else:
            out.append(f"<p>{html.escape(block)}</p>")
    return "".join(out)


def build(title: str, link: str, description: str, items: list[dict], *,
          language: str = "ru", logo: str | None = None,
          last_build: datetime | None = None) -> str:
    """Собрать валидный RSS 2.0 с расширением Яндекса.

    ``items``: ``{title, body, url, guid?, published_at?, image?}``.
    """
    now = format_datetime(last_build or datetime.now(timezone.utc))
    rows = []
    for item in items[:MAX_ITEMS]:
        body_html = _plain_to_html(item.get("body") or item.get("text") or "")
        summary = re.sub(r"<[^>]+>", " ", body_html)
        summary = re.sub(r"\s+", " ", summary).strip()[:200]
        guid = item.get("guid") or item.get("url") or item.get("id") or summary[:32]
        enclosure = ""
        if item.get("image"):
            enclosure = (f'<enclosure url="{html.escape(item["image"])}" '
                         'type="image/jpeg" />')
        rows.append(
            "    <item>\n"
            f"      <title>{html.escape(item.get('title', ''))}</title>\n"
            f"      <link>{html.escape(item.get('url', link))}</link>\n"
            f"      <guid isPermaLink=\"false\">{html.escape(str(guid))}</guid>\n"
            f"      <pubDate>{_rfc822(item.get('published_at'))}</pubDate>\n"
            f"      <description>{_cdata(summary)}</description>\n"
            f"      {enclosure}\n"
            f"      <yandex:full-text>{_cdata(body_html)}</yandex:full-text>\n"
            "    </item>"
        )
    image = (f"    <image><url>{html.escape(logo)}</url><title>{html.escape(title)}</title>"
             f"<link>{html.escape(link)}</link></image>\n") if logo else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<rss version="2.0" {NS}>\n'
        "  <channel>\n"
        f"    <title>{html.escape(title)}</title>\n"
        f"    <link>{html.escape(link)}</link>\n"
        f"    <description>{html.escape(description)}</description>\n"
        f"    <language>{language}</language>\n"
        f"    <lastBuildDate>{now}</lastBuildDate>\n"
        f"{image}"
        + "\n".join(rows) + "\n"
        "  </channel>\n</rss>\n"
    )


def validate(xml: str) -> dict:
    """Мини-валидатор требований Дзена (без внешних парсеров)."""
    problems = []
    if not xml.startswith("<?xml"):
        problems.append("нет XML-пролога")
    if "<rss version=\"2.0\"" not in xml:
        problems.append("нет корня rss version=2.0")
    for tag in ("<title>", "<link>", "<description>", "<language>"):
        if tag not in xml:
            problems.append(f"в канале нет {tag}")
    items = xml.count("<item>")
    if items > MAX_ITEMS:
        problems.append(f"элементов больше {MAX_ITEMS}")
    if items and "<yandex:full-text>" not in xml:
        problems.append("нет yandex:full-text — Дзен обрежет текст")
    if items and "<guid" not in xml:
        problems.append("нет guid у элементов")
    if len(xml.encode()) > 10 * 1024 * 1024:
        problems.append("файл больше 10 МБ")
    return {"ok": not problems, "problems": problems, "items": items,
            "bytes": len(xml.encode())}
