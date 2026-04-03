"""
summarize.py

Generates strategic summaries for scored articles and podcast transcripts using Claude.

Usage (standalone test):
    python scripts/summarize.py [--verbose] [--dry-run]
"""

import json
import os
import sys
import time
import argparse
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import List, Optional

import anthropic

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def _fetch_full_text(url: str) -> str:
    """Fetch article full text on demand."""
    if not url:
        return ""
    try:
        import time, requests
        from bs4 import BeautifulSoup
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        time.sleep(0.5)
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["nav", "header", "footer", "script", "style", "aside"]):
            tag.decompose()
        for selector in ["article", "main", "body"]:
            container = soup.select_one(selector)
            if container:
                text = " ".join(container.get_text(" ", strip=True).split())
                if len(text) > 200:
                    return text[:3000]
        return ""
    except Exception:
        return ""
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))
from collect import Article
from filter_score import ScoredArticle


@dataclass
class SummarizedItem:
    # Article identity
    source_id: str
    source_name: str
    tier: int
    title: str
    url: str
    published_date: datetime
    relevance_score: int
    cluster_id: str
    cluster_size: int
    supporting_sources: List[str]
    is_podcast: bool

    # Generated summary fields
    headline: str = ""
    what_happened: str = ""
    why_it_matters: str = ""
    strategic_implication: str = ""

    # Digest fields (used for stories ranked 6-10 — brief 2-3 sentence treatment)
    is_digest: bool = False
    digest_summary: str = ""

    # Podcast-specific fields
    podcast_topics: List[dict] = None   # [{topic, what_was_discussed, why_it_matters}]
    podcast_takeaway: str = ""
    podcast_title_en: str = ""          # YouTube title translated to English

    def __post_init__(self):
        if self.podcast_topics is None:
            self.podcast_topics = []

    def to_dict(self):
        d = asdict(self)
        d["published_date"] = self.published_date.isoformat()
        return d


ARTICLE_SYSTEM_PROMPT = """You write for a weekly AI intelligence brief read by senior technology strategists and enterprise consultants. Be direct, analytical, and free of hype. No filler sentences. Never start with "This article" or "The author"."""

PODCAST_SYSTEM_PROMPT = """You summarize podcast transcripts for a weekly AI intelligence brief read by senior technology strategists. The transcript may be in Dutch — translate key points to English before summarizing. Be direct and analytical."""


def summarize_article(item: ScoredArticle, client: anthropic.Anthropic, dry_run: bool = False) -> SummarizedItem:
    """Generate a strategic summary for one article (or cluster primary)."""
    if dry_run:
        return SummarizedItem(
            source_id=item.source_id,
            source_name=item.source_name,
            tier=item.tier,
            title=item.title,
            url=item.url,
            published_date=item.published_date,
            relevance_score=item.relevance_score,
            cluster_id=item.cluster_id,
            cluster_size=item.cluster_size,
            supporting_sources=item.supporting_sources,
            is_podcast=False,
            headline=f"[DRY RUN] {item.title[:60]}",
            what_happened="[dry-run placeholder]",
            why_it_matters="[dry-run placeholder]",
            strategic_implication="[dry-run placeholder]",
        )

    multi_source = ""
    if item.supporting_sources:
        multi_source = f"Also covered by: {', '.join(item.supporting_sources)}\n"

    # Fetch full text on demand if not already available
    full_text = item.full_text or _fetch_full_text(item.url)
    content = full_text[:2500] if full_text else item.body_snippet

    prompt = f"""Article details:
Title: {item.title}
Source: {item.source_name} (Tier {item.tier})
{multi_source}Content:
{content}

Write exactly:
1. A punchy headline (max 10 words, factual, no clickbait)
2. What happened: 1 sentence, factual
3. Why it matters: 2 sentences, focus on competitive/regulatory/capability implications for enterprises
4. Strategic implication: 1 sentence — what should a technology strategist do or watch?

Respond in JSON only:
{{"headline": "...", "what_happened": "...", "why_it_matters": "...", "strategic_implication": "..."}}"""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=ARTICLE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        data = json.loads(text)
        return SummarizedItem(
            source_id=item.source_id,
            source_name=item.source_name,
            tier=item.tier,
            title=item.title,
            url=item.url,
            published_date=item.published_date,
            relevance_score=item.relevance_score,
            cluster_id=item.cluster_id,
            cluster_size=item.cluster_size,
            supporting_sources=item.supporting_sources,
            is_podcast=False,
            headline=data.get("headline", item.title),
            what_happened=data.get("what_happened", ""),
            why_it_matters=data.get("why_it_matters", ""),
            strategic_implication=data.get("strategic_implication", ""),
        )
    except Exception as e:
        print(f"  [summarize_article] Error for '{item.title[:50]}': {e}")
        return SummarizedItem(
            source_id=item.source_id,
            source_name=item.source_name,
            tier=item.tier,
            title=item.title,
            url=item.url,
            published_date=item.published_date,
            relevance_score=item.relevance_score,
            cluster_id=item.cluster_id,
            cluster_size=item.cluster_size,
            supporting_sources=item.supporting_sources,
            is_podcast=False,
            headline=item.title,
            what_happened="[Summary unavailable — API error]",
            why_it_matters="",
            strategic_implication="",
        )


