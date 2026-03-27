"""
collect.py

Fetches articles from all Tier 1 and Tier 2 sources defined in config/sources.yaml.
Applies freshness filter (last 7 days) and deduplicates against the seen-articles cache.

Usage (standalone test):
    python scripts/collect.py [--verbose]
"""

import hashlib
import json
import os
import sys
import time
import argparse
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from typing import List, Optional

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup
from dateutil import parser as dateutil_parser

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(PROJECT_ROOT, "config", "sources.yaml")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


@dataclass
class Article:
    id: str                      # SHA256 of URL
    source_id: str
    source_name: str
    tier: int
    title: str
    url: str
    published_date: datetime
    body_snippet: str            # first ~500 chars of content
    full_text: str               # full article body (best effort)
    score_threshold: int
    is_podcast: bool = False
    transcript: str = ""
    raw_language: str = "en"

    def to_dict(self):
        d = asdict(self)
        d["published_date"] = self.published_date.isoformat()
        return d


def _article_id(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_date(date_str) -> Optional[datetime]:
    if not date_str:
        return None
    try:
        if hasattr(date_str, "tm_year"):
            # feedparser time struct
            import calendar
            ts = calendar.timegm(date_str)
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        dt = dateutil_parser.parse(str(date_str))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _fetch_url(url: str, retries: int = 1) -> Optional[requests.Response]:
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            if resp.status_code in (429, 503) and attempt < retries:
                time.sleep(2)
                continue
            resp.raise_for_status()
            return resp
        except Exception:
            if attempt < retries:
                time.sleep(2)
    return None


def _extract_article_text(url: str) -> str:
    """Fetch article page and extract main text content."""
    time.sleep(0.5)
    resp = _fetch_url(url)
    if not resp:
        return ""
    try:
        soup = BeautifulSoup(resp.text, "html.parser")
        # Remove nav, header, footer, scripts, ads
        for tag in soup(["nav", "header", "footer", "script", "style", "aside", "form"]):
            tag.decompose()
        # Try article tag first, then main, then body
        for selector in ["article", "main", "[role='main']", "body"]:
            container = soup.select_one(selector)
            if container:
                text = " ".join(container.get_text(" ", strip=True).split())
                if len(text) > 200:
                    return text[:3000]
        return ""
    except Exception:
        return ""


def _parse_rss(url: str) -> object:
    """Fetch RSS via requests (handles redirects), then parse with feedparser."""
    resp = _fetch_url(url)
    if resp and resp.content:
        return feedparser.parse(resp.content)
    # Fallback: let feedparser try directly
    return feedparser.parse(url, request_headers={"User-Agent": HEADERS["User-Agent"]})


def fetch_rss(source: dict, cutoff: datetime, verbose: bool = False) -> List[Article]:
    articles = []
    try:
        feed = _parse_rss(source["url"])
        if verbose:
            print(f"  [{source['id']}] RSS: {len(feed.entries)} entries found")
        for entry in feed.entries[:source.get("max_articles", 20)]:
            pub = _parse_date(entry.get("published_parsed") or entry.get("updated_parsed"))
            if pub and pub < cutoff:
                continue
            if pub is None:
                pub = _now_utc()
            url = entry.get("link", "")
            if not url:
                continue
            title = entry.get("title", "").strip()
            snippet = BeautifulSoup(
                entry.get("summary", entry.get("description", "")), "html.parser"
            ).get_text(" ", strip=True)[:500]
            # Full text is fetched lazily during summarization, not here
            articles.append(Article(
                id=_article_id(url),
                source_id=source["id"],
                source_name=source["name"],
                tier=source["tier"],
                title=title,
                url=url,
                published_date=pub,
                body_snippet=snippet,
                full_text="",  # fetched later if article passes filter
                score_threshold=source["score_threshold"],
            ))
    except Exception as e:
        print(f"  [{source['id']}] RSS error: {e}")
    return articles


def fetch_scrape(source: dict, cutoff: datetime, verbose: bool = False) -> List[Article]:
    articles = []
    try:
        resp = _fetch_url(source["url"])
        if not resp:
            print(f"  [{source['id']}] Scrape: failed to fetch page")
            return []
        soup = BeautifulSoup(resp.text, "html.parser")

        # Generic article link discovery: find all <a> with meaningful href
        candidates = []
        base_url = "/".join(source["url"].split("/")[:3])

        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.startswith("/"):
                href = base_url + href
            if not href.startswith("http"):
                continue
            # Must contain the source domain
            if base_url.split("//")[-1].split("/")[0] not in href:
                continue
            # Skip nav/footer links (too short)
            text = a.get_text(strip=True)
            if len(text) < 15:
                continue
            candidates.append((href, text))

        # Filter out navigation-style links: require href to contain path depth > 1
        # (e.g. /blog/some-article-title, not just /blog/ or /)
        def is_article_link(href: str, text: str) -> bool:
            try:
                path = href.split("//", 1)[-1].split("/", 1)[-1] if "//" in href else href
                segments = [s for s in path.split("/") if s]
                # Article links typically have 2+ path segments and a meaningful title text
                return len(segments) >= 2 and len(text) >= 20
            except Exception:
                return False

        url_must_contain = source.get("url_must_contain", "")

        seen_urls = set()
        count = 0
        for url, title in candidates:
            if url in seen_urls or count >= source.get("max_articles", 20):
                break
            if not is_article_link(url, title):
                continue
            if url_must_contain and url_must_contain not in url:
                continue
            # Clean garbled titles: if title is very long or contains digits mid-word,
            # derive a cleaner title from the URL slug instead
            if len(title) > 120 or any(c.isdigit() for c in title[:20]):
                slug = url.rstrip("/").split("/")[-1]
                title = slug.replace("-", " ").title() if slug else title[:80]
            seen_urls.add(url)
            articles.append(Article(
                id=_article_id(url),
                source_id=source["id"],
                source_name=source["name"],
                tier=source["tier"],
                title=title,
                url=url,
                published_date=_now_utc(),
                body_snippet="",   # fetched later if article passes filter
                full_text="",
                score_threshold=source["score_threshold"],
            ))
            count += 1

        if verbose:
            print(f"  [{source['id']}] Scrape: {len(articles)} articles extracted")
    except Exception as e:
        print(f"  [{source['id']}] Scrape error: {e}")
    return articles


def fetch_rss_with_fallback(source: dict, cutoff: datetime, verbose: bool = False) -> List[Article]:
    """Try RSS feed. Only fall back to scraping if the RSS feed itself fails (no entries at all)."""
    try:
        feed = _parse_rss(source["url"])
        if feed.entries:
            # RSS feed works — use it even if all entries are older than cutoff
            # (scraping won't give us fresher articles if RSS doesn't have them)
            articles = []
            for entry in feed.entries[:source.get("max_articles", 20)]:
                pub = _parse_date(entry.get("published_parsed") or entry.get("updated_parsed"))
                if pub and pub < cutoff:
                    continue
                if pub is None:
                    pub = _now_utc()
                url = entry.get("link", "")
                if not url:
                    continue
                title = entry.get("title", "").strip()
                snippet = BeautifulSoup(
                    entry.get("summary", entry.get("description", "")), "html.parser"
                ).get_text(" ", strip=True)[:500]
                articles.append(Article(
                    id=_article_id(url),
                    source_id=source["id"],
                    source_name=source["name"],
                    tier=source["tier"],
                    title=title,
                    url=url,
                    published_date=pub,
                    body_snippet=snippet,
                    full_text="",
                    score_threshold=source["score_threshold"],
                ))
            if verbose:
                print(f"  [{source['id']}] RSS: {len(feed.entries)} entries, {len(articles)} within freshness window")
            return articles
        else:
            if verbose:
                print(f"  [{source['id']}] RSS returned no entries, trying scrape fallback")
    except Exception as e:
        if verbose:
            print(f"  [{source['id']}] RSS failed ({e}), trying scrape fallback")

    if "fallback_url" in source:
        fallback_source = {**source, "url": source["fallback_url"]}
        return fetch_scrape(fallback_source, cutoff, verbose)
    return []


def filter_freshness(articles: List[Article], days: int) -> List[Article]:
    cutoff = _now_utc() - timedelta(days=days)
    return [a for a in articles if a.published_date >= cutoff]


def deduplicate_against_cache(articles: List[Article], cache_path: str) -> List[Article]:
    if not os.path.exists(cache_path):
        return articles
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            cache = json.load(f)
        seen_ids = set(cache.get("articles", {}).keys())
        return [a for a in articles if a.id not in seen_ids]
    except Exception:
        return articles


def deduplicate_within_batch(articles: List[Article]) -> List[Article]:
    """Remove duplicate URLs within a single collection run."""
    seen = set()
    result = []
    for a in articles:
        if a.id not in seen:
            seen.add(a.id)
            result.append(a)
    return result


def collect_all(config_path: str = CONFIG_PATH, verbose: bool = False) -> List[Article]:
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    settings = config.get("settings", {})
    freshness_days = settings.get("freshness_days", 7)
    cutoff = _now_utc() - timedelta(days=freshness_days)
    cache_path = os.path.join(PROJECT_ROOT, settings.get("cache_file", "cache/seen_articles.json"))

    all_articles = []
    for source in config["sources"]:
        if verbose:
            print(f"\nCollecting: {source['name']} (Tier {source['tier']}, method: {source['method']})")
        try:
            method = source["method"]
            if method == "rss":
                articles = fetch_rss(source, cutoff, verbose)
            elif method == "scrape":
                articles = fetch_scrape(source, cutoff, verbose)
            elif method == "rss_with_fallback":
                articles = fetch_rss_with_fallback(source, cutoff, verbose)
            else:
                print(f"  [{source['id']}] Unknown method: {method}")
                articles = []
            if verbose:
                print(f"  -> {len(articles)} articles after freshness filter")
            all_articles.extend(articles)
        except Exception as e:
            print(f"  [{source['id']}] FAILED: {e}")

    all_articles = deduplicate_within_batch(all_articles)
    all_articles = deduplicate_against_cache(all_articles, cache_path)

    if verbose:
        print(f"\nTotal new articles after deduplication: {len(all_articles)}")
    return all_articles


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--dump", action="store_true", help="Dump collected articles to cache/raw_collected.json")
    args = parser.parse_args()

    articles = collect_all(verbose=args.verbose)
    print(f"\nCollected {len(articles)} articles total")
    for a in articles:
        print(f"  [{a.source_name}] T{a.tier} | {a.title[:70]}")

    if args.dump:
        os.makedirs(os.path.join(PROJECT_ROOT, "cache"), exist_ok=True)
        dump_path = os.path.join(PROJECT_ROOT, "cache", "raw_collected.json")
        with open(dump_path, "w", encoding="utf-8") as f:
            json.dump([a.to_dict() for a in articles], f, indent=2, ensure_ascii=False)
        print(f"\nDumped to {dump_path}")
