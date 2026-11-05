"""Crypto news from public RSS feeds, matched to coins. Read-only, no keys.

Headlines are third-party text: they are shown as-is and never executed or fed into commands.
"""
from __future__ import annotations

import re
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

FEEDS = {
    "CoinDesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "Cointelegraph": "https://cointelegraph.com/rss",
    "The Block": "https://www.theblock.co/rss.xml",
    "Decrypt": "https://decrypt.co/feed",
}
# Names that are also everyday words need the ticker or a fuller name to count as a match.
ALIASES = {
    "BTC": ["Bitcoin"], "ETH": ["Ethereum", "Ether"], "HBAR": ["Hedera"], "QNT": ["Quant Network", "Quant (QNT)"],
    "RENDER": ["Render Network", "RNDR"], "ARB": ["Arbitrum"], "LCX": ["LCX"], "DOT": ["Polkadot"], "SOL": ["Solana"],
}


@dataclass(frozen=True)
class Article:
    source: str
    title: str
    link: str
    published: datetime
    summary: str


def _text(el: ET.Element | None) -> str:
    return re.sub(r"<[^>]+>", "", (el.text or "") if el is not None else "").strip()


def fetch_feed(source: str, url: str) -> list[Article]:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 cbagent/0.1"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        root = ET.fromstring(resp.read())
    out = []
    for item in root.iter("item"):
        try:
            published = parsedate_to_datetime(_text(item.find("pubDate")))
        except (TypeError, ValueError):
            continue
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        link = _text(item.find("link")).split("?utm_")[0]
        out.append(Article(source, _text(item.find("title")), link, published,
                           _text(item.find("description"))[:400]))
    return out


def fetch_all(days: int = 3) -> tuple[list[Article], list[str]]:
    """All articles from the last `days` days, newest first, plus the names of feeds that failed."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    failed: list[str] = []

    def one(item):
        try:
            return fetch_feed(*item)
        except Exception:
            failed.append(item[0])
            return []

    with ThreadPoolExecutor(max_workers=len(FEEDS)) as pool:
        articles = [a for batch in pool.map(one, FEEDS.items()) for a in batch if a.published >= cutoff]
    seen, unique = set(), []
    for a in sorted(articles, key=lambda a: a.published, reverse=True):
        if a.title.lower() not in seen:
            seen.add(a.title.lower())
            unique.append(a)
    return unique, failed


def _patterns(coin: str) -> list[re.Pattern]:
    """Tickers match case-sensitively (ARB, not 'arb'); names match in any case (Bitcoin, bitcoin)."""
    edge = r"(?<![A-Za-z0-9$])\$?({})(?![A-Za-z0-9])"
    pats = [re.compile(edge.format(re.escape(coin)))]
    if ALIASES.get(coin):
        pats.append(re.compile(edge.format("|".join(re.escape(t) for t in ALIASES[coin])), re.IGNORECASE))
    return pats


def match(articles: list[Article], coins: list[str]) -> dict[str, list[Article]]:
    out = {}
    for coin in coins:
        pats = _patterns(coin.upper())
        out[coin] = [a for a in articles if any(p.search(a.title) or p.search(a.summary) for p in pats)]
    return out


def report(articles: list[Article], failed: list[str], coins: list[str], per_coin: int = 5, market: int = 10) -> str:
    lines = [f"{len(articles)} articles from {', '.join(s for s in FEEDS if s not in failed)}"
             + (f" (unreachable: {', '.join(failed)})" if failed else ""), ""]
    for coin, items in match(articles, coins).items():
        lines.append(f"{coin}: {len(items)} mention{'s' if len(items) != 1 else ''}")
        for a in items[:per_coin]:
            lines.append(f"  {a.published:%b %d %H:%M} {a.source:<13} {a.title}")
            lines.append(f"  {'':<26}{a.link}")
    lines += ["", "Latest market headlines"]
    for a in articles[:market]:
        lines.append(f"  {a.published:%b %d %H:%M} {a.source:<13} {a.title}")
    return "\n".join(lines)
