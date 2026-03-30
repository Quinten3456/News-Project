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
- 9-10: Changes competitive dynamics or strategic options significantly (major model release, significant regulatory ruling, large enterprise AI deployment)
- 7-8:  Worth tracking — meaningful new AI capability, partnership, or finding with near-term implications for enterprises
- 4-6:  Incremental — minor AI product update, general trend piece, no new data
- 1-3:  Skip — not about AI, hype, speculation, listicle, rehash, or developer-only content

Primary question: Is this an AI development worth knowing to support better decision-making for a technology strategist?
If the article is not about AI or has no strategic relevance to AI, score it 1-3."""


CLUSTERING_SYSTEM_PROMPT = """You are deduplicating a list of AI news articles. Group articles that cover the same underlying story, announcement, or event — even if covered by different sources.

Rules:
- Articles about the same product launch, partnership, or regulatory decision belong in one cluster
- Articles covering different aspects of a broad topic (e.g., "AI regulation" vs "EU AI Act vote") are separate clusters unless they directly reference the same event
- Each article must appear in exactly one cluster"""

EDITORIAL_SELECT_SYSTEM_PROMPT = """You are the editor of a weekly AI intelligence brief for technology strategists. You will receive fully analyzed AI news stories including their summaries. Select the 4-6 most worth reading this week. Prioritize stories that change competitive dynamics, signal a strategic shift, or give actionable intelligence for enterprise decisions. Avoid duplicating themes — if two stories cover the same development, pick only the more informative one.

Return a JSON array of article IDs in ranked order, most important first. Example: ["abc123", "def456"]"""


_DRY_RUN_SCORES = [5, 7, 6, 8, 5, 9, 6, 7, 4, 8]  # realistic spread, ~40% pass at threshold 7


def score_batch(articles: List[Article], client: anthropic.Anthropic, dry_run: bool = False) -> List[dict]:
    """Score a batch of articles. Returns list of {id, score, rationale}."""
    if dry_run:
        results = []
        for i, a in enumerate(articles):
            score = _DRY_RUN_SCORES[i % len(_DRY_RUN_SCORES)]
            results.append({"id": a.id, "score": score, "rationale": f"[dry-run mock score {score}]"})
        return results

    payload = [
        {
            "id": a.id,
            "title": a.title,
            "source": a.source_name,
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

    fallback = [{"cluster_id": a.id, "article_ids": [a.id], "canonical_title": a.title} for a in articles]
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
        parsed = json.loads(text)
        # Unwrap if Claude returned {"clusters": [...]} instead of a bare array
        if isinstance(parsed, dict):
            parsed = next((v for v in parsed.values() if isinstance(v, list)), fallback)
        # Normalise keys: accept "id" as alias for "cluster_id", "articles" for "article_ids"
        normalised = []
        for c in parsed:
            normalised.append({
                "cluster_id": c.get("cluster_id") or c.get("id", ""),
                "article_ids": c.get("article_ids") or c.get("articles", []),
                "canonical_title": c.get("canonical_title") or c.get("title", ""),
            })
        # Validate: every article must appear in exactly one cluster
        seen = set()
        for c in normalised:
            seen.update(c["article_ids"])
        if not seen:
            return fallback
        return normalised
    except Exception as e:
        print(f"  [cluster_articles] Error: {e}")
        return fallback


def editorial_select(
    items: list,
    client: anthropic.Anthropic,
    dry_run: bool = False,
    verbose: bool = False,
) -> list:
    """Claude picks the 4-6 best stories from fully analyzed candidates.
    Operates on SummarizedItem objects. Podcast items are always preserved."""
    candidates = [i for i in items if not i.is_podcast]
    podcasts = [i for i in items if i.is_podcast]

    if len(candidates) <= 6:
        return items

    if dry_run:
        if verbose:
            print(f"  [editorial_select] dry-run: keeping top 6 of {len(candidates)}")
        return candidates[:6] + podcasts

    payload = [
        {
            "id": i.cluster_id,
            "title": i.title,
            "source": i.source_name,
            "what": i.what_happened,
            "why": i.why_it_matters,
            "implication": i.strategic_implication,
        }
        for i in candidates
    ]
    prompt = (
        f"From the {len(candidates)} analyzed stories below, select the 4-6 most worth "
        f"reading this week. Return a JSON array of article IDs in ranked order.\n\n"
        f"{json.dumps(payload, ensure_ascii=False)}"
    )

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=256,
            system=EDITORIAL_SELECT_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        selected_ids = json.loads(text)
    except Exception as e:
        print(f"  [editorial_select] Error: {e} — keeping top 6 by score")
        return candidates[:6] + podcasts

    id_to_item = {i.cluster_id: i for i in candidates}
    selected = [id_to_item[aid] for aid in selected_ids if aid in id_to_item]

    # Safety: pad to 4 if Claude returned fewer
    if len(selected) < 4:
        included_ids = {i.cluster_id for i in selected}
        for i in candidates:
            if i.cluster_id not in included_ids:
                selected.append(i)
            if len(selected) >= 4:
                break

    if verbose:
        print(f"  [editorial_select] {len(candidates)} → {len(selected)} articles selected")
    return selected + podcasts


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
            title_safe = a.title[:60].encode("ascii", errors="replace").decode("ascii")
            print(f"  [{status}] score={score} threshold={a.score_threshold} | {a.source_name} | {title_safe}")
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
