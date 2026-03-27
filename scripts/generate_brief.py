"""
generate_brief.py

Orchestrator for the Weekly AI Intelligence Brief pipeline.

Usage:
    python scripts/generate_brief.py [--podcast-url URL] [--dry-run] [--verbose] [--skip-collect]

Environment:
    ANTHROPIC_API_KEY  (required unless --dry-run)
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import List, Optional

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))

import yaml
import anthropic

from collect import collect_all, Article, _article_id
from filter_score import score_and_filter, ScoredArticle
from summarize import summarize_all, SummarizedItem
from compile_brief import render_markdown, render_email_text, write_brief

CONFIG_PATH = os.path.join(PROJECT_ROOT, "config", "sources.yaml")
CACHE_PATH = os.path.join(PROJECT_ROOT, "cache", "seen_articles.json")
RAW_CACHE_PATH = os.path.join(PROJECT_ROOT, "cache", "raw_collected.json")


# ---------- Cache helpers ----------

def load_cache() -> dict:
    if os.path.exists(CACHE_PATH):
        try:
            with open(CACHE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"articles": {}, "last_updated": None}


def save_cache(cache: dict, articles: List[Article]):
    """Add collected articles to cache and prune entries older than 30 days."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for a in articles:
        cache["articles"][a.id] = {
            "title": a.title,
            "url": a.url,
            "seen_date": today,
        }

    # Prune entries older than 30 days
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
    cache["articles"] = {
        k: v for k, v in cache["articles"].items()
        if v.get("seen_date", "1970-01-01") >= cutoff
    }
    cache["last_updated"] = datetime.now(timezone.utc).isoformat()

    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    tmp_path = CACHE_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, CACHE_PATH)


# ---------- Podcast transcript ----------

def fetch_podcast_transcript(youtube_url: str, verbose: bool = False) -> Optional[str]:
    """Fetch transcript from a YouTube URL. Handles multilingual (tries EN, falls back)."""
    import re
    match = re.search(r"(?:v=|youtu\.be/)([a-zA-Z0-9_-]{11})", youtube_url)
    if not match:
        print(f"  Could not extract video ID from URL: {youtube_url}")
        return None
    video_id = match.group(1)
    if verbose:
        print(f"  Fetching transcript for video: {video_id}")

    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        api = YouTubeTranscriptApi()
        transcript_list = api.list(video_id)
        available = [(t.language_code, t.is_generated) for t in transcript_list]
        if verbose:
            print(f"  Available languages: {available}")

        try:
            transcript = api.fetch(video_id, languages=["en"])
        except Exception:
            first_lang = available[0][0] if available else "en"
            if verbose:
                print(f"  English not available, fetching '{first_lang}'")
            transcript = api.fetch(video_id, languages=[first_lang])

        text = " ".join([t.text for t in transcript])

        # Save transcript to file
        transcripts_dir = os.path.join(PROJECT_ROOT, "transcripts")
        os.makedirs(transcripts_dir, exist_ok=True)
        transcript_path = os.path.join(transcripts_dir, f"{video_id}.txt")
        with open(transcript_path, "w", encoding="utf-8") as f:
            f.write(text)
        if verbose:
            print(f"  Transcript saved: {transcript_path} ({len(text)} chars)")
        return text
    except Exception as e:
        print(f"  [fetch_podcast_transcript] Error: {e}")
        return None


# ---------- Source stats ----------

def compute_source_stats(config_path: str, all_articles: List[Article], passing: List[ScoredArticle]) -> List[dict]:
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    passing_source_ids = {a.source_id for a in passing}
    stats = []
    for source in config["sources"]:
        fetched = sum(1 for a in all_articles if a.source_id == source["id"])
        included = sum(1 for a in passing if a.source_id == source["id"])
        stats.append({
            "name": source["name"],
            "tier": source["tier"],
            "fetched": fetched,
            "included": included,
        })
    return stats


# ---------- Main ----------

