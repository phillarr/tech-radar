-- Reliable scheduler: Supabase starts the GitHub workflows every morning.
--
-- GitHub's own cron is best effort and can delay or skip runs, so the workflows have no
-- cron of their own: pg_cron calls the GitHub API (workflow_dispatch) at a fixed time.
-- The agent posts at most one digest per day and one radar per week.
--
-- Before running this file, store a fine-grained GitHub token (repository tech-radar
-- only, permission "Actions: Read and write") in the Vault, once, in a separate query:
--
--   select vault.create_secret('<paste the token here>', 'github_dispatch_token');
--
-- To replace it later:
--
--   select vault.update_secret(id, '<new token>')
--   from vault.secrets where name = 'github_dispatch_token';
--
-- This file can be run again safely: the jobs are replaced, not duplicated.

create extension if not exists pg_cron;
create extension if not exists pg_net;

create or replace function public.dispatch_workflow(workflow text, inputs jsonb)
returns bigint
language plpgsql
security definer
set search_path = ''
as $$
declare
  token text;
begin
  select decrypted_secret into token
  from vault.decrypted_secrets
  where name = 'github_dispatch_token';
  if token is null then
    raise exception 'Vault secret github_dispatch_token is missing';
  end if;
  return net.http_post(
    url := 'https://api.github.com/repos/phillarr/tech-radar/actions/workflows/'
           || workflow || '/dispatches',
    body := jsonb_build_object('ref', 'main', 'inputs', inputs),
    headers := jsonb_build_object(
      'Authorization', 'Bearer ' || token,
      'Accept', 'application/vnd.github+json',
      'X-GitHub-Api-Version', '2022-11-28',
      'User-Agent', 'tech-radar-scheduler',
      'Content-Type', 'application/json'
    ),
    timeout_milliseconds := 10000
  );
end;
$$;

-- Only the scheduler (postgres role) may call it, never the public API.
revoke all on function public.dispatch_workflow(text, jsonb) from public, anon, authenticated;

-- pg_cron uses UTC: 07:05 UTC is 08:05 in Paris in winter and 09:05 in summer, inside
-- the workflows' 08:00-11:59 window either way.
select cron.schedule(
  'tech-radar-daily', '5 7 * * *',
  $job$select public.dispatch_workflow('daily-watch.yml', '{"scheduled": "true"}')$job$
);

-- Mondays, after the daily digest.
select cron.schedule(
  'tech-radar-weekly', '45 7 * * 1',
  $job$select public.dispatch_workflow('weekly-radar.yml',
                                       '{"scheduled": "true", "dry_run": "false"}')$job$
);

-- Checks:
--   select jobname, schedule, active from cron.job;
--   select status, return_message, start_time from cron.job_run_details
--   order by start_time desc limit 5;
--   select status_code, content, created from net._http_response
--   order by created desc limit 5;   -- 204 means GitHub accepted the request
