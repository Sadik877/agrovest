-- ============================================================
-- MercX Digital Marketplace — Database Schema
-- Supabase PostgreSQL | Legal Digital Goods Only
-- ============================================================

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ============================================================
-- USERS & AUTH
-- ============================================================

CREATE TABLE public.users (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    email VARCHAR(255) UNIQUE NOT NULL,
    username VARCHAR(100) UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role VARCHAR(20) DEFAULT 'buyer' CHECK (role IN ('buyer', 'seller', 'admin', 'moderator')),
    is_verified BOOLEAN DEFAULT FALSE,
    is_banned BOOLEAN DEFAULT FALSE,
    is_suspended BOOLEAN DEFAULT FALSE,
    suspend_reason TEXT,
    balance DECIMAL(15,2) DEFAULT 0.00 CHECK (balance >= 0),
    two_factor_enabled BOOLEAN DEFAULT FALSE,
    two_factor_secret TEXT,
    email_verification_token TEXT,
    email_verification_expires TIMESTAMPTZ,
    password_reset_token TEXT,
    password_reset_expires TIMESTAMPTZ,
    last_login TIMESTAMPTZ,
    login_count INTEGER DEFAULT 0,
    failed_login_count INTEGER DEFAULT 0,
    locked_until TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);

CREATE TABLE public.user_profiles (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    user_id UUID REFERENCES public.users(id) ON DELETE CASCADE UNIQUE NOT NULL,
    full_name VARCHAR(255),
    bio TEXT,
    phone VARCHAR(50),
    country VARCHAR(100),
    timezone VARCHAR(100) DEFAULT 'UTC',
    avatar_url TEXT,
    cover_url TEXT,
    website VARCHAR(255),
    twitter VARCHAR(100),
    github VARCHAR(100),
    linkedin VARCHAR(255),
    -- Seller
    store_name VARCHAR(255),
    store_slug VARCHAR(255) UNIQUE,
    store_description TEXT,
    store_banner_url TEXT,
    seller_verified BOOLEAN DEFAULT FALSE,
    seller_rating DECIMAL(3,2) DEFAULT 0.00,
    total_sales INTEGER DEFAULT 0,
    total_revenue DECIMAL(15,2) DEFAULT 0.00,
    -- Referral
    referral_code VARCHAR(20) UNIQUE,
    referred_by UUID REFERENCES public.users(id),
    referral_earnings DECIMAL(15,2) DEFAULT 0.00,
    referral_count INTEGER DEFAULT 0,
    -- Prefs
    notifications_email BOOLEAN DEFAULT TRUE,
    notifications_inapp BOOLEAN DEFAULT TRUE,
    theme VARCHAR(10) DEFAULT 'dark',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================
-- CATEGORIES (Legal digital goods only)
-- ============================================================

CREATE TABLE public.categories (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    slug VARCHAR(100) UNIQUE NOT NULL,
    description TEXT,
    icon VARCHAR(100),
    image_url TEXT,
    color VARCHAR(7) DEFAULT '#7C3AED',
    parent_id UUID REFERENCES public.categories(id),
    sort_order INTEGER DEFAULT 0,
    listing_count INTEGER DEFAULT 0,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================
-- LISTINGS (Digital products)
-- ============================================================

CREATE TABLE public.listings (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    seller_id UUID REFERENCES public.users(id) ON DELETE CASCADE NOT NULL,
    category_id UUID REFERENCES public.categories(id),
    title VARCHAR(255) NOT NULL,
    slug VARCHAR(300) UNIQUE NOT NULL,
    description TEXT NOT NULL,
    short_description VARCHAR(500),
    price DECIMAL(15,2) NOT NULL CHECK (price >= 0),
    compare_price DECIMAL(15,2),
    license_type VARCHAR(30) DEFAULT 'personal'
        CHECK (license_type IN ('personal', 'commercial', 'extended', 'saas', 'unlimited')),
    version VARCHAR(50),
    file_format TEXT[],           -- ['zip', 'figma', 'sketch', 'pdf', etc.]
    file_size VARCHAR(50),
    demo_url TEXT,
    documentation_url TEXT,
    support_included BOOLEAN DEFAULT FALSE,
    support_duration_days INTEGER,
    updates_included BOOLEAN DEFAULT TRUE,
    stock INTEGER DEFAULT -1,     -- -1 = unlimited digital delivery
    delivery_type VARCHAR(20) DEFAULT 'instant'
        CHECK (delivery_type IN ('instant', 'manual')),
    download_url TEXT,            -- Stored securely, revealed only after purchase
    preview_images TEXT[],
    status VARCHAR(20) DEFAULT 'pending'
        CHECK (status IN ('pending', 'active', 'paused', 'rejected', 'deleted')),
    reject_reason TEXT,
    is_featured BOOLEAN DEFAULT FALSE,
    is_approved BOOLEAN DEFAULT FALSE,
    tags TEXT[] DEFAULT '{}',
    views INTEGER DEFAULT 0,
    sales_count INTEGER DEFAULT 0,
    download_count INTEGER DEFAULT 0,
    rating DECIMAL(3,2) DEFAULT 0.00,
    review_count INTEGER DEFAULT 0,
    wishlist_count INTEGER DEFAULT 0,
    meta_title VARCHAR(255),
    meta_description TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);

CREATE TABLE public.listing_images (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    listing_id UUID REFERENCES public.listings(id) ON DELETE CASCADE NOT NULL,
    url TEXT NOT NULL,
    alt_text VARCHAR(255),
    is_primary BOOLEAN DEFAULT FALSE,
    sort_order INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE public.listing_files (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    listing_id UUID REFERENCES public.listings(id) ON DELETE CASCADE NOT NULL,
    version VARCHAR(50),
    filename TEXT NOT NULL,
    file_url TEXT NOT NULL,           -- Supabase Storage path
    file_size_bytes BIGINT,
    download_count INTEGER DEFAULT 0,
    is_active BOOLEAN DEFAULT TRUE,
    changelog TEXT,
    released_at TIMESTAMPTZ DEFAULT NOW(),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE public.listing_reports (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    reporter_id UUID REFERENCES public.users(id) NOT NULL,
    listing_id UUID REFERENCES public.listings(id) NOT NULL,
    reason VARCHAR(100) NOT NULL,
    description TEXT,
    status VARCHAR(20) DEFAULT 'pending'
        CHECK (status IN ('pending', 'reviewed', 'actioned', 'dismissed')),
    reviewed_by UUID REFERENCES public.users(id),
    reviewed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================
-- ORDERS
-- ============================================================

CREATE TABLE public.orders (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    buyer_id UUID REFERENCES public.users(id) NOT NULL,
    seller_id UUID REFERENCES public.users(id) NOT NULL,
    order_number VARCHAR(50) UNIQUE NOT NULL,
    status VARCHAR(30) DEFAULT 'pending'
        CHECK (status IN ('pending', 'processing', 'completed', 'cancelled', 'refunded', 'disputed')),
    payment_method VARCHAR(50),
    payment_reference TEXT,
    gateway_response JSONB DEFAULT '{}',
    subtotal DECIMAL(15,2) NOT NULL,
    discount_amount DECIMAL(15,2) DEFAULT 0.00,
    coupon_id UUID,
    coupon_code VARCHAR(50),
    tax_amount DECIMAL(15,2) DEFAULT 0.00,
    platform_fee DECIMAL(15,2) DEFAULT 0.00,
    seller_earnings DECIMAL(15,2) DEFAULT 0.00,
    total DECIMAL(15,2) NOT NULL,
    buyer_note TEXT,
    dispute_reason TEXT,
    dispute_resolved BOOLEAN DEFAULT FALSE,
    refund_amount DECIMAL(15,2) DEFAULT 0.00,
    refund_reason TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE public.order_items (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    order_id UUID REFERENCES public.orders(id) ON DELETE CASCADE NOT NULL,
    listing_id UUID REFERENCES public.listings(id) NOT NULL,
    file_id UUID REFERENCES public.listing_files(id),
    title VARCHAR(255) NOT NULL,
    quantity INTEGER NOT NULL DEFAULT 1 CHECK (quantity > 0),
    unit_price DECIMAL(15,2) NOT NULL,
    total_price DECIMAL(15,2) NOT NULL,
    license_type VARCHAR(30),
    download_url TEXT,               -- Signed URL generated on purchase
    download_count INTEGER DEFAULT 0,
    max_downloads INTEGER DEFAULT 5,
    download_expires_at TIMESTAMPTZ,
    delivery_status VARCHAR(20) DEFAULT 'pending'
        CHECK (delivery_status IN ('pending', 'delivered', 'failed')),
    delivered_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================
-- WALLET & TRANSACTIONS
-- ============================================================

CREATE TABLE public.wallet_transactions (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    user_id UUID REFERENCES public.users(id) NOT NULL,
    type VARCHAR(30) NOT NULL
        CHECK (type IN ('deposit', 'withdrawal', 'purchase', 'sale', 'refund',
                        'referral', 'transfer_in', 'transfer_out', 'fee', 'bonus', 'chargeback')),
    amount DECIMAL(15,2) NOT NULL,
    balance_before DECIMAL(15,2) NOT NULL,
    balance_after DECIMAL(15,2) NOT NULL,
    reference VARCHAR(100) UNIQUE,
    status VARCHAR(20) DEFAULT 'pending'
        CHECK (status IN ('pending', 'completed', 'failed', 'cancelled')),
    payment_method VARCHAR(50),
    gateway_reference TEXT,
    description TEXT,
    metadata JSONB DEFAULT '{}',
    admin_note TEXT,
    processed_by UUID REFERENCES public.users(id),
    order_id UUID REFERENCES public.orders(id),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================
-- CART
-- ============================================================

CREATE TABLE public.cart_items (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    user_id UUID REFERENCES public.users(id) ON DELETE CASCADE NOT NULL,
    listing_id UUID REFERENCES public.listings(id) ON DELETE CASCADE NOT NULL,
    quantity INTEGER DEFAULT 1 CHECK (quantity > 0),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(user_id, listing_id)
);

-- ============================================================
-- MESSAGING
-- ============================================================

CREATE TABLE public.conversations (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    participant_1 UUID REFERENCES public.users(id) NOT NULL,
    participant_2 UUID REFERENCES public.users(id) NOT NULL,
    last_message_at TIMESTAMPTZ,
    unread_count_1 INTEGER DEFAULT 0,
    unread_count_2 INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(participant_1, participant_2)
);

CREATE TABLE public.messages (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    conversation_id UUID REFERENCES public.conversations(id) ON DELETE CASCADE NOT NULL,
    sender_id UUID REFERENCES public.users(id) NOT NULL,
    receiver_id UUID REFERENCES public.users(id) NOT NULL,
    order_id UUID REFERENCES public.orders(id),
    content TEXT NOT NULL,
    message_type VARCHAR(20) DEFAULT 'text' CHECK (message_type IN ('text', 'image', 'file', 'system')),
    attachment_url TEXT,
    is_read BOOLEAN DEFAULT FALSE,
    read_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================
-- NOTIFICATIONS
-- ============================================================

CREATE TABLE public.notifications (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    user_id UUID REFERENCES public.users(id) ON DELETE CASCADE NOT NULL,
    type VARCHAR(50) NOT NULL,
    title VARCHAR(255) NOT NULL,
    message TEXT NOT NULL,
    is_read BOOLEAN DEFAULT FALSE,
    link TEXT,
    icon VARCHAR(50),
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================
-- REVIEWS
-- ============================================================

CREATE TABLE public.reviews (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    buyer_id UUID REFERENCES public.users(id) NOT NULL,
    seller_id UUID REFERENCES public.users(id) NOT NULL,
    listing_id UUID REFERENCES public.listings(id) NOT NULL,
    order_id UUID REFERENCES public.orders(id),
    rating INTEGER NOT NULL CHECK (rating >= 1 AND rating <= 5),
    review_text TEXT,
    helpful_votes INTEGER DEFAULT 0,
    is_verified_purchase BOOLEAN DEFAULT FALSE,
    is_reported BOOLEAN DEFAULT FALSE,
    is_hidden BOOLEAN DEFAULT FALSE,
    seller_reply TEXT,
    seller_replied_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(buyer_id, listing_id)
);

CREATE TABLE public.review_votes (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    review_id UUID REFERENCES public.reviews(id) ON DELETE CASCADE NOT NULL,
    user_id UUID REFERENCES public.users(id) NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(review_id, user_id)
);

-- ============================================================
-- WISHLIST & BROWSING
-- ============================================================

CREATE TABLE public.wishlist (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    user_id UUID REFERENCES public.users(id) ON DELETE CASCADE NOT NULL,
    listing_id UUID REFERENCES public.listings(id) ON DELETE CASCADE NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(user_id, listing_id)
);

CREATE TABLE public.recently_viewed (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    user_id UUID REFERENCES public.users(id) ON DELETE CASCADE NOT NULL,
    listing_id UUID REFERENCES public.listings(id) ON DELETE CASCADE NOT NULL,
    viewed_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(user_id, listing_id)
);

-- ============================================================
-- COUPONS
-- ============================================================

CREATE TABLE public.coupons (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    code VARCHAR(50) UNIQUE NOT NULL,
    type VARCHAR(20) NOT NULL CHECK (type IN ('percentage', 'fixed')),
    value DECIMAL(10,2) NOT NULL CHECK (value > 0),
    min_order DECIMAL(15,2) DEFAULT 0.00,
    max_discount DECIMAL(15,2),
    max_uses INTEGER,
    used_count INTEGER DEFAULT 0,
    per_user_limit INTEGER DEFAULT 1,
    seller_id UUID REFERENCES public.users(id),
    category_id UUID REFERENCES public.categories(id),
    expires_at TIMESTAMPTZ,
    is_active BOOLEAN DEFAULT TRUE,
    description TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE public.coupon_uses (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    coupon_id UUID REFERENCES public.coupons(id) NOT NULL,
    user_id UUID REFERENCES public.users(id) NOT NULL,
    order_id UUID REFERENCES public.orders(id),
    discount_amount DECIMAL(15,2) NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================
-- SUPPORT
-- ============================================================

CREATE TABLE public.support_tickets (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    ticket_number VARCHAR(30) UNIQUE NOT NULL,
    user_id UUID REFERENCES public.users(id),
    order_id UUID REFERENCES public.orders(id),
    subject VARCHAR(255) NOT NULL,
    status VARCHAR(20) DEFAULT 'open'
        CHECK (status IN ('open', 'in_progress', 'waiting_reply', 'resolved', 'closed')),
    priority VARCHAR(20) DEFAULT 'normal'
        CHECK (priority IN ('low', 'normal', 'high', 'urgent')),
    category VARCHAR(50),
    assigned_to UUID REFERENCES public.users(id),
    closed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE public.ticket_messages (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    ticket_id UUID REFERENCES public.support_tickets(id) ON DELETE CASCADE NOT NULL,
    user_id UUID REFERENCES public.users(id) NOT NULL,
    message TEXT NOT NULL,
    is_staff BOOLEAN DEFAULT FALSE,
    attachments TEXT[] DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================
-- SITE SETTINGS
-- ============================================================

CREATE TABLE public.site_settings (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    key VARCHAR(100) UNIQUE NOT NULL,
    value TEXT,
    type VARCHAR(20) DEFAULT 'string',
    description TEXT,
    is_public BOOLEAN DEFAULT FALSE,
    updated_by UUID REFERENCES public.users(id),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================
-- API KEYS (for seller API access)
-- ============================================================

CREATE TABLE public.api_keys (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    user_id UUID REFERENCES public.users(id) ON DELETE CASCADE NOT NULL,
    name VARCHAR(100) NOT NULL,
    key_hash TEXT NOT NULL,
    key_prefix VARCHAR(20) NOT NULL,
    permissions TEXT[] DEFAULT '{}',
    last_used TIMESTAMPTZ,
    request_count INTEGER DEFAULT 0,
    expires_at TIMESTAMPTZ,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================
-- AUDIT LOGS
-- ============================================================

CREATE TABLE public.audit_logs (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    user_id UUID REFERENCES public.users(id),
    action VARCHAR(100) NOT NULL,
    resource_type VARCHAR(50),
    resource_id UUID,
    details JSONB DEFAULT '{}',
    ip_address INET,
    user_agent TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================
-- NEWSLETTER
-- ============================================================

CREATE TABLE public.newsletter_subscribers (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    email VARCHAR(255) UNIQUE NOT NULL,
    name VARCHAR(255),
    is_active BOOLEAN DEFAULT TRUE,
    subscribed_at TIMESTAMPTZ DEFAULT NOW(),
    unsubscribed_at TIMESTAMPTZ
);

-- ============================================================
-- INDEXES
-- ============================================================

CREATE INDEX idx_users_email ON public.users(email);
CREATE INDEX idx_users_username ON public.users(username);
CREATE INDEX idx_users_role ON public.users(role);
CREATE INDEX idx_listings_seller ON public.listings(seller_id);
CREATE INDEX idx_listings_category ON public.listings(category_id);
CREATE INDEX idx_listings_status ON public.listings(status);
CREATE INDEX idx_listings_featured ON public.listings(is_featured) WHERE is_featured = TRUE;
CREATE INDEX idx_listings_price ON public.listings(price);
CREATE INDEX idx_listings_created ON public.listings(created_at DESC);
CREATE INDEX idx_listings_slug ON public.listings(slug);
CREATE INDEX idx_listings_search ON public.listings USING gin(
    to_tsvector('english', title || ' ' || COALESCE(short_description,'') || ' ' || COALESCE(description,''))
);
CREATE INDEX idx_listings_tags ON public.listings USING gin(tags);
CREATE INDEX idx_orders_buyer ON public.orders(buyer_id);
CREATE INDEX idx_orders_seller ON public.orders(seller_id);
CREATE INDEX idx_orders_status ON public.orders(status);
CREATE INDEX idx_orders_number ON public.orders(order_number);
CREATE INDEX idx_wallet_user ON public.wallet_transactions(user_id);
CREATE INDEX idx_wallet_type ON public.wallet_transactions(type);
CREATE INDEX idx_wallet_status ON public.wallet_transactions(status);
CREATE INDEX idx_wallet_created ON public.wallet_transactions(created_at DESC);
CREATE INDEX idx_messages_conv ON public.messages(conversation_id);
CREATE INDEX idx_notifications_user ON public.notifications(user_id);
CREATE INDEX idx_notifications_unread ON public.notifications(user_id, is_read) WHERE is_read = FALSE;
CREATE INDEX idx_reviews_listing ON public.reviews(listing_id);
CREATE INDEX idx_wishlist_user ON public.wishlist(user_id);
CREATE INDEX idx_cart_user ON public.cart_items(user_id);
CREATE INDEX idx_audit_user ON public.audit_logs(user_id);
CREATE INDEX idx_audit_created ON public.audit_logs(created_at DESC);

-- ============================================================
-- TRIGGERS
-- ============================================================

CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN NEW.updated_at = NOW(); RETURN NEW; END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_users_upd BEFORE UPDATE ON public.users FOR EACH ROW EXECUTE FUNCTION update_updated_at();
CREATE TRIGGER trg_profiles_upd BEFORE UPDATE ON public.user_profiles FOR EACH ROW EXECUTE FUNCTION update_updated_at();
CREATE TRIGGER trg_listings_upd BEFORE UPDATE ON public.listings FOR EACH ROW EXECUTE FUNCTION update_updated_at();
CREATE TRIGGER trg_orders_upd BEFORE UPDATE ON public.orders FOR EACH ROW EXECUTE FUNCTION update_updated_at();
CREATE TRIGGER trg_wallet_upd BEFORE UPDATE ON public.wallet_transactions FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- Auto-generate order number
CREATE OR REPLACE FUNCTION generate_order_number()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.order_number IS NULL OR NEW.order_number = '' THEN
        NEW.order_number := 'MX-' || TO_CHAR(NOW(), 'YYYYMMDD') || '-' ||
                            UPPER(SUBSTRING(gen_random_uuid()::TEXT, 1, 6));
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_order_number BEFORE INSERT ON public.orders
    FOR EACH ROW EXECUTE FUNCTION generate_order_number();

-- Auto-generate ticket number
CREATE OR REPLACE FUNCTION generate_ticket_number()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.ticket_number IS NULL OR NEW.ticket_number = '' THEN
        NEW.ticket_number := 'TK-' || TO_CHAR(NOW(), 'YYYYMMDD') || '-' ||
                             LPAD(FLOOR(RANDOM() * 10000)::TEXT, 4, '0');
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_ticket_number BEFORE INSERT ON public.support_tickets
    FOR EACH ROW EXECUTE FUNCTION generate_ticket_number();

-- Update listing rating on review change
CREATE OR REPLACE FUNCTION update_listing_rating()
RETURNS TRIGGER AS $$
DECLARE v_lid UUID;
BEGIN
    v_lid := COALESCE(NEW.listing_id, OLD.listing_id);
    UPDATE public.listings SET
        rating = COALESCE((SELECT ROUND(AVG(rating)::numeric, 2) FROM public.reviews WHERE listing_id = v_lid AND is_hidden = FALSE), 0),
        review_count = (SELECT COUNT(*) FROM public.reviews WHERE listing_id = v_lid AND is_hidden = FALSE)
    WHERE id = v_lid;
    RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_listing_rating AFTER INSERT OR UPDATE OR DELETE ON public.reviews
    FOR EACH ROW EXECUTE FUNCTION update_listing_rating();

-- Update category listing count
CREATE OR REPLACE FUNCTION update_category_count()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'INSERT' AND NEW.status = 'active' AND NEW.is_approved THEN
        UPDATE public.categories SET listing_count = listing_count + 1 WHERE id = NEW.category_id;
    ELSIF TG_OP = 'DELETE' AND OLD.status = 'active' THEN
        UPDATE public.categories SET listing_count = GREATEST(listing_count - 1, 0) WHERE id = OLD.category_id;
    ELSIF TG_OP = 'UPDATE' THEN
        IF OLD.status != 'active' AND NEW.status = 'active' AND NEW.is_approved THEN
            UPDATE public.categories SET listing_count = listing_count + 1 WHERE id = NEW.category_id;
        ELSIF OLD.status = 'active' AND NEW.status != 'active' THEN
            UPDATE public.categories SET listing_count = GREATEST(listing_count - 1, 0) WHERE id = OLD.category_id;
        END IF;
    END IF;
    RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_category_count AFTER INSERT OR UPDATE OR DELETE ON public.listings
    FOR EACH ROW EXECUTE FUNCTION update_category_count();

-- ============================================================
-- DEFAULT CATEGORIES (Legal digital goods only)
-- ============================================================

INSERT INTO public.categories (name, slug, description, icon, color, sort_order) VALUES
('Templates & Themes',  'templates-themes',   'Website templates, WordPress themes, landing pages, email templates', 'layout',         '#7C3AED', 1),
('UI Kits & Design',    'ui-kits-design',     'Figma, Sketch, Adobe XD design systems, component libraries, wireframes', 'pen-tool',    '#06B6D4', 2),
('Source Code',         'source-code',        'Full applications, scripts, plugins, snippets across any language',   'code',          '#10B981', 3),
('APIs & Integrations', 'apis-integrations',  'REST APIs, SDKs, automation tools, webhooks, SaaS integrations',     'zap',           '#F59E0B', 4),
('eBooks & Courses',    'ebooks-courses',     'Programming guides, tutorials, documentation, video courses',         'book-open',     '#EF4444', 5),
('SaaS Products',       'saas-products',      'Ready-to-deploy software, white-label solutions, micro-SaaS tools',   'cloud',         '#8B5CF6', 6),
('Fonts & Typography',  'fonts-typography',   'Custom typefaces, font families, variable fonts, type specimens',     'type',          '#EC4899', 7),
('Icons & Illustrations','icons-illustrations','Icon packs, illustration sets, vector art, SVG libraries',           'image',         '#14B8A6', 8),
('Plugins & Extensions','plugins-extensions', 'Browser extensions, IDE plugins, CMS plugins, framework add-ons',    'puzzle',        '#F97316', 9),
('Mobile Apps',         'mobile-apps',        'iOS/Android source code, Flutter, React Native, Expo starters',       'smartphone',    '#3B82F6', 10),
('Stock Assets',        'stock-assets',       'Royalty-free photos, videos, audio tracks, sound effects',            'film',          '#6366F1', 11),
('Tools & Utilities',   'tools-utilities',    'CLI tools, desktop apps, browser bookmarklets, productivity scripts', 'tool',          '#84CC16', 12);

-- ============================================================
-- DEFAULT SITE SETTINGS
-- ============================================================

INSERT INTO public.site_settings (key, value, type, description, is_public) VALUES
('site_name',                'MercX Digital Marketplace', 'string', 'Website name', true),
('site_tagline',             'Buy & Sell Digital Products Securely.',   'string', 'Tagline', true),
('site_email',               'hello@mercxdigital.com',   'string', 'Contact email', true),
('site_url',                 'https://mercxdigital.com', 'string', 'Site URL', true),
('currency',                 'USD',   'string', 'Default currency', true),
('currency_symbol',          '$',     'string', 'Currency symbol',  true),
('commission_rate',          '10',    'number', 'Platform fee %',   false),
('min_withdrawal',           '10',    'number', 'Min withdrawal',   true),
('max_withdrawal',           '10000', 'number', 'Max withdrawal',   true),
('min_deposit',              '5',     'number', 'Min deposit',      true),
('listing_approval_required','true',  'boolean','Require approval', false),
('maintenance_mode',         'false', 'boolean','Maintenance mode', false),
('allow_registration',       'true',  'boolean','Allow signups',    false),
('flutterwave_enabled',      'false', 'boolean','Flutterwave',      false),
('paystack_enabled',         'false', 'boolean','Paystack',         false),
('stripe_enabled',           'false', 'boolean','Stripe',           false),
('referral_bonus',           '5',     'number', 'Referral bonus $', false),
('max_cart_items',           '20',    'number', 'Max cart items',   false),
('max_downloads_per_purchase','5',    'number', 'Downloads/purchase',false),
('download_link_expiry_days','7',     'number', 'Download expiry',  false);