def main():
    parser = argparse.ArgumentParser(description="Generate weekly AI Intelligence Brief")
    parser.add_argument("--podcast-url", help="YouTube URL for this week's AI Report episode")
    parser.add_argument("--dry-run", action="store_true", help="Skip Claude API calls, use mock scores")
    parser.add_argument("--skip-collect", action="store_true", help="Reuse last cached raw collection")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    print(f"\n{'='*60}")
    print(f"AI Intelligence Brief — {date_str}")
    print(f"{'='*60}")

    # --- Validate API key ---
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key and not args.dry_run:
        print("\nERROR: ANTHROPIC_API_KEY environment variable not set.")
        print("Set it with: set ANTHROPIC_API_KEY=your-key-here")
        print("Or use --dry-run to test without API calls.")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key or "dummy")
    cache = load_cache()

    # --- PHASE 1: COLLECT ---
    print("\n[1/4] Collecting articles...")
    if args.skip_collect and os.path.exists(RAW_CACHE_PATH):
        print("  Using cached raw collection")
        with open(RAW_CACHE_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        from datetime import datetime as dt
        articles = []
        for d in raw:
            d["published_date"] = dt.fromisoformat(d["published_date"])
            articles.append(Article(**{k: v for k, v in d.items() if k in Article.__dataclass_fields__}))
    else:
        articles = collect_all(CONFIG_PATH, verbose=args.verbose)
        # Save raw collection for debugging / --skip-collect reuse
        os.makedirs(os.path.dirname(RAW_CACHE_PATH), exist_ok=True)
        with open(RAW_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump([a.to_dict() for a in articles], f, indent=2, ensure_ascii=False)

    print(f"  {len(articles)} new articles collected")

    # --- PHASE 2: PODCAST TRANSCRIPT ---
    podcast_transcript = None
    if args.podcast_url:
        print(f"\n[+] Fetching podcast transcript...")
        podcast_transcript = fetch_podcast_transcript(args.podcast_url, verbose=args.verbose)
        if podcast_transcript:
            print(f"  Transcript fetched ({len(podcast_transcript)} chars)")
        else:
            print("  Warning: Could not fetch podcast transcript — podcast section will be skipped")

    # --- PHASE 3: FILTER & SCORE ---
    print("\n[2/4] Scoring and filtering articles...")
    scored = score_and_filter(articles, client, dry_run=args.dry_run, verbose=args.verbose)
    print(f"  {len(scored)} articles passed filter (from {len(articles)} collected)")

    # --- PHASE 4: SUMMARIZE ---
    print("\n[3/4] Generating strategic summaries...")
    summarized = summarize_all(
        scored,
        client,
        podcast_transcript=podcast_transcript,
        podcast_source_name="AI Report",
        dry_run=args.dry_run,
        verbose=args.verbose,
    )
    print(f"  {len(summarized)} items summarized")

    # --- PHASE 5: COMPILE ---
    print("\n[4/4] Compiling brief...")
    source_stats = compute_source_stats(CONFIG_PATH, articles, scored)
    metadata = {
        "date": date_str,
        "n_sources": len(source_stats),
        "n_collected": len(articles),
        "n_included": len([i for i in summarized if not i.is_podcast]),
        "source_stats": source_stats,
    }
    md_text = render_markdown(summarized, metadata)
    email_text = render_email_text(summarized, metadata)
    md_path, email_path = write_brief(md_text, email_text, date_str)
    print(f"  Brief written to: {md_path}")
    print(f"  Email text:       {email_path}")

    # --- UPDATE CACHE ---
    if not args.dry_run:
        save_cache(cache, articles)
        print(f"\n  Cache updated ({len(cache['articles'])} total seen articles)")

    # --- SUMMARY ---
    print(f"\n{'='*60}")
    print(f"Done. {len([i for i in summarized if not i.is_podcast])} stories | {'+ podcast' if podcast_transcript else 'no podcast'}")
    print(f"Brief: {md_path}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
