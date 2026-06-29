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
