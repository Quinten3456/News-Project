"""
filter_score.py

Scores articles for strategic relevance using Claude API.
Applies tier-based thresholds and groups cross-source stories.

Usage (standalone test):
    python scripts/filter_score.py --input cache/raw_collected.json [--verbose]
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

# Import Article from collect
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))
from collect import Article


@dataclass
class ScoredArticle(Article):
    relevance_score: int = 0
    relevance_rationale: str = ""
    cluster_id: str = ""
    cluster_size: int = 1
    supporting_sources: List[str] = None
    passed_threshold: bool = False

    def __post_init__(self):
        if self.supporting_sources is None:
            self.supporting_sources = []

    def to_dict(self):
        d = super().to_dict()
        d["relevance_score"] = self.relevance_score
        d["relevance_rationale"] = self.relevance_rationale
        d["cluster_id"] = self.cluster_id
        d["cluster_size"] = self.cluster_size
        d["supporting_sources"] = self.supporting_sources
        d["passed_threshold"] = self.passed_threshold
        return d


SCORING_SYSTEM_PROMPT = """You are a relevance filter for a weekly AI intelligence brief read by senior technology strategists and enterprise consultants.

Score each article 1-10 for strategic relevance using this scale:
- 9-10: Changes competitive dynamics or strategic options significantly (e.g., major model release, significant regulatory ruling, large enterprise AI deployment)
- 7-8:  Worth tracking — meaningful new capability, partnership, or finding with near-term implications for enterprises
- 5-6:  Incremental — minor product update, general trend piece, no new data
- 1-4:  Skip — hype, speculation, listicle, rehash of known information, or developer-only content

Primary question: Is this worth knowing to support better decision-making and client conversations for a technology strategist?"""


CLUSTERING_SYSTEM_PROMPT = """You are deduplicating a list of AI news articles. Group articles that cover the same underlying story, announcement, or event — even if covered by different sources.

Rules:
- Articles about the same product launch, partnership, or regulatory decision belong in one cluster
- Articles covering different aspects of a broad topic (e.g., "AI regulation" vs "EU AI Act vote") are separate clusters unless they directly reference the same event
- Each article must appear in exactly one cluster"""


def score_batch(articles: List[Article], client: anthropic.Anthropic, dry_run: bool = False) -> List[dict]:
    """Score a batch of articles. Returns list of {id, score, rationale}."""
    if dry_run:
        return [{"id": a.id, "score": 7 if a.tier == 1 else 6, "rationale": "[dry-run mock score]"} for a in articles]

    payload = [
        {
            "id": a.id,
            "title": a.title,
            "source": a.source_name,
            "tier": a.tier,
            "snippet": (a.body_snippet or a.full_text[:500])[:400],
        }
        for a in articles
    ]

    prompt = f"Articles to score:\n{json.dumps(payload, ensure_ascii=False)}\n\nRespond with a JSON array only."

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=SCORING_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        # Strip markdown code fences if present
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text)
    except Exception as e:
        print(f"  [score_batch] Error: {e}")
        return []


def cluster_articles(articles: List[ScoredArticle], client: anthropic.Anthropic, dry_run: bool = False) -> List[dict]:
    """Group articles into story clusters. Returns list of {cluster_id, article_ids, canonical_title}."""
    if len(articles) <= 1:
        return [{"cluster_id": a.id, "article_ids": [a.id], "canonical_title": a.title} for a in articles]

    if dry_run:
        return [{"cluster_id": a.id, "article_ids": [a.id], "canonical_title": a.title} for a in articles]

    payload = [{"id": a.id, "title": a.title, "source": a.source_name} for a in articles]
    prompt = f"Articles to cluster:\n{json.dumps(payload, ensure_ascii=False)}\n\nRespond with a JSON array of clusters only."

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=CLUSTERING_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text)
    except Exception as e:
        print(f"  [cluster_articles] Error: {e}")
        return [{"cluster_id": a.id, "article_ids": [a.id], "canonical_title": a.title} for a in articles]


def score_and_filter(
    articles: List[Article],
    client: anthropic.Anthropic,
    dry_run: bool = False,
    verbose: bool = False,
) -> List[ScoredArticle]:
    if not articles:
        return []

    # Score in batches of 10
    scored_map = {}
    batch_size = 10
    for i in range(0, len(articles), batch_size):
        batch = articles[i: i + batch_size]
        if verbose:
            print(f"  Scoring batch {i // batch_size + 1} ({len(batch)} articles)...")
        results = score_batch(batch, client, dry_run)
        for r in results:
            scored_map[r["id"]] = r
        if not dry_run:
            time.sleep(1)

    # Build ScoredArticle objects
    scored = []
    for a in articles:
        result = scored_map.get(a.id, {})
        score = result.get("score", 0)
        rationale = result.get("rationale", "")
        sa = ScoredArticle(
            **{k: v for k, v in asdict(a).items() if k not in (
                "relevance_score", "relevance_rationale", "cluster_id",
                "cluster_size", "supporting_sources", "passed_threshold",
                "published_date"
            )},
            published_date=a.published_date,
            relevance_score=score,
            relevance_rationale=rationale,
        )
        sa.passed_threshold = score >= a.score_threshold
        if verbose:
            status = "PASS" if sa.passed_threshold else "FAIL"
            print(f"  [{status}] score={score} threshold={a.score_threshold} | {a.source_name} | {a.title[:60]}")
        scored.append(sa)

    # Filter to passing articles
    passing = [a for a in scored if a.passed_threshold]
    if verbose:
        print(f"\n  {len(passing)}/{len(scored)} articles passed threshold")

    if not passing:
        return []

    # Cluster passing articles
    if verbose:
        print("  Clustering articles by story...")
    clusters = cluster_articles(passing, client, dry_run)

    # Build id -> article map
    article_map = {a.id: a for a in passing}

    # Apply cluster metadata
    result = []
    for cluster in clusters:
        cluster_id = cluster["cluster_id"]
        ids = cluster["article_ids"]
        size = len(ids)

        # Find the primary article (highest tier = lowest number, then highest score)
        cluster_articles_list = [article_map[aid] for aid in ids if aid in article_map]
        if not cluster_articles_list:
            continue

        primary = sorted(cluster_articles_list, key=lambda x: (x.tier, -x.relevance_score))[0]
        supporting = [a.source_name for a in cluster_articles_list if a.id != primary.id]

        # Apply minor cross-source boost to primary
        if size >= 2:
            primary.relevance_score = min(10, primary.relevance_score + 1)

        primary.cluster_id = cluster_id
        primary.cluster_size = size
        primary.supporting_sources = supporting
        result.append(primary)

    # Sort: by score descending
    result.sort(key=lambda x: -x.relevance_score)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="cache/raw_collected.json")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    input_path = os.path.join(PROJECT_ROOT, args.input)
    with open(input_path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    articles = []
    for d in raw:
        d["published_date"] = datetime.fromisoformat(d["published_date"])
        articles.append(Article(**{k: v for k, v in d.items() if k in Article.__dataclass_fields__}))

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key and not args.dry_run:
        print("ERROR: ANTHROPIC_API_KEY not set. Use --dry-run to test without API.")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key or "dummy")
    scored = score_and_filter(articles, client, dry_run=args.dry_run, verbose=args.verbose)

    print(f"\n{len(scored)} articles passed filter:")
    for a in scored:
        multi = f" [+{len(a.supporting_sources)} sources]" if a.supporting_sources else ""
        print(f"  score={a.relevance_score} T{a.tier}{multi} | {a.source_name} | {a.title[:70]}")
