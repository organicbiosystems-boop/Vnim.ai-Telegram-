"""Сбор новостей из RSS-лент."""
import asyncio
import calendar
import html
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

import feedparser

log = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


@dataclass
class Item:
    guid: str
    url: str
    title: str
    source: str
    summary: str
    published_ts: float | None


def clean_html(text: str) -> str:
    text = _TAG_RE.sub(" ", text or "")
    return _WS_RE.sub(" ", html.unescape(text)).strip()


def load_feeds(path: str) -> list[str]:
    p = Path(path)
    if not p.exists():
        log.warning("Файл с лентами %s не найден", path)
        return []
    return [
        line.strip()
        for line in p.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _parse_feed(url: str) -> list[Item]:
    feed = feedparser.parse(url, agent="VnimAiNewsBot/1.0")
    if feed.bozo and not feed.entries:
        log.warning("Не удалось прочитать ленту %s: %s", url, feed.get("bozo_exception"))
        return []
    source = clean_html(feed.feed.get("title", "")) or url
    items = []
    for e in feed.entries:
        link = e.get("link", "")
        guid = e.get("id") or link or e.get("title", "")
        if not guid:
            continue
        ts = None
        for key in ("published_parsed", "updated_parsed"):
            if e.get(key):
                ts = calendar.timegm(e[key])
                break
        summary = e.get("summary", "")
        if e.get("content"):
            summary = e["content"][0].get("value", summary)
        items.append(Item(
            guid=guid,
            url=link,
            title=clean_html(e.get("title", "")),
            source=source,
            summary=clean_html(summary)[:4000],
            published_ts=ts,
        ))
    return items


async def fetch_all(feed_urls: list[str], max_age_hours: int) -> list[Item]:
    results = await asyncio.gather(
        *(asyncio.to_thread(_parse_feed, u) for u in feed_urls), return_exceptions=True
    )
    cutoff = time.time() - max_age_hours * 3600
    items: list[Item] = []
    for url, res in zip(feed_urls, results):
        if isinstance(res, Exception):
            log.warning("Ошибка ленты %s: %s", url, res)
            continue
        for it in res:
            if it.published_ts is not None and it.published_ts < cutoff:
                continue
            items.append(it)
    # Новые сначала
    items.sort(key=lambda i: i.published_ts or 0, reverse=True)
    return items
