-- ════════════════════════════════════════════════════════════════
-- AgroVest Pro — Migration: Investment Plan Quota & Badges (Phase 5)
-- ════════════════════════════════════════════════════════════════
--
-- SAFE TO RUN: this migration only ADDS columns, it never drops,
-- renames, or modifies existing data. Every existing plan keeps
-- working exactly as it does today, with zero manual updates needed.
--
-- What each column does, and its safe default:
--
--   max_investors  (integer, nullable, no default)
--     NULL = unlimited (the automatic value for every existing plan
--     the moment this column is added — Postgres backfills NULL for
--     any column added without a DEFAULT). No quota badge, no
--     progress bar, no "sold out" state, and no extra database
--     query is run for these plans — verified directly against the
--     application code before shipping this migration.
--
--   is_popular     (boolean, NOT NULL, default FALSE)
--   is_featured    (boolean, NOT NULL, default FALSE)
--     FALSE = no badge shown. Postgres backfills FALSE into every
--     existing row automatically when a NOT NULL column is added
--     with a DEFAULT, so this is also a no-op for current plans.
--
-- After running this, nothing changes on your live site until you
-- deliberately open a plan in the admin panel and set one of these
-- fields — existing plans render identically to how they do now.
--
-- Idempotent: safe to run more than once (IF NOT EXISTS on every line).
-- ════════════════════════════════════════════════════════════════

ALTER TABLE public.plans ADD COLUMN IF NOT EXISTS max_investors INTEGER;
ALTER TABLE public.plans ADD COLUMN IF NOT EXISTS is_popular BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE public.plans ADD COLUMN IF NOT EXISTS is_featured BOOLEAN NOT NULL DEFAULT FALSE;

-- Sanity check: confirms the columns exist and shows you that every
-- existing plan came through with the expected safe defaults.
SELECT id, name, max_investors, is_popular, is_featured
FROM public.plans
ORDER BY id;

SELECT 'Phase 5 plan quota/badge migration applied successfully!' AS status;
