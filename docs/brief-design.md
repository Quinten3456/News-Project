# AI Intelligence Brief — System Design

> This document describes the design of the weekly AI Intelligence Brief pipeline.
> Edit this file to evolve the design as the project grows.

## Purpose

An automated pipeline that collects AI news weekly, filters for strategic relevance using Claude, and produces a consultant-style brief for technology strategists.

**Core principle:** Value lies in deciding what is worth knowing. Content quality and strategic relevance is the primary filter. Volume stays low.

---

## Source Tiers

| Tier | Sources | Inclusion Rule |
|------|---------|----------------|
| 1 | OpenAI, Google DeepMind, Anthropic, Microsoft AI | Include if relevant (score ≥ 6) |
| 2 | TechCrunch AI, VentureBeat AI, McKinsey AI, Reuters/DeepSeek | Include only if high impact (score ≥ 8) |
| 3 | AI Report podcast (YouTube) | Always included as a separate section |

Tiers act as **filtering thresholds**. Primary scoring criterion: *"Is this worth knowing to support better decision-making and client conversations for a technology strategist?"*

Cross-source overlap is a secondary signal only (+1 score boost). It does not override content quality as the primary filter.

---

## Repository Structure

```
ClaudeProjects/
├── scripts/
│   ├── fetch_transcript.py     (existing — YouTube transcript fetcher)
│   ├── collect.py              (RSS + scraping for all Tier 1+2 sources)
│   ├── filter_score.py         (Claude relevance scoring + tier filtering)
│   ├── summarize.py            (Claude strategic summaries)
│   ├── compile_brief.py        (Markdown + email text assembly)
│   └── generate_brief.py       (orchestrator — single entry point)
├── config/
│   └── sources.yaml            (source definitions, tiers, RSS URLs, thresholds)
├── briefs/                     (weekly output: .md + _email.txt)
├── transcripts/                (podcast transcripts)
├── cache/
│   └── seen_articles.json      (deduplication state across weeks)
└── docs/
    └── brief-design.md         (this file)
```

---

## Running the Pipeline

```bash
# Full run with podcast
python scripts/generate_brief.py --podcast-url https://www.youtube.com/watch?v=VIDEO_ID

# Full run without podcast
python scripts/generate_brief.py

# Test collection without Claude API calls
python scripts/generate_brief.py --dry-run --verbose

# Iterate on scoring/summarization without re-fetching sources
python scripts/generate_brief.py --skip-collect --podcast-url URL
```

**Required:** `ANTHROPIC_API_KEY` environment variable.

---

## Brief Output Format

```
# AI Intelligence Brief — Week of {date}
> {N} sources | {M} collected | {K} included

## Top Stories
### 1. {headline}
*{source} | Tier {tier} | {date}*
**What happened:** ...
**Why it matters:** ...
**Strategic implication:** ...

## Podcast Intelligence — AI Report
- **{topic}:** {what was discussed} {why it matters}
**This week's takeaway:** ...

## Sources Monitored
| Source | Tier | Fetched | Included |
```

Also outputs `_email.txt` (plain text, ALL CAPS section labels, paste-ready for Gmail/Outlook).

---

## Claude API Usage

~18 calls, ~29,500 tokens per weekly run. Under $1 at Claude Sonnet pricing.

| Step | Calls | Tokens |
|------|-------|--------|
| Scoring batches (10 articles each) | 3 | 6,000 |
| Clustering | 1 | 1,500 |
| Article summaries (12 items) | 12 | 18,000 |
| Podcast summary (1) | 1 | 3,000 |
| **Total** | **17** | **~28,500** |

---

## Automation

Runs every Friday at 8:00 AM via Windows Task Scheduler (`AIBrief` task).
Podcast URL must be provided manually each week via `--podcast-url`.

---

## Future Nice-to-Haves (not in MVP)

- **Watchlist system**: Standing topics (e.g., "EU AI Act", "agentic AI") that always surface
- **Context delta**: How this week's themes compare to last week
- **Automatic YouTube channel discovery**: No more manual URL input
- **HTML email rendering**: Formatted output with styling
