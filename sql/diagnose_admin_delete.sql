-- Diagnostic: why does a delete report "The database accepted no deletions"?
--
-- Run this in the Supabase SQL Editor. Read-only — it changes nothing.
--
-- That message means the DELETE returned zero rows. Under RLS a DELETE with no matching
-- policy matches zero rows *without raising*, so "refused" and "already deleted" look
-- identical from the app. This file asks the database directly which one it was.
--
-- Read the sections in order; the first one that reports a problem is the cause.

-- ---------------------------------------------------------------------------
-- 1. Is there a DELETE policy at all, and what does it say?
-- ---------------------------------------------------------------------------
-- Expect exactly one DELETE policy per table, granted to {authenticated}, whose
-- using_expression mentions auth.uid() AND is_app_admin().
--
--   NO ROWS AT ALL       -> no DELETE policy exists. Nothing can ever be deleted, by
--                           anyone, admin or not. Run sql/add_ownership_delete_policies.sql.
--                           This is the most likely cause if your own entries also refuse
--                           to delete.
--   using_expression 'true' -> the old blanket policy. Deletes would WORK (too well) --
--                           so this is not your problem, but run the ownership migration.
--   mentions auth.uid()  -> the policy is right; go to section 2.
select
  tablename,
  policyname,
  roles,
  qual as using_expression,
  case
    when qual is null                      then 'CHECK -- no using expression'
    when qual = 'true'                     then 'OLD BLANKET POLICY -- run add_ownership_delete_policies.sql'
    when qual like '%is_app_admin%'        then 'OK -- ownership policy present'
    else                                        'UNEXPECTED -- read the expression'
  end as verdict
from pg_policies
where schemaname = 'public'
  and tablename in ('specimen_records', 'bioassay_results', 'clinical_case_data')
  and cmd = 'DELETE'
order by tablename;

-- If the query above returned NO ROWS for specimen_records, stop here: that is the bug.
-- Everything below assumes a policy exists.

-- ---------------------------------------------------------------------------
-- 2. Is RLS even switched on, and is the table there?
-- ---------------------------------------------------------------------------
-- rowsecurity = false with policies present means the policies are inert (everything is
-- permitted) -- which would not cause a refusal, but is worth knowing.
select relname as table_name,
       relrowsecurity as rls_enabled,
       relforcerowsecurity as rls_forced
from pg_class
where relnamespace = 'public'::regnamespace
  and relname in ('specimen_records', 'bioassay_results', 'clinical_case_data', 'app_admins')
order by relname;

-- ---------------------------------------------------------------------------
-- 3. Who is registered as an admin, and does that match a real account?
-- ---------------------------------------------------------------------------
-- The app decides whether to SHOW the controls by reading app_admins as you; the database
-- decides whether to ALLOW the delete by calling is_app_admin(). Both read the same table,
-- so a uid here that is not a real auth user means the app would offer the control to
-- nobody -- and a missing row means it offers it to nobody either.
--
-- Expect: one row per admin, matched = true, and your own email listed.
select
  a.user_id,
  u.email,
  (u.id is not null) as matches_a_real_account,
  a.note,
  a.added_at
from public.app_admins a
left join auth.users u on u.id = a.user_id
order by a.added_at;

-- Empty result = nobody is an admin. Add yourself:
--   insert into public.app_admins (user_id, note)
--   select id, email from auth.users where email = 'you@example.com'
--   on conflict (user_id) do nothing;

-- ---------------------------------------------------------------------------
-- 4. Does the ownership rule actually admit your account?
-- ---------------------------------------------------------------------------
-- Evaluates the real policy expression for each admin against the live rows, as that user.
-- This is the query that answers "would the delete have worked for me?" without deleting.
--
-- deletable_rows = 0 while total_rows > 0 is the failure you are chasing.
select
  a.user_id,
  u.email,
  (select count(*) from public.specimen_records) as total_rows,
  (
    select count(*)
    from public.specimen_records s
    where s.collector_id = a.user_id::text
       or exists (select 1 from public.app_admins x where x.user_id = a.user_id)
  ) as deletable_rows
from public.app_admins a
left join auth.users u on u.id = a.user_id
order by u.email;

-- ---------------------------------------------------------------------------
-- 5. Who actually owns the rows?
-- ---------------------------------------------------------------------------
-- collector_id is stamped at write time. 'unattributed-legacy' rows predate identity
-- tracking and are admin-only to delete. A collector_id that matches no auth user means
-- those rows are deletable only by an admin too.
select
  s.collector_id,
  count(*) as rows,
  (u.id is not null) as collector_is_a_real_account,
  u.email
from public.specimen_records s
left join auth.users u on u.id::text = s.collector_id
group by s.collector_id, u.id, u.email
order by rows desc;

-- ---------------------------------------------------------------------------
-- 6. The definitive test: delete as the authenticated role, then roll back.
-- ---------------------------------------------------------------------------
-- Everything above inspects metadata. This exercises the actual policy the way the app
-- does -- as `authenticated`, with a JWT claim, which is where auth.uid() reads from. The
-- SQL Editor otherwise runs as a superuser role that bypasses RLS entirely and would
-- succeed no matter how broken the policy is.
--
-- REPLACE the uuid below with your own (section 3 lists it), then run the whole block.
-- It ends in ROLLBACK: nothing is deleted.
begin;

  -- Stand up a row owned by nobody, so the test cannot destroy real data even if the
  -- rollback were skipped.
  -- collection_date is `date` and collector_id is NOT NULL with a non-blank CHECK
  -- (sql/enforce_collector_id.sql); 'unattributed-legacy' satisfies it and belongs to
  -- nobody, so only an admin should be able to delete this row.
  insert into public.specimen_records (specimen_id, collector_id, collection_date)
  values ('diag-probe-row', 'unattributed-legacy', current_date)
  on conflict (specimen_id) do nothing;

  set local role authenticated;
  -- <<< PUT YOUR UID HERE >>>
  set local request.jwt.claims = '{"sub":"00000000-0000-0000-0000-000000000000","role":"authenticated"}';

  select
    auth.uid()              as uid_the_database_sees,
    public.is_app_admin()   as database_says_i_am_admin;

  -- An admin should delete this ownerless row; a non-admin should not.
  with attempt as (
    delete from public.specimen_records
    where specimen_id = 'diag-probe-row'
    returning specimen_id
  )
  select
    count(*) as rows_deleted,
    case count(*)
      when 0 then 'REFUSED -- the policy does not admit this uid (see sections 1 and 3)'
      else        'ALLOWED -- the policy works; the app''s failure is elsewhere'
    end as verdict
  from attempt;

rollback;

-- If section 6 says ALLOWED but the app still reports "accepted no deletions", the
-- database is fine and the request is not arriving as this user -- check that the app is
-- sending the access token (utils/auth.py::get_supabase_client) rather than the anon key.
