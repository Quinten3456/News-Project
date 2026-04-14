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


SCORING_SYSTEM_PROMPT = """You are a relevance filter for a weekly AI intelligence brief read by a senior technology strategy consultant in a Business of Technology Advisory practice at a top consulting firm. The reader advises CIOs and CTOs of large enterprises (banks, retailers, industrials, government) on IT strategy and roadmaps, Technology Operating Model design, IT sourcing and vendor strategy, and how the IT function could absorb AI. The reader is NOT a data scientist, ML researcher, or AI product builder. They care about AI only insofar as it reshapes how the IT organization is structured, funded, sourced, governed, and run.

Scope is AI-related news only. Articles not primarily about AI are out of scope and should be scored 1-3 regardless of how interesting they are on other dimensions.

Step 1 — GATE: Before scoring each article, assess whether it concretely informs any of these six questions:
  Q1. How should the CIO restructure the technology organization or operating model to absorb AI?
  Q2. What AI-related items belong in, or should leave, the IT roadmap in the next 6-24 months?
  Q3. How should the client source AI capability — build, buy, partner, which vendor, which contract model, at what unit cost?
  Q4. What AI governance, risk, cybersecurity, assurance, or control changes are required for AI? This includes AI model vulnerabilities, adversarial attacks on enterprise AI systems, data poisoning, and AI-specific security incidents that affect how enterprises deploy or govern AI.
  Q5. How do the cost, talent, or delivery economics of running IT shift because of AI?
  Q6. What new AI capabilities from major platforms (OpenAI, Google, Anthropic, Microsoft, AWS) change what enterprises can realistically build or buy in the next 12 months?
If the article does not concretely inform a specific question, the article is capped at 5. "It's about AI and enterprises care about AI" is not a specific way.

Step 2 — SCORE on decision impact for THIS reader, not on general AI importance:
- 9-10 ESSENTIAL. Would change advice the reader is giving a client or force the rewrite of an in-progress deliverable (AI strategy, operating model, sourcing case, roadmaps). Illustrations: AI pricing or licensing change that rewrites sourcing math; enforcement action under the EU AI Act that forces operating model changes; a large-enterprise disclosure of AI operating model structure, funding, or outcomes; a shift in the build-vs-buy frontier; consolidation among AI platform vendors CIOs actually buy from.
- 7-8 WORTH TRACKING. Meaningfully informs the advice the reader is giving a client but does not force an immediate advice change. Illustrations: material new AI capability from platforms; credible research on AI's impact on IT workforce, cost structures, or delivery models; AI governance, risk, or assurance frameworks from regulators or standards bodies; AI supply-chain partnerships with disclosed terms; AI sourcing benchmarks or TCO data from a credible source; significant AI security vulnerability or incident affecting enterprise deployments.
- 4-6 INCREMENTAL. Real AI signal but not decision-changing. Minor product updates, generic trend pieces, foundational research without a clear operating-model path, vendor announcements without pricing or availability, single-company anecdotes without structural lessons, confirmations of things already covered.
- 1-3 SKIP. Articles not primarily about AI; consumer AI features; developer/engineering tooling with no CIO-level implication; model benchmark leaderboards; sub-$100M funding rounds unless they reshape a category; opinion without new data; listicles; sponsored or promotional content; pure research previews; prompt-engineering tips; hype pieces; personnel moves without strategic consequence.

CALIBRATION DISCIPLINE: In a typical week of ~100 articles, expect roughly 0-3 at 9-10 and 8-20 at 7-8. If the distribution shifts a lot, recheck the gate step. The reader needs at least 4 articles at a score of 7 or higher to get a useful brief. When torn between two adjacent scores, pick the higher one."""


CLUSTERING_SYSTEM_PROMPT = """You are deduplicating a list of AI news articles. Group articles that cover the same underlying story, announcement, or event — even if covered by different sources.

Rules:
- Articles about the same product launch, partnership, or regulatory decision belong in one cluster
- Articles covering different aspects of a broad topic (e.g., "AI regulation" vs "EU AI Act vote") are separate clusters unless they directly reference the same event
- Each article must appear in exactly one cluster"""

EDITORIAL_SELECT_SYSTEM_PROMPT = """You are the editor of a weekly AI intelligence brief for a senior technology strategy consultant who advises CIOs and CTOs on IT strategy, operating models, sourcing, governance, and how the IT function absorbs AI. Select the 8-10 most worth reading this week. Prioritize stories that change advice to clients on operating model design, IT roadmaps, vendor/sourcing strategy, AI governance, or IT cost structures. Avoid duplicating themes — if two stories cover the same development, pick only the more informative one.

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
    articles: List[ScoredArticle],
    client: anthropic.Anthropic,
    dry_run: bool = False,
    verbose: bool = False,
) -> List[ScoredArticle]:
    """Claude picks the 4-6 best stories from scored+clustered candidates.
    Uses title + score + rationale as selection signal — no summarization needed yet."""
    if len(articles) <= 10:
        return articles

    if dry_run:
        if verbose:
            print(f"  [editorial_select] dry-run: keeping top 10 of {len(articles)}")
        return articles[:10]

    payload = [
        {
            "id": a.cluster_id,
            "title": a.title,
            "source": a.source_name,
            "score": a.relevance_score,
            "rationale": a.relevance_rationale,
        }
        for a in articles
    ]
    prompt = (
        f"From the {len(articles)} analyzed stories below, select the 8-10 most worth "
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
        return articles[:6]

    id_to_article = {a.cluster_id: a for a in articles}
    selected = [id_to_article[aid] for aid in selected_ids if aid in id_to_article]

    # Safety: pad to 8 if Claude returned fewer
    if len(selected) < 8:
        included_ids = {a.cluster_id for a in selected}
        for a in articles:
            if a.cluster_id not in included_ids:
                selected.append(a)
            if len(selected) >= 8:
                break

    if verbose:
        print(f"  [editorial_select] {len(articles)} → {len(selected)} articles selected")
    return selected


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
