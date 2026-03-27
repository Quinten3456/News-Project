# Scoring Redesign & Source Fixes — Design Spec

**Date:** 2026-03-27
**Goal:** Limit the weekly AI Intelligence Brief to 4–6 stories (plus podcast), and fix broken/stale news sources.

---

## Problem

1. **Too many articles pass filtering.** Current Tier 1 threshold (score ≥ 6) is too permissive. In a typical week, 20+ articles pass, producing a brief that takes far longer than 5 minutes to read.
2. **No hard editorial cap.** `max_final_articles: 12` in `sources.yaml` is defined but never read or enforced anywhere.
3. **Dry-run scores are unrealistic.** Mock scores assign `7` to all Tier 1 articles, making every Tier 1 article pass — inflating the dry-run output and making it useless for testing the filter.
4. **Three sources return 0 articles:**
   - **Reuters**: RSS URL (`feeds.reuters.com/reuters/technologyNews`) is deprecated and dead.
   - **Microsoft AI Blog**: `fetch_rss_with_fallback` only falls back to scraping when RSS returns zero entries. When RSS has entries that are all older than 7 days, the fallback never triggers.
   - **VentureBeat**: RSS returns stale articles (all older than freshness window); no fallback configured.

---

## Design

### 1. Raise Tier 1 score threshold: 6 → 8

All sources (Tier 1 and Tier 2) now require a score ≥ 8 to pass the initial filter.

The scoring scale defines:
- 9–10: Changes competitive dynamics significantly
- 7–8: Worth tracking — meaningful new capability or finding
- 5–6: Incremental — minor update, no new data
- 1–4: Skip

A threshold of 8 means only genuinely impactful stories from any source enter the pipeline. This is the primary filter; the editorial pass is the final gate.

**Change:** `config/sources.yaml` — set `score_threshold: 8` on all Tier 1 sources.
**Change:** Remove dead `max_final_articles` setting from `sources.yaml`.

### 2. Add editorial selection step (new)

After scoring + clustering, a new `editorial_select()` function in `filter_score.py` asks Claude to pick the best 4–6 stories from all passing clustered articles.

**Prompt framing:** "You are the editor of a weekly AI intelligence brief for technology strategists. From the following N stories that passed relevance scoring, select the 4–6 that are most worth reading this week. Return their IDs in ranked order."

**Behavior:**
- If fewer than 4 stories pass scoring, all are included (no artificial padding).
- If more than 6 pass, Claude selects the best 6.
- In dry-run mode, mock implementation returns the top 6 by mock score (no API call needed).

**Change:** `filter_score.py` — add `editorial_select(articles, client, dry_run)` called at the end of `score_and_filter()`, returns a capped ranked list.
**Change:** `generate_brief.py` — no changes needed; `score_and_filter()` already returns the final list.

### 3. Fix dry-run mock scores

Current mock assigns `score=7` to all Tier 1 and `score=6` to all Tier 2 — every Tier 1 article passes and no Tier 2 article passes, regardless of content.

**Fix:** Mock scores should vary realistically: a mix of 6–9 for Tier 1, 5–8 for Tier 2, distributed pseudo-randomly (e.g. based on article index) so the dry-run exercises the filter and editorial select properly.

**Change:** `filter_score.py` — update the `if dry_run` branch in `score_batch()`.

### 4. Fix broken/stale sources

**Reuters** — replace dead RSS URL with NYT Technology RSS (already noted as an option in `sources.yaml` comments):
`https://rss.nytimes.com/services/xml/rss/nyt/Technology.xml`

**Microsoft AI Blog** — fix `fetch_rss_with_fallback` fallback trigger: fall back to scraping not only when `feed.entries` is empty, but also when entries exist but none pass the freshness window.

**VentureBeat** — add `fallback_url` pointing to `https://venturebeat.com/category/ai/` and switch method to `rss_with_fallback` so the scrape fallback is available when RSS is stale.

---

## Files Changed

| File | Change |
|------|--------|
| `config/sources.yaml` | Tier 1 thresholds 6 → 8; remove `max_final_articles`; Reuters URL replaced; VentureBeat gets fallback URL + method |
| `scripts/filter_score.py` | Add `editorial_select()`; fix dry-run mock scores; call editorial select at end of `score_and_filter()` |
| `scripts/collect.py` | Fix `fetch_rss_with_fallback` to fall back when entries exist but all are stale |

---

## Out of Scope

- Podcast summarization format (unchanged)
- `compile_brief.py` and `summarize.py` (unchanged)
- Watchlist / context delta features (future)
