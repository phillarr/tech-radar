# Tech Radar

An autonomous technology intelligence agent. Every morning it collects news from about
45 official, media and research sources, asks Claude to turn them into a short
set of evidence-based events and signals, and posts the digest to Slack.

It covers AI product management, AI engineering, cloud and infrastructure,
cybersecurity, startups, research and AI governance.

## How it works

```
GitHub Actions (08:00 Europe/Paris)
  │
  ├─ fetch      RSS feeds + GitHub releases from the last 24 h      src/fetcher.py
  ├─ store      sources health and articles in Supabase (optional)  src/storage.py
  ├─ dedupe     skip articles already published unchanged
  ├─ analyze    Claude groups items into events, scores them        src/analyzer.py
  ├─ validate   deterministic rules on the model output             src/analyzer.py
  ├─ notify     Slack Block Kit message via Incoming Webhook        src/notifier.py
  └─ store      analysis, events, signals, distribution, statuses   src/storage.py
```

**Intelligence Engine.** Claude returns a strict JSON contract (structured outputs):
events with a category, an event type, facts, why it matters, recommended actions,
an optional product opportunity, three separate scores (relevance, impact, urgency),
a priority (CRITICAL / MAJOR / INFO) and an evidence status (CONFIRMED / REPORTED /
UNVERIFIED). Fetched content is treated as untrusted data.

**Deterministic validation.** Whatever can be checked without judgment is enforced in
code, not left to the model:

- every source URL must come from the ingested items, and belongs to one event only;
- source names and types come from the configuration, not from the model;
- exactly one PRIMARY source per event, an official one when available;
- CONFIRMED requires an official source, otherwise the event is REPORTED;
- UNVERIFIED events (thin or contradictory claims) are left out of the digest;
- CRITICAL requires an urgency of at least 80, otherwise it is demoted to MAJOR;
- a signal needs at least two known items as evidence.

**Storage.** With Supabase configured, the agent keeps the history of articles,
analyses, events, signals and Slack distributions, tracks each source as ACTIVE,
DEGRADED or INACTIVE, and never re-posts an article already published in the last
14 days unless its content changed (for example an incident moving to resolved).
Storage is best effort: if Supabase is unavailable, the digest is still sent.

## Setup

### 1. Secrets

Add these repository secrets (Settings → Secrets and variables → Actions):

| Secret | Required | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | yes | Claude API key |
| `SLACK_WEBHOOK_URL` | yes | Slack Incoming Webhook of the target channel |
| `SUPABASE_URL` | no | Supabase project URL (`https://<ref>.supabase.co`) |
| `SUPABASE_SERVICE_KEY` | no | Supabase secret key (`sb_secret_…`) or legacy `service_role` key |

`GITHUB_TOKEN` is provided by GitHub Actions and raises the GitHub API rate limit.

### 2. Database (optional)

Create a Supabase project, open the SQL Editor and run [`db/schema.sql`](db/schema.sql)
once. Row Level Security is enabled on every table with no policy, so only the secret
key can read or write.

### 3. Sources and settings

Everything is configured in [`config/sources.json`](config/sources.json):

- `settings`: lookback window, items per source, Claude model and effort, maximum number
  of events, display timezone, and the `audience` description the analysis is written for;
- `sources.github_repositories`: repositories whose latest release is tracked;
- `sources.rss_feeds`: feeds with a `type` of `OFFICIAL`, `MEDIA`, `RESEARCH` or
  `COMMUNITY`, which drives source attribution and evidence status.

## Running

The workflow [`.github/workflows/daily-watch.yml`](.github/workflows/daily-watch.yml)
runs every day around 08:00 Europe/Paris. GitHub can delay or drop scheduled runs, so
several crons fire between 08:00 and 11:59 Paris time (whatever the daylight saving
period); the first one posts the digest and the others stop early when Supabase shows it
was already posted today. It can also be started by hand from the Actions tab or with:

```bash
gh workflow run daily-watch.yml -f dry_run=true   # print the Slack payload in the logs
gh workflow run daily-watch.yml                   # post to Slack
```

A dry run never marks articles as published.

### Locally

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                # then fill in the values
DRY_RUN=true python -m src.main
```

### Tests

```bash
python -m unittest discover tests
```

The tests run without network access and without calling Claude. They replay mistakes
seen in real digests (duplicate events, media cited instead of the official source,
over-ranked resolved incidents) and check deduplication, priorities, Slack rendering
and the rows written to Supabase. They also run in CI before each digest.

## Project layout

```
config/sources.json        sources and settings
db/schema.sql              Supabase schema
src/config_loader.py       configuration and secrets (Pydantic)
src/fetcher.py             RSS and GitHub ingestion, per-source health
src/analyzer.py            prompt, output contract, Claude call, validation rules
src/notifier.py            Slack message
src/storage.py             Supabase client and cross-day deduplication
src/main.py                pipeline
tests/                     unit tests
```

## Roadmap

- Collection every 6 hours, with the morning digest built from the accumulated articles
- Weekly Radar on Mondays, built from the stored history, with a separate "Worth reading"
  section for analysis sources such as Harvard Business Review
  (`http://feeds.hbr.org/harvardbusiness`), kept out of the daily digest because they
  publish commentary rather than news
- Reader feedback (👍 ➖ ⚠️) through a Slack app
- Watchlists for topics to follow closely
