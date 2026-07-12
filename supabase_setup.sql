-- ════════════════════════════════════════════
-- AgroVest Pro — Supabase Table Setup
-- Run this ONCE in: Supabase Dashboard → SQL Editor → New Query
-- ════════════════════════════════════════════

-- 1. USERS
CREATE TABLE IF NOT EXISTS public.users (
    id                BIGSERIAL PRIMARY KEY,
    full_name         TEXT NOT NULL,
    email             TEXT UNIQUE NOT NULL,
    phone             TEXT,
    password_hash     TEXT NOT NULL,
    referral_code     TEXT UNIQUE NOT NULL,
    referred_by       BIGINT REFERENCES public.users(id) ON DELETE SET NULL,
    balance           NUMERIC(15,2) DEFAULT 0,
    total_invested    NUMERIC(15,2) DEFAULT 0,
    total_earnings    NUMERIC(15,2) DEFAULT 0,
    referral_earnings NUMERIC(15,2) DEFAULT 0,
    is_admin          BOOLEAN DEFAULT FALSE,
    is_active         BOOLEAN DEFAULT TRUE,
    created_at        TIMESTAMPTZ DEFAULT NOW()
);

-- 2. PLANS
CREATE TABLE IF NOT EXISTS public.plans (
    id            BIGSERIAL PRIMARY KEY,
    name          TEXT NOT NULL,
    slug          TEXT UNIQUE NOT NULL,
    icon          TEXT DEFAULT '🌱',
    image_filename TEXT,
    description   TEXT,
    min_amount    NUMERIC(15,2) NOT NULL DEFAULT 10000,
    max_amount    NUMERIC(15,2),
    roi_percent   NUMERIC(5,2) NOT NULL DEFAULT 10,
    total_return  NUMERIC(6,2),
    duration_days INTEGER NOT NULL DEFAULT 30,
    features      TEXT DEFAULT '',
    is_active     BOOLEAN DEFAULT TRUE,
    sort_order    INTEGER DEFAULT 0,
    created_at    TIMESTAMPTZ DEFAULT NOW()
);

-- Migration for existing databases that already had a plans table before
-- image/total_return were added — safe to re-run, does nothing if these
-- columns already exist.
ALTER TABLE public.plans ADD COLUMN IF NOT EXISTS image_filename TEXT;
ALTER TABLE public.plans ADD COLUMN IF NOT EXISTS total_return NUMERIC(6,2);

