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
    description   TEXT,
    min_amount    NUMERIC(15,2) NOT NULL DEFAULT 10000,
    max_amount    NUMERIC(15,2),
    roi_percent   NUMERIC(5,2) NOT NULL DEFAULT 10,
    duration_days INTEGER NOT NULL DEFAULT 30,
    features      TEXT DEFAULT '',
    is_active     BOOLEAN DEFAULT TRUE,
    sort_order    INTEGER DEFAULT 0,
    created_at    TIMESTAMPTZ DEFAULT NOW()
);

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

-- Done! Tables are ready.
SELECT 'AgroVest tables created successfully!' AS status;