def summarize_digest_article(item: ScoredArticle, client: anthropic.Anthropic, dry_run: bool = False) -> SummarizedItem:
    """Generate a 2-3 sentence digest summary for a lower-ranked story."""
    base = SummarizedItem(
        source_id=item.source_id,
        source_name=item.source_name,
        tier=item.tier,
        title=item.title,
        url=item.url,
        published_date=item.published_date,
        relevance_score=item.relevance_score,
        cluster_id=item.cluster_id,
        cluster_size=item.cluster_size,
        supporting_sources=item.supporting_sources,
        is_podcast=False,
        is_digest=True,
    )
    if dry_run:
        base.headline = item.title[:80]
        base.digest_summary = "[dry-run digest placeholder]"
        return base

    full_text = item.full_text or _fetch_full_text(item.url)
    content = full_text[:1500] if full_text else item.body_snippet

    prompt = f"""Article:
Title: {item.title}
Source: {item.source_name}
Content: {content}

Write 2-3 sentences for a technology strategist: what happened and why it may be relevant. Be factual and concise. No headers, no bullet points.

Respond in JSON only: {{"headline": "...", "summary": "..."}}"""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=256,
            system=ARTICLE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        data = json.loads(text)
        base.headline = data.get("headline", item.title[:80])
        base.digest_summary = data.get("summary", "")
    except Exception as e:
        print(f"  [summarize_digest] Error for '{item.title[:50]}': {e}")
        base.headline = item.title[:80]
        base.digest_summary = item.relevance_rationale or "[Summary unavailable]"
    return base


def summarize_podcast(
    transcript: str,
    source_name: str,
    published_date: datetime,
    client: anthropic.Anthropic,
    dry_run: bool = False,
    youtube_title: str = "",
) -> SummarizedItem:
    """Generate a strategic summary for a podcast transcript."""
    if dry_run:
        return SummarizedItem(
            source_id="podcast",
            source_name=source_name,
            tier=3,
            title=f"{source_name} — this week's episode",
            url="",
            published_date=published_date,
            relevance_score=10,
            cluster_id="podcast",
            cluster_size=1,
            supporting_sources=[],
            is_podcast=True,
            headline=f"[DRY RUN] {source_name} podcast",
            podcast_topics=[{"topic": "dry-run", "what_was_discussed": "placeholder", "why_it_matters": "placeholder"}],
            podcast_takeaway="[dry-run placeholder]",
        )

    title_line = f'YouTube title (in Dutch): "{youtube_title}"\n' if youtube_title else ""
    prompt = f"""Podcast: {source_name}
{title_line}Transcript (may be in Dutch — translate key points to English before summarizing):
{transcript[:5000]}

Extract the 3-5 most strategically significant topics discussed. For each:
- Topic name
- What was discussed (2 sentences)
- Why it matters for technology strategists (1 sentence)

Also provide one overall strategic takeaway for the episode.
{f'Also translate the YouTube title to English and include it as "title_en".' if youtube_title else ""}

Respond in JSON only:
{{
  "headline": "Podcast: [main theme in max 8 words]",
  {"\"title_en\": \"[English translation of the YouTube title]\"," if youtube_title else ""}
  "topics": [
    {{"topic": "...", "what_was_discussed": "...", "why_it_matters": "..."}}
  ],
  "overall_takeaway": "..."
}}"""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=PODCAST_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        data = json.loads(text)
        return SummarizedItem(
            source_id="podcast",
            source_name=source_name,
            tier=3,
            title=data.get("headline", f"{source_name} — this week"),
            url="",
            published_date=published_date,
            relevance_score=10,
            cluster_id="podcast",
            cluster_size=1,
            supporting_sources=[],
            is_podcast=True,
            headline=data.get("headline", source_name),
            podcast_topics=data.get("topics", []),
            podcast_takeaway=data.get("overall_takeaway", ""),
            podcast_title_en=data.get("title_en", ""),
        )
    except Exception as e:
        print(f"  [summarize_podcast] Error: {e}")
        return SummarizedItem(
            source_id="podcast",
            source_name=source_name,
            tier=3,
            title=f"{source_name} — this week's episode",
            url="",
            published_date=published_date,
            relevance_score=10,
            cluster_id="podcast",
            cluster_size=1,
            supporting_sources=[],
            is_podcast=True,
            headline=f"{source_name} — this week",
            podcast_topics=[],
            podcast_takeaway="[Summary unavailable — API error]",
        )


def summarize_all(
    scored_articles: List[ScoredArticle],
    client: anthropic.Anthropic,
    podcast_transcript: Optional[str] = None,
    podcast_source_name: str = "AI Report",
    podcast_youtube_title: str = "",
    dry_run: bool = False,
    verbose: bool = False,
    full_count: int = 5,
) -> List[SummarizedItem]:
    results = []

    for i, item in enumerate(scored_articles):
        is_digest = i >= full_count
        if verbose:
            title_safe = item.title[:60].encode("ascii", errors="replace").decode("ascii")
            label = "digest" if is_digest else "full"
            print(f"  Summarizing [{label}] ({i+1}/{len(scored_articles)}): {title_safe}")
        if is_digest:
            summary = summarize_digest_article(item, client, dry_run)
        else:
            summary = summarize_article(item, client, dry_run)
        results.append(summary)
        if not dry_run:
            time.sleep(1)

    if podcast_transcript:
        if verbose:
            print(f"  Summarizing podcast: {podcast_source_name}")
        podcast_summary = summarize_podcast(
            podcast_transcript,
            podcast_source_name,
            datetime.now(timezone.utc),
            client,
            dry_run,
            youtube_title=podcast_youtube_title,
        )
        results.append(podcast_summary)

    return results