-- 3. INVESTMENTS
CREATE TABLE IF NOT EXISTS public.investments (
    id              BIGSERIAL PRIMARY KEY,
    user_id         BIGINT NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    plan_id         BIGINT REFERENCES public.plans(id) ON DELETE SET NULL,
    plan_name       TEXT NOT NULL,
    amount          NUMERIC(15,2) NOT NULL,
    roi_percent     NUMERIC(5,2) NOT NULL,
    expected_return NUMERIC(15,2) NOT NULL,
    duration_days   INTEGER NOT NULL,
    start_date      TIMESTAMPTZ DEFAULT NOW(),
    end_date        TIMESTAMPTZ NOT NULL,
    status          TEXT DEFAULT 'active',
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- 4. DEPOSITS
CREATE TABLE IF NOT EXISTS public.deposits (
    id               BIGSERIAL PRIMARY KEY,
    user_id          BIGINT NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    amount           NUMERIC(15,2) NOT NULL,
    payment_method   TEXT NOT NULL,
    proof_filename   TEXT,
    reference        TEXT UNIQUE NOT NULL,
    status           TEXT DEFAULT 'pending',
    admin_note       TEXT,
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    updated_at       TIMESTAMPTZ DEFAULT NOW()
);

-- 5. WITHDRAWALS
CREATE TABLE IF NOT EXISTS public.withdrawals (
    id             BIGSERIAL PRIMARY KEY,
    user_id        BIGINT NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    amount         NUMERIC(15,2) NOT NULL,
    bank_name      TEXT NOT NULL,
    account_number TEXT NOT NULL,
    account_name   TEXT NOT NULL,
    status         TEXT DEFAULT 'pending',
    admin_note     TEXT,
    created_at     TIMESTAMPTZ DEFAULT NOW(),
    updated_at     TIMESTAMPTZ DEFAULT NOW()
);

-- 6. REFERRALS
CREATE TABLE IF NOT EXISTS public.referrals (
    id          BIGSERIAL PRIMARY KEY,
    referrer_id BIGINT NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    referred_id BIGINT NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    commission  NUMERIC(15,2) DEFAULT 0,
    status      TEXT DEFAULT 'pending',
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- 7. NOTIFICATIONS
CREATE TABLE IF NOT EXISTS public.notifications (
    id         BIGSERIAL PRIMARY KEY,
    user_id    BIGINT NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    message    TEXT NOT NULL,
    type       TEXT DEFAULT 'info',
    is_read    BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ── Disable Row Level Security (backend uses service_role key) ──
ALTER TABLE public.users         DISABLE ROW LEVEL SECURITY;
ALTER TABLE public.plans         DISABLE ROW LEVEL SECURITY;
ALTER TABLE public.investments   DISABLE ROW LEVEL SECURITY;
ALTER TABLE public.deposits      DISABLE ROW LEVEL SECURITY;
ALTER TABLE public.withdrawals   DISABLE ROW LEVEL SECURITY;
ALTER TABLE public.referrals     DISABLE ROW LEVEL SECURITY;
ALTER TABLE public.notifications DISABLE ROW LEVEL SECURITY;

-- ════════════════════════════════════════════
-- Indexes — every column the app filters or sorts by gets one.
-- Without these, every dashboard/admin list does a full table scan
-- that gets slower as the platform grows.
-- ════════════════════════════════════════════
CREATE INDEX IF NOT EXISTS idx_users_email          ON public.users(email);
CREATE INDEX IF NOT EXISTS idx_users_referral_code  ON public.users(referral_code);
CREATE INDEX IF NOT EXISTS idx_users_referred_by    ON public.users(referred_by);

CREATE INDEX IF NOT EXISTS idx_investments_user_id  ON public.investments(user_id);
CREATE INDEX IF NOT EXISTS idx_investments_status   ON public.investments(status);
CREATE INDEX IF NOT EXISTS idx_investments_end_date ON public.investments(end_date);

CREATE INDEX IF NOT EXISTS idx_deposits_user_id     ON public.deposits(user_id);
CREATE INDEX IF NOT EXISTS idx_deposits_status      ON public.deposits(status);
CREATE INDEX IF NOT EXISTS idx_deposits_reference    ON public.deposits(reference);

CREATE INDEX IF NOT EXISTS idx_withdrawals_user_id  ON public.withdrawals(user_id);
CREATE INDEX IF NOT EXISTS idx_withdrawals_status   ON public.withdrawals(status);

CREATE INDEX IF NOT EXISTS idx_referrals_referrer_id ON public.referrals(referrer_id);
CREATE INDEX IF NOT EXISTS idx_referrals_referred_id ON public.referrals(referred_id);

CREATE INDEX IF NOT EXISTS idx_notifications_user_id  ON public.notifications(user_id);
CREATE INDEX IF NOT EXISTS idx_notifications_is_read  ON public.notifications(user_id, is_read);

-- ════════════════════════════════════════════
-- Auto-maintain updated_at on deposits/withdrawals.
-- (The app never set this column itself, so every row's updated_at
--  was permanently stuck at its created_at value — this fixes that.)
-- ════════════════════════════════════════════
CREATE OR REPLACE FUNCTION public.set_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_deposits_updated_at ON public.deposits;
CREATE TRIGGER trg_deposits_updated_at
    BEFORE UPDATE ON public.deposits
    FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

DROP TRIGGER IF EXISTS trg_withdrawals_updated_at ON public.withdrawals;
CREATE TRIGGER trg_withdrawals_updated_at
    BEFORE UPDATE ON public.withdrawals
    FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

-- ════════════════════════════════════════════
-- Atomic balance adjustment — fixes a real money bug.
--
-- The old app code did: "read user.balance in Python, subtract the
-- amount, write the new value back". If a user double-clicked Submit,
-- or had two tabs open, two requests could both read the SAME starting
-- balance before either one wrote back, letting them spend more than
-- they actually have. This function takes a row lock (FOR UPDATE) and
-- does the read-check-write as a single atomic database operation, so
-- concurrent requests are serialized correctly.
--
-- Called from app.py via: supabase.rpc('agrovest_adjust_balance', {...})
-- ════════════════════════════════════════════
CREATE OR REPLACE FUNCTION public.agrovest_adjust_balance(
    p_user_id                      BIGINT,
    p_balance_delta                NUMERIC DEFAULT 0,
    p_total_invested_delta         NUMERIC DEFAULT 0,
    p_total_earnings_delta         NUMERIC DEFAULT 0,
    p_referral_earnings_delta      NUMERIC DEFAULT 0,
    p_require_sufficient_balance   BOOLEAN DEFAULT FALSE
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
    v_balance NUMERIC;
BEGIN
    SELECT balance INTO v_balance FROM public.users WHERE id = p_user_id FOR UPDATE;

    IF v_balance IS NULL THEN
        RETURN FALSE;  -- user doesn't exist
    END IF;

    IF p_require_sufficient_balance AND (v_balance + p_balance_delta) < 0 THEN
        RETURN FALSE;  -- insufficient funds — abort, nothing is written
    END IF;

    UPDATE public.users
    SET balance           = balance + p_balance_delta,
        total_invested    = total_invested + p_total_invested_delta,
        total_earnings    = total_earnings + p_total_earnings_delta,
        referral_earnings = referral_earnings + p_referral_earnings_delta
    WHERE id = p_user_id;

    RETURN TRUE;
END;
$$;

GRANT EXECUTE ON FUNCTION public.agrovest_adjust_balance TO service_role, authenticated, anon;

-- Done! Tables are ready.
SELECT 'AgroVest tables created successfully!' AS status;

-- ════════════════════════════════════════════
-- OPTIONAL — persistent deposit-proof storage
--
-- Render's disk is ephemeral: anything saved to /tmp or the local
-- filesystem is wiped on every deploy and every restart. By default
-- this app still works fine (deposit proofs just won't survive a
-- redeploy). To persist them permanently instead, uncomment the line
-- below to create a private Storage bucket, then set
-- USE_SUPABASE_STORAGE=true in your environment variables.
-- ════════════════════════════════════════════
-- INSERT INTO storage.buckets (id, name, public)
-- VALUES ('deposit-proofs', 'deposit-proofs', false)
-- ON CONFLICT (id) DO NOTHING;

-- ════════════════════════════════════════════
-- OPTIONAL — persistent plan images
--
-- Same ephemeral-disk situation as above, but plan images (uploaded from
-- Admin → Plans) are public marketing content, not private documents, so
-- this bucket is created PUBLIC — anyone with the URL can view the image
-- (nobody can list/browse the bucket or see anything else). Uncomment to
-- create it, then set USE_SUPABASE_STORAGE=true (same flag as above covers
-- both buckets).
-- ════════════════════════════════════════════
-- INSERT INTO storage.buckets (id, name, public)
-- VALUES ('plan-images', 'plan-images', true)
-- ON CONFLICT (id) DO NOTHING;

-- ════════════════════════════════════════════
-- MIGRATION — Payment Settings & Multi-Bank Accounts
-- Safe to re-run. Adds:
--   1. `settings` key/value table (used by maintenance mode already —
--      created here in case your database doesn't have it yet).
--   2. `bank_accounts` table — unlimited admin-managed accounts, with
--      exactly one flagged is_active at a time (enforced by the app,
--      not a DB constraint, so admins can freely add/remove accounts).
--   3. `fee_amount` / `net_amount` columns on withdrawals — used when
--      an admin configures a withdrawal fee percentage in Payment
--      Settings. Existing rows are unaffected (defaults to 0 / amount).
-- ════════════════════════════════════════════

-- 8. SETTINGS (key/value store — also used by maintenance mode)
CREATE TABLE IF NOT EXISTS public.settings (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
ALTER TABLE public.settings DISABLE ROW LEVEL SECURITY;

-- 9. BANK ACCOUNTS (unlimited, admin-managed — only one is_active shows on payment.html)
CREATE TABLE IF NOT EXISTS public.bank_accounts (
    id             BIGSERIAL PRIMARY KEY,
    bank_name      TEXT NOT NULL,
    account_number TEXT NOT NULL,
    account_name   TEXT NOT NULL,
    logo_color     TEXT DEFAULT '#0f3d2e',
    is_active      BOOLEAN DEFAULT FALSE,
    sort_order     INTEGER DEFAULT 0,
    created_at     TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_bank_accounts_active ON public.bank_accounts(is_active);
ALTER TABLE public.bank_accounts DISABLE ROW LEVEL SECURITY;

-- Withdrawal fee columns (percentage-based fee, configured in Payment Settings)
ALTER TABLE public.withdrawals ADD COLUMN IF NOT EXISTS fee_amount NUMERIC(15,2) DEFAULT 0;
ALTER TABLE public.withdrawals ADD COLUMN IF NOT EXISTS net_amount NUMERIC(15,2);

SELECT 'Payment settings & bank accounts migration applied successfully!' AS status;

-- ════════════════════════════════════════════
-- MIGRATION — Gift Codes & Daily Check-in
-- Safe to re-run. No existing tables/columns touched.
-- ════════════════════════════════════════════

-- GIFT CODES — admin-managed redemption codes
CREATE TABLE IF NOT EXISTS public.gift_codes (
    id             BIGSERIAL PRIMARY KEY,
    code           TEXT UNIQUE NOT NULL,
    reward_amount  NUMERIC(15,2) NOT NULL,
    usage_limit    INTEGER NOT NULL DEFAULT 1,
    times_used     INTEGER NOT NULL DEFAULT 0,
    expires_at     TIMESTAMPTZ,
    is_active      BOOLEAN NOT NULL DEFAULT TRUE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE public.gift_codes DISABLE ROW LEVEL SECURITY;

-- One redemption per user per code — the UNIQUE constraint is the real
-- guard against double-redeeming (app-level checks are just a fast-fail
-- for a nicer error message; this is what actually prevents it under a race).
CREATE TABLE IF NOT EXISTS public.gift_code_redemptions (
    id             BIGSERIAL PRIMARY KEY,
    gift_code_id   BIGINT NOT NULL REFERENCES public.gift_codes(id) ON DELETE CASCADE,
    user_id        BIGINT NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    amount         NUMERIC(15,2) NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (gift_code_id, user_id)
);
ALTER TABLE public.gift_code_redemptions DISABLE ROW LEVEL SECURITY;
CREATE INDEX IF NOT EXISTS idx_gift_redemptions_user ON public.gift_code_redemptions(user_id);

-- DAILY CHECK-IN — one row per check-in; streak/last-checkin-date are
-- derived from this table's history rather than adding columns to `users`,
-- so no existing table structure needs to change.
CREATE TABLE IF NOT EXISTS public.checkins (
    id             BIGSERIAL PRIMARY KEY,
    user_id        BIGINT NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    reward_amount  NUMERIC(15,2) NOT NULL,
    streak_day     INTEGER NOT NULL DEFAULT 1,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE public.checkins DISABLE ROW LEVEL SECURITY;
CREATE INDEX IF NOT EXISTS idx_checkins_user_date ON public.checkins(user_id, created_at DESC);

-- TRANSACTIONS — records every daily-profit (ROI) credit. This table was
-- previously only documented as a comment in app.py ("create once in the
-- SQL editor if not present") rather than included in this migration file,
-- which is why production was hitting:
--   "column transactions.transaction_type does not exist"
-- The CREATE TABLE below is a no-op if the table already exists; the
-- ALTER TABLE ... ADD COLUMN IF NOT EXISTS lines then backfill any columns
-- missing from whatever version of the table you already have, without
-- touching existing rows or other columns.
CREATE TABLE IF NOT EXISTS public.transactions (
    id               BIGSERIAL PRIMARY KEY,
    user_id          BIGINT       NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    investment_id    BIGINT,
    plan_name        TEXT,
    amount           NUMERIC(15,2) NOT NULL,
    transaction_type TEXT         NOT NULL DEFAULT 'ROI',
    status           TEXT         NOT NULL DEFAULT 'Completed',
    description      TEXT,
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
ALTER TABLE public.transactions ADD COLUMN IF NOT EXISTS investment_id BIGINT;
ALTER TABLE public.transactions ADD COLUMN IF NOT EXISTS plan_name TEXT;
ALTER TABLE public.transactions ADD COLUMN IF NOT EXISTS transaction_type TEXT NOT NULL DEFAULT 'ROI';
ALTER TABLE public.transactions ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'Completed';
ALTER TABLE public.transactions ADD COLUMN IF NOT EXISTS description TEXT;
-- Legacy 'type' column — some deployments of this table have both `type`
-- and `transaction_type` as separate NOT NULL columns with no shared
-- default. The app now writes the same value to both on every insert, but
-- this ALTER covers two cases in one shot: (1) a fresh install gets a
-- `type` column with a default so it's never NULL even before the app's
-- fix is deployed, and (2) an existing table where `type` was NOT NULL
-- with NO default (the actual production error this fixes) gets that
-- constraint relaxed so old rows and any other insert path can't violate it.
ALTER TABLE public.transactions ADD COLUMN IF NOT EXISTS type TEXT DEFAULT 'ROI';
ALTER TABLE public.transactions ALTER COLUMN type SET DEFAULT 'ROI';
ALTER TABLE public.transactions ALTER COLUMN type DROP NOT NULL;
ALTER TABLE public.transactions DISABLE ROW LEVEL SECURITY;
CREATE INDEX IF NOT EXISTS idx_transactions_user_date ON public.transactions(user_id, created_at DESC);

-- PLANS — quota + badge fields (Phase 5, Investment Plans redesign).
-- All nullable/defaulted so existing plans behave exactly as before:
-- max_investors NULL = unlimited (no quota/progress bar shown), and both
-- badge flags default to false (no badge shown) until an admin sets them.
ALTER TABLE public.plans ADD COLUMN IF NOT EXISTS max_investors INTEGER;
ALTER TABLE public.plans ADD COLUMN IF NOT EXISTS is_popular BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE public.plans ADD COLUMN IF NOT EXISTS is_featured BOOLEAN NOT NULL DEFAULT FALSE;

-- USERS — saved bank details (Phase 9, Profile page). Optional/nullable —
-- users who never save these still withdraw exactly as before, just
-- re-entering bank details each time on the withdraw form. If saved,
-- the withdraw form pre-fills from these as a convenience only.
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS bank_name TEXT;
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS bank_account_number TEXT;
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS bank_account_name TEXT;

-- ANNOUNCEMENTS — Phase 10. A full, manageable announcement system,
-- distinct from the single sitewide banner (`settings.notice_*`, admin
-- page "Site Banner") built earlier. Each announcement is a persistent,
-- editable record; target_user_ids is only used when target_type =
-- 'selected'. Read state is tracked lazily — a row in
-- announcement_reads only exists once a user has actually opened it,
-- so broadcasting to "all" never requires inserting one row per user
-- up front.
CREATE TABLE IF NOT EXISTS public.announcements (
    id               BIGSERIAL PRIMARY KEY,
    title            TEXT NOT NULL,
    message          TEXT NOT NULL,
    is_important     BOOLEAN NOT NULL DEFAULT FALSE,
    target_type      TEXT NOT NULL DEFAULT 'all',   -- 'all' | 'selected'
    target_user_ids  INTEGER[],                      -- used only when target_type = 'selected'
    scheduled_at     TIMESTAMPTZ,                     -- NULL or in the past = published immediately
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by       BIGINT
);
ALTER TABLE public.announcements DISABLE ROW LEVEL SECURITY;

CREATE TABLE IF NOT EXISTS public.announcement_reads (
    id              BIGSERIAL PRIMARY KEY,
    announcement_id BIGINT NOT NULL REFERENCES public.announcements(id) ON DELETE CASCADE,
    user_id         BIGINT NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    read_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (announcement_id, user_id)
);
ALTER TABLE public.announcement_reads DISABLE ROW LEVEL SECURITY;

-- BANNERS — Phase 11 Banner Manager. Powers the promotional slider on
-- the dashboard (frontend already built in Phase 4, was passed an empty
-- list until this existed). is_active controls whether a banner shows;
-- sort_order controls slide order. Reuses the same public image-upload
-- path already used for plan images (upload_plan_image()).
CREATE TABLE IF NOT EXISTS public.banners (
    id          BIGSERIAL PRIMARY KEY,
    image_filename TEXT NOT NULL,
    title       TEXT,
    link_url    TEXT,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE public.banners DISABLE ROW LEVEL SECURITY;

-- Announcement Popup fields — extends the existing `announcements` table
-- (Phase 10) rather than creating a new system. All nullable/optional:
-- an announcement with none of these set just shows as a plain text
-- popup (title + message), same as before this existed.
ALTER TABLE public.announcements ADD COLUMN IF NOT EXISTS image_filename TEXT;
ALTER TABLE public.announcements ADD COLUMN IF NOT EXISTS telegram_channel_url TEXT;
ALTER TABLE public.announcements ADD COLUMN IF NOT EXISTS telegram_group_url TEXT;
ALTER TABLE public.announcements ADD COLUMN IF NOT EXISTS learn_more_url TEXT;

SELECT 'Gift Codes & Daily Check-in migration applied successfully!' AS status;
