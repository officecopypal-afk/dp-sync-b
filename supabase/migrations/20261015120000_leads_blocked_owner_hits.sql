-- 2026-10-15: ślad po ogłoszeniach zablokowanych ogłoszeniodawców (skarga 13.10.2026).
-- Zbieracz Otodom pomija je przed ingestem; tu zapisuje tylko, że się pojawiły,
-- żeby zadanie-strażnik mogło dać znać. Bez nazwiska i treści ogłoszenia.
-- Jeden wiersz na ogłoszenie; kolejne przebiegi podbijają last_seen_at i seen_count.

create table if not exists leads.blocked_owner_hits (
  id bigserial primary key,
  portal text not null default 'otodom',
  portal_listing_id text not null,
  url text,
  owner_id text,
  match text not null check (match in ('konto', 'nazwisko')),
  phone_normalized text,
  job_name text,
  first_seen_at timestamptz not null default now(),
  last_seen_at timestamptz not null default now(),
  seen_count integer not null default 1,
  unique (portal, portal_listing_id)
);

alter table leads.blocked_owner_hits enable row level security;

create or replace function public.leads_log_blocked_owner_hit(p_hit jsonb)
returns void
language sql
security definer
set search_path = leads, public
as $$
  insert into leads.blocked_owner_hits as h
    (portal, portal_listing_id, url, owner_id, match, phone_normalized, job_name)
  values (
    coalesce(p_hit->>'portal', 'otodom'),
    p_hit->>'portal_listing_id',
    p_hit->>'url',
    p_hit->>'owner_id',
    p_hit->>'match',
    p_hit->>'phone',
    p_hit->>'job_name'
  )
  on conflict (portal, portal_listing_id) do update
    set last_seen_at = now(),
        seen_count = h.seen_count + 1,
        url = coalesce(excluded.url, h.url),
        phone_normalized = coalesce(excluded.phone_normalized, h.phone_normalized);
$$;

revoke all on function public.leads_log_blocked_owner_hit(jsonb) from public, anon, authenticated;
grant execute on function public.leads_log_blocked_owner_hit(jsonb) to service_role;
