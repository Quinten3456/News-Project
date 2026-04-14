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

_firecrawl_client = None  # type: Optional[object]


def _get_firecrawl_client():
    """Return a cached Firecrawl client, or None if SDK/key is unavailable."""
    global _firecrawl_client
    if _firecrawl_client is not None:
        return _firecrawl_client
    api_key = os.environ.get("FIRECRAWL_API_KEY")
    if not api_key:
        return None
    try:
        from firecrawl import Firecrawl
        _firecrawl_client = Firecrawl(api_key=api_key)
        return _firecrawl_client
    except ImportError:
        print("  [firecrawl] firecrawl-py not installed. Run: pip install firecrawl-py")
        return None


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/xml, text/xml, */*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
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
    """Fetch full article text. Falls back to Firecrawl if plain HTTP returns too little."""
    # 1. Try plain HTTP first (free, works for most sources)
    try:
        time.sleep(0.5)
        resp = _fetch_url(url)
        if resp and resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            for tag in soup(["script", "style", "nav", "header", "footer",
                              "aside", "form"]):
                tag.decompose()
            for sel in ["article", "main", "[role='main']", "body"]:
                el = soup.select_one(sel)
                if el:
                    text = el.get_text(" ", strip=True)[:3000]
                    if len(text) >= 200:   # got real content
                        return text
    except Exception:
        pass

    # 2. Firecrawl fallback — only if plain HTTP returned too little
    client = _get_firecrawl_client()
    if client is None:
        return ""
    try:
        result = client.scrape(url=url, formats=["markdown"])
        markdown = getattr(result, "markdown", "") or ""
        return markdown[:3000]
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
    keywords = [kw.lower() for kw in source.get("title_keywords", [])]
    url_must_contain = source.get("url_must_contain", "")
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
            if url_must_contain and url_must_contain not in url:
                continue
            title = entry.get("title", "").strip()
            # Keyword pre-filter: if defined, skip articles whose title matches none
            if keywords:
                title_lower = title.lower()
                if not any(kw in title_lower for kw in keywords):
                    continue
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
    except Exception as e:
        print(f"  [{source['id']}] RSS error: {e}")
    return articles


def _parse_date_text(text: str) -> Optional[datetime]:
    """Parse a display date string like 'Mar 18, 2026' into a UTC datetime."""
    try:
        dt = dateutil_parser.parse(text.strip())
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _fetch_scrape_structured(
    source: dict, cutoff: datetime, soup: BeautifulSoup,
    base_url: str, verbose: bool
) -> List[Article]:
    """Extract articles using article_selector + date_selector from source config.
    Returns articles with real publish dates instead of today's date.
    Falls back to a link-based approach if selectors return 0 results (handles
    sites with CSS-module-hashed class names, e.g. Anthropic)."""
    import re
    articles = []
    seen_urls = set()
    url_must_contain = source.get("url_must_contain", "")
    date_sel = source["date_selector"]

    link_sel = source.get("link_selector", "")

    for container in soup.select(source["article_selector"])[:source.get("max_articles", 20)]:
        if link_sel:
            link_el = container.select_one(link_sel)
        else:
            link_el = container if container.name == "a" else container.find("a", href=True)
        if not link_el:
            continue
        href = link_el.get("href", "")
        if href.startswith("/"):
            href = base_url + href
        if not href.startswith("http") or href in seen_urls:
            continue
        if url_must_contain and url_must_contain not in href:
            continue

        date_el = container.select_one(date_sel)
        date_text = (date_el.get("datetime") or date_el.get_text()) if date_el else None
        pub = _parse_date_text(date_text) if date_text else None
        if pub and pub < cutoff:
            continue
        if pub is None:
            pub = _now_utc()

        title = link_el.get_text(strip=True)
        if not title or len(title) < 10:
            heading = container.find(["h1", "h2", "h3", "h4"])
            title = heading.get_text(strip=True) if heading else href.rstrip("/").split("/")[-1]

        seen_urls.add(href)
        articles.append(Article(
            id=_article_id(href),
            source_id=source["id"],
            source_name=source["name"],
            tier=source["tier"],
            title=title,
            url=href,
            published_date=pub,
            body_snippet="",
            full_text="",
            score_threshold=source["score_threshold"],
        ))

    # Fallback: if selectors matched nothing (e.g. CSS-module hashed classes),
    # find links by url_must_contain and extract dates from surrounding text.
    if not articles and url_must_contain:
        month_re = re.compile(
            r'\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|'
            r'Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|'
            r'Dec(?:ember)?)\s+\d{1,2},\s+\d{4}'
        )
        # Category words to strip from start of extracted titles
        category_prefix_re = re.compile(
            r'^(Announcements|Product|Policy|Research|News|Update|Blog)\s*', re.IGNORECASE
        )
        for a in soup.find_all("a", href=True):
            href = a.get("href", "")
            if href.startswith("/"):
                href = base_url + href
            if not href.startswith("http") or href in seen_urls:
                continue
            if url_must_contain not in href:
                continue
            # Skip navigation links: article slugs always contain a hyphen
            last_segment = href.rstrip("/").split("/")[-1]
            if "-" not in last_segment:
                continue
            if len(articles) >= source.get("max_articles", 20):
                break

            # Walk up to find the first ancestor whose text contains a date
            pub = None
            container = a
            for _ in range(4):
                text = container.get_text(" ", strip=True)
                m = month_re.search(text)
                if m:
                    pub = _parse_date_text(m.group(0))
                    if pub:
                        break
                if container.parent:
                    container = container.parent
                else:
                    break

            # In link fallback, skip articles with no visible date — they are
            # likely footer/related links that don't represent fresh content.
            if pub is None or pub < cutoff:
                continue

            # Extract title: prefer heading inside container, else link text
            heading = container.find(["h1", "h2", "h3", "h4"])
            if heading:
                title = heading.get_text(strip=True)
            else:
                # Strip date and category from link text
                raw = a.get_text(" ", strip=True)
                title = month_re.sub("", raw).strip()
                title = category_prefix_re.sub("", title).strip()
            if not title or len(title) < 5:
                title = href.rstrip("/").split("/")[-1].replace("-", " ").title()

            seen_urls.add(href)
            articles.append(Article(
                id=_article_id(href),
                source_id=source["id"],
                source_name=source["name"],
                tier=source["tier"],
                title=title,
                url=href,
                published_date=pub,
                body_snippet="",
                full_text="",
                score_threshold=source["score_threshold"],
            ))

        if verbose and articles:
            print(f"  [{source['id']}] Structured scrape (link fallback): {len(articles)} articles")

    if verbose:
        print(f"  [{source['id']}] Structured scrape: {len(articles)} articles with real dates")
    return articles


def fetch_scrape(source: dict, cutoff: datetime, verbose: bool = False) -> List[Article]:
    articles = []
    try:
        resp = _fetch_url(source["url"])
        if not resp:
            print(f"  [{source['id']}] Scrape: failed to fetch page")
            return []
        soup = BeautifulSoup(resp.text, "html.parser")
        base_url = "/".join(source["url"].split("/")[:3])

        if source.get("article_selector") and source.get("date_selector"):
            return _fetch_scrape_structured(source, cutoff, soup, base_url, verbose)

        # Generic article link discovery: find all <a> with meaningful href
        candidates = []

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
    """Try RSS feed. Fall back to scraping if RSS has no entries or all entries are stale."""
    try:
        feed = _parse_rss(source["url"])
        if feed.entries:
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
            if articles:
                return articles
            if verbose:
                print(f"  [{source['id']}] RSS entries all stale, trying scrape fallback")
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


def fetch_date_range(source: dict, cutoff: datetime, verbose: bool = False) -> List[Article]:
    """Fetch a date-based source by constructing daily URLs for the freshness window.
    Extracts individual story cards per day using selectors defined in source config."""
    import re
    articles = []
    seen_urls = set()
    base_url = source["base_url"]
    story_sel = source["story_selector"]
    headline_sel = source["headline_selector"]
    link_sel = source["link_selector"]
    summary_sel = source.get("summary_selector", "")
    skip_sections = set(source.get("skip_sections", []))

    today = _now_utc().date()
    cutoff_date = cutoff.date()
    current = today
    while current >= cutoff_date:
        date_str = current.strftime("%Y-%m-%d")
        url = f"{base_url}{date_str}"
        pub_date = datetime(current.year, current.month, current.day, tzinfo=timezone.utc)

        resp = _fetch_url(url)
        if resp is not None:
            try:
                soup = BeautifulSoup(resp.text, "html.parser")

                # Identify story cards that belong to skipped sections (e.g. ads)
                skip_these = set()
                for section in soup.find_all("section"):
                    if section.get("id", "") in skip_sections:
                        for story in section.select(story_sel):
                            skip_these.add(id(story))

                for story in soup.select(story_sel):
                    if id(story) in skip_these:
                        continue
                    link_el = story.select_one(link_sel)
                    if not link_el:
                        continue
                    href = link_el.get("href", "")
                    if not href.startswith("http") or href in seen_urls:
                        continue
                    headline_el = story.select_one(headline_sel)
                    if not headline_el:
                        continue
                    title = re.sub(r'\s*\(\d+\s+minute read\)\s*$', '',
                                   headline_el.get_text(strip=True))
                    if not title:
                        continue
                    snippet = ""
                    if summary_sel:
                        summary_el = story.select_one(summary_sel)
                        if summary_el:
                            snippet = summary_el.get_text(" ", strip=True)[:500]
                    seen_urls.add(href)
                    articles.append(Article(
                        id=_article_id(href),
                        source_id=source["id"],
                        source_name=source["name"],
                        tier=source["tier"],
                        title=title,
                        url=href,
                        published_date=pub_date,
                        body_snippet=snippet,
                        full_text="",
                        score_threshold=source["score_threshold"],
                    ))
            except Exception as e:
                if verbose:
                    print(f"  [{source['id']}] Error parsing {url}: {e}")
        current -= timedelta(days=1)
        time.sleep(0.5)

    if verbose:
        print(f"  [{source['id']}] date_range: {len(articles)} individual stories")
    return articles


def fetch_firecrawl(source: dict, cutoff: datetime, verbose: bool = False) -> List[Article]:
    """
    Scrape a listing/index page via the Firecrawl SDK (1 API credit per call).
    Parses markdown for inline-link titles + nearby dates; uses result.links as
    secondary URL discovery. Falls back to [] on any failure.
    """
    import re

    client = _get_firecrawl_client()
    if client is None:
        print(f"  [{source['id']}] firecrawl: no client available (check FIRECRAWL_API_KEY)")
        return []

    url_must_contain = source.get("url_must_contain", "")
    max_articles = source.get("max_articles", 20)

    try:
        result = client.scrape(url=source["url"], formats=["markdown", "links"])
    except Exception as e:
        print(f"  [{source['id']}] firecrawl: API error: {e}")
        return []

    markdown_text: str = getattr(result, "markdown", "") or ""
    raw_links: list = getattr(result, "links", []) or []

    if verbose:
        print(f"  [{source['id']}] firecrawl: {len(markdown_text)} chars markdown, {len(raw_links)} raw links")

    inline_link_re = re.compile(r'\[([^\]]{5,200})\]\((https?://[^\)]+)\)')
    month_re = re.compile(
        r'\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|'
        r'Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|'
        r'Dec(?:ember)?)\s+\d{1,2},?\s+\d{4}', re.IGNORECASE)
    iso_date_re = re.compile(r'\b(20\d{2}-\d{2}-\d{2})\b')

    lines = markdown_text.splitlines()
    url_data: dict = {}

    for i, line in enumerate(lines):
        for m in inline_link_re.finditer(line):
            title_candidate = m.group(1).strip()
            article_url = m.group(2).strip()
            if article_url in url_data:
                continue
            # Skip CDN/image URLs, broken image-link captures, and nav/author pages
            if "?" in article_url or title_candidate.startswith("!"):
                continue
            url_path = article_url.split("//", 1)[-1].split("/", 1)[-1] if "//" in article_url else ""
            path_segments = [s for s in url_path.split("/") if s]
            if len(path_segments) < 2 or "/author/" in article_url:
                continue
            context = "\n".join(lines[max(0, i - 3) : i + 8])
            date_str = None
            dm = month_re.search(context) or iso_date_re.search(context)
            if dm:
                date_str = dm.group(0)
            snippet = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', context)
            snippet = re.sub(r'[#*_`>]+', '', snippet).strip()[:300]
            url_data[article_url] = (title_candidate, date_str, snippet)

    for link_item in raw_links:
        link_url = link_item.get("url", "") if isinstance(link_item, dict) else str(link_item)
        link_url = link_url.strip()
        if not link_url.startswith("http") or link_url in url_data:
            continue
        # Skip CDN image URLs, author pages, and shallow nav URLs (< 2 path segments)
        if "?" in link_url or "/author/" in link_url:
            continue
        raw_path = link_url.split("//", 1)[-1].split("/", 1)[-1] if "//" in link_url else ""
        if len([s for s in raw_path.split("/") if s]) < 2:
            continue
        slug = link_url.rstrip("/").split("/")[-1]
        title_from_slug = slug.replace("-", " ").title() if "-" in slug else ""
        if len(title_from_slug) >= 10:
            url_data[link_url] = (title_from_slug, None, "")

    articles: List[Article] = []
    seen_urls: set = set()

    for article_url, (title, date_str, snippet) in url_data.items():
        if len(articles) >= max_articles:
            break
        if article_url in seen_urls:
            continue
        if url_must_contain and url_must_contain not in article_url:
            continue
        last_segment = article_url.rstrip("/").split("/")[-1]
        if "-" not in last_segment:
            continue
        pub = _parse_date_text(date_str) if date_str else None
        if pub and pub < cutoff:
            continue
        if pub is None:
            if source.get("require_date", False):
                continue
            pub = _now_utc()
        seen_urls.add(article_url)
        articles.append(Article(
            id=_article_id(article_url),
            source_id=source["id"],
            source_name=source["name"],
            tier=source["tier"],
            title=title,
            url=article_url,
            published_date=pub,
            body_snippet=snippet,
            full_text="",
            score_threshold=source["score_threshold"],
        ))

    if verbose:
        no_date = sum(1 for a in articles if a.published_date.date() == _now_utc().date())
        print(f"  [{source['id']}] firecrawl: {len(articles)} articles after filtering "
              f"({no_date} with no extracted date, assigned today)")
    return articles


def filter_freshness(articles: List[Article], days: int) -> List[Article]:
    cutoff = _now_utc() - timedelta(days=days)
    return [a for a in articles if a.published_date >= cutoff]


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

    all_articles = []
    for source in config["sources"]:
        source_cutoff = _now_utc() - timedelta(days=source.get("freshness_days", freshness_days))
        if verbose:
            print(f"\nCollecting: {source['name']} (Tier {source['tier']}, method: {source['method']})")
        try:
            method = source["method"]
            if method == "rss":
                articles = fetch_rss(source, source_cutoff, verbose)
            elif method == "scrape":
                articles = fetch_scrape(source, source_cutoff, verbose)
            elif method == "rss_with_fallback":
                articles = fetch_rss_with_fallback(source, source_cutoff, verbose)
            elif method == "date_range":
                articles = fetch_date_range(source, source_cutoff, verbose)
            elif method == "firecrawl":
                articles = fetch_firecrawl(source, source_cutoff, verbose)
            else:
                print(f"  [{source['id']}] Unknown method: {method}")
                articles = []
            if verbose:
                print(f"  -> {len(articles)} articles after freshness filter")
            all_articles.extend(articles)
        except Exception as e:
            print(f"  [{source['id']}] FAILED: {e}")

    all_articles = deduplicate_within_batch(all_articles)

    if verbose:
        print(f"\nTotal articles collected: {len(all_articles)}")
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
