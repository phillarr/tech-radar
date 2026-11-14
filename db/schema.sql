-- Technology Intelligence Agent: storage schema (spec section "Storage").
-- Paste once into the Supabase SQL Editor and run. Safe to re-run.
--
-- Row Level Security is enabled on every table with no policy: only the service key used by
-- the agent (GitHub secret SUPABASE_SERVICE_KEY) can read or write. The public/anon key
-- has no access.

-- Sources: one row per RSS feed or GitHub repository, with its health status.
create table if not exists sources (
  name                 text primary key,
  url                  text not null,
  kind                 text not null check (kind in ('rss', 'github')),
  type                 text not null check (type in ('OFFICIAL', 'MEDIA', 'RESEARCH', 'COMMUNITY')),
  status               text not null default 'ACTIVE'
                       check (status in ('ACTIVE', 'DEGRADED', 'INACTIVE')),
  consecutive_failures integer not null default 0,
  last_checked_at      timestamptz,
  last_success_at      timestamptz,
  last_item_at         timestamptz,          -- cursor: newest item seen from this source
  last_error           text,
  created_at           timestamptz not null default now()
);

-- Articles: every ingested item, keyed by URL.
create table if not exists articles (
  id                  bigint generated always as identity primary key,
  url                 text not null unique,
  source_name         text not null,
  source_type         text not null,
  title               text not null,
  published_at        timestamptz,
  content             text not null default '',
  content_hash        text not null,         -- detects substantial updates of the same URL
  status              text not null default 'DISCOVERED'
                      check (status in ('DISCOVERED', 'ANALYZED', 'PUBLISHED', 'REJECTED',
                                        'FAILED', 'REQUIRES_REVIEW')),
  first_seen_at       timestamptz not null default now(),
  last_seen_at        timestamptz not null default now(),
  last_published_hash text,                  -- content_hash when it was last published
  last_published_at   timestamptz,
  last_event_id       text
);
create index if not exists articles_last_published_at_idx on articles (last_published_at);
create index if not exists articles_published_at_idx on articles (published_at);

-- Analyses: one row per Intelligence Engine run (model call).
create table if not exists analyses (
  id             uuid primary key default gen_random_uuid(),
  created_at     timestamptz not null default now(),
  model          text not null,
  prompt_version text not null,
  stop_reason    text,
  input_tokens   integer,
  output_tokens  integer,
  items_in       integer not null,
  events_out     integer not null,
  signals_out    integer not null,
  dry_run        boolean not null
);

-- Events: the validated events of each analysis.
create table if not exists events (
  id                   bigint generated always as identity primary key,
  analysis_id          uuid not null references analyses (id) on delete cascade,
  event_id             text not null,         -- evt_YYYYMMDD_NNN, unique within an analysis
  title                text not null,
  category             text not null,
  secondary_categories text[] not null default '{}',
  event_type           text not null,
  priority             text not null check (priority in ('CRITICAL', 'MAJOR', 'INFO')),
  evidence_status      text not null check (evidence_status in ('CONFIRMED', 'REPORTED', 'UNVERIFIED')),
  source_type          text not null,
  primary_url          text not null,
  relevance            integer,
  impact               integer,
  urgency              integer,
  payload              jsonb not null,        -- full event as produced by the pipeline
  created_at           timestamptz not null default now(),
  unique (analysis_id, event_id)
);
create index if not exists events_created_at_idx on events (created_at);

-- Signals: cross-event signals of each analysis.
create table if not exists signals (
  id             bigint generated always as identity primary key,
  analysis_id    uuid not null references analyses (id) on delete cascade,
  signal         text not null,
  evidence_level text not null check (evidence_level in ('WEAK_SIGNAL', 'CORROBORATED')),
  evidence_urls  text[] not null,
  interpretation text not null,
  what_to_watch  text not null,
  created_at     timestamptz not null default now()
);

-- Distributions: what was sent where.
create table if not exists distributions (
  id          bigint generated always as identity primary key,
  analysis_id uuid not null references analyses (id) on delete cascade,
  channel     text not null default 'slack',
  status      text not null check (status in ('POSTED', 'DRY_RUN', 'FAILED')),
  event_ids   text[] not null default '{}',
  created_at  timestamptz not null default now()
);

-- Feedback: reader reactions on events (needs a Slack bot; not wired yet).
create table if not exists feedback (
  id         bigint generated always as identity primary key,
  event_ref  bigint not null references events (id) on delete cascade,
  reaction   text not null check (reaction in ('USEFUL', 'NEUTRAL', 'WRONG')),
  slack_user text,
  created_at timestamptz not null default now()
);

-- Watchlists: topics to follow closely (not wired yet).
create table if not exists watchlists (
  id         bigint generated always as identity primary key,
  name       text not null unique,
  keywords   text[] not null default '{}',
  categories text[] not null default '{}',
  active     boolean not null default true,
  created_at timestamptz not null default now()
);

alter table sources       enable row level security;
alter table articles      enable row level security;
alter table analyses      enable row level security;
alter table events        enable row level security;
alter table signals       enable row level security;
alter table distributions enable row level security;
alter table feedback      enable row level security;
alter table watchlists    enable row level security;
