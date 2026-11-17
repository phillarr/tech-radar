# Tech Radar

An autonomous technology intelligence agent. Every morning it collects news from about
45 official, media and research sources, asks Claude to turn them into a short
set of evidence-based events and signals, and posts the digest to Slack.

It covers AI product management, AI engineering, cloud and infrastructure,
cybersecurity, startups, research and AI governance.

![Daily digest in Slack: summary, priorities and the first cybersecurity events](docs/images/digest-top.png)

<details>
<summary>Full digest: all 10 events by category, signals and the evidence legend</summary>

![Full daily digest of 17 November 2026](docs/images/digest-full.png)

</details>

*Digest of 17 November 2026, as posted in Slack.*

## How it works

```
Supabase pg_cron (08:05 Europe/Paris) ──▶ GitHub Actions
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
- CRITICAL requires an urgency of at least 80 and an officially confirmed event,
  otherwise it is demoted to MAJOR;
- a signal needs at least two known items as evidence.

**Storage.** With Supabase configured, the agent keeps the history of articles,
analyses, events, signals and Slack distributions, tracks each source as ACTIVE,
DEGRADED or INACTIVE, and never re-posts an article already published in the last
14 days unless its content changed (for example an incident moving to resolved).
Storage is best effort: if Supabase is unavailable, the digest is still sent.

**Slack message.** Each event shows what happened, why it matters, a recommended action,
its primary source and its evidence status, spelled out for new readers: "✅ officially
confirmed" (stated by the company, project or authority itself) or "📰 press report, not
officially confirmed" (covered by the media only). A one-line legend at the bottom of the
digest and of the Weekly Radar repeats these definitions.

> **AI-generated content.** The text of each event ("What happened", "Why it matters",
> "Recommended action" and, when present, "Product opportunity"), as well as the
> priority, the signals and the Weekly Radar synthesis, is written by Claude and can be
> wrong or incomplete. Links, source names, dates and evidence rules are checked in code,
> but the wording and the judgment are not. Read the linked source before acting on a
> recommendation.

**Weekly Radar.** Every Monday morning, a second workflow reads back the events of the
daily digests posted the previous week (Monday to Sunday, Paris time) and asks Claude for
a short synthesis: the top stories, the themes that connect several of them, and a
"Worth reading" section picked from analysis outlets listed in `reading_feeds` (Harvard
Business Review by default), clearly separated from the news. Claude may only reference
stored events and reading-feed links; counts, titles, links and evidence statuses come
from storage. The Weekly Radar requires Supabase.

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

### 2. Database and scheduler

Create a Supabase project, open the SQL Editor and run [`db/schema.sql`](db/schema.sql)
once. Row Level Security is enabled on every table with no policy, so only the secret
key can read or write. The daily digest works without a database, but cross-day
deduplication, the Weekly Radar and the scheduler need it.

The workflows have no GitHub cron, because GitHub delays or drops scheduled runs (in
practice by several hours, or not at all, on this repository). Run [`db/scheduler.sql`](db/scheduler.sql): Supabase
(`pg_cron` + `pg_net`) then starts the daily workflow at 07:05 UTC and the Weekly Radar
on Mondays at 07:45 UTC through the GitHub API. It needs a fine-grained GitHub token
limited to this repository with the "Actions: Read and write" permission, stored in the
Supabase Vault as `github_dispatch_token` (instructions at the top of the file).

- The token expires (one year at most): renew it before then and replace the Vault
  secret with `vault.update_secret`, otherwise the digests stop.
- If no digest arrives one morning, check GitHub's answers to the scheduler in the SQL
  Editor (204 means the run was accepted):

  ```sql
  select status_code, content, created from net._http_response order by created desc limit 5;
  select jobname, schedule, active from cron.job;
  ```

### 3. Sources and settings

Everything is configured in [`config/sources.json`](config/sources.json):

- `settings`: lookback window, items per source, Claude model and effort, maximum number
  of events, display timezone, and the `audience` description the analysis is written for;
- `sources.github_repositories`: repositories whose latest release is tracked;
- `sources.cisa_kev_url`: CISA's [Known Exploited Vulnerabilities](https://www.cisa.gov/known-exploited-vulnerabilities-catalog)
  catalog: vulnerabilities with evidence of active exploitation (an official source). It is
  read from CISA's own GitHub repository (`cisagov/kev-data`) because cisa.gov refuses
  requests from GitHub Actions. Entries added since the previous day are analyzed, each
  linked to its NVD page. Remove the key to disable it;
- `sources.rss_feeds`: feeds with a `type` of `OFFICIAL`, `MEDIA`, `RESEARCH` or
  `COMMUNITY`, which drives source attribution and evidence status;
- `sources.reading_feeds`: analysis and commentary feeds, used only by the Weekly Radar.

## Running

The workflow [`.github/workflows/daily-watch.yml`](.github/workflows/daily-watch.yml)
runs every day around 08:00 Europe/Paris. It is started by Supabase (`db/scheduler.sql`,
with `scheduled=true`) rather than GitHub's cron, which can delay or drop scheduled runs.
A scheduled run only posts between 08:00 and 11:59 Paris time, and stops early when
Supabase shows today's digest was already posted. It can also be started by hand from the Actions tab or with:

```bash
gh workflow run daily-watch.yml -f dry_run=true   # print the Slack payload in the logs
gh workflow run daily-watch.yml                   # post to Slack
```

A dry run never marks articles as published.

The Weekly Radar runs from [`.github/workflows/weekly-radar.yml`](.github/workflows/weekly-radar.yml)
on Mondays around 08:45 Paris time, also started by Supabase. A manual run covers the
last seven days up to now and is a dry run by default:

```bash
gh workflow run weekly-radar.yml                    # print the payload in the logs
gh workflow run weekly-radar.yml -f dry_run=false   # post to Slack
```

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
db/scheduler.sql           Supabase scheduler (pg_cron) that starts the workflows
src/config_loader.py       configuration and secrets (Pydantic)
src/fetcher.py             RSS and GitHub ingestion, per-source health
src/analyzer.py            prompt, output contract, Claude call, validation rules
src/notifier.py            Slack message
src/storage.py             Supabase client and cross-day deduplication
src/weekly.py              Weekly Radar
src/main.py                pipeline
tests/                     unit tests
```

## Roadmap

- Collection every 6 hours, with the morning digest built from the accumulated articles
- Reader feedback (👍 ➖ ⚠️) through a Slack app
- Watchlists for topics to follow closely
