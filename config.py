import os
from datetime import timedelta
from dotenv import load_dotenv

load_dotenv()

class Config:
    # ── Core ──────────────────────────────────────────────────
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-in-production")
    WTF_CSRF_SECRET_KEY = os.environ.get("WTF_CSRF_SECRET_KEY", SECRET_KEY)
    DEBUG = os.environ.get("FLASK_DEBUG", "0") == "1"

    # ── Session ───────────────────────────────────────────────
    PERMANENT_SESSION_LIFETIME = timedelta(
        minutes=int(os.environ.get("PERMANENT_SESSION_LIFETIME", 1440))
    )
    SESSION_COOKIE_SECURE   = os.environ.get("SESSION_COOKIE_SECURE", "false").lower() == "true"
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"

    # ── Supabase ──────────────────────────────────────────────
    SUPABASE_URL         = os.environ.get("SUPABASE_URL", "")
    SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
    SUPABASE_ANON_KEY    = os.environ.get("SUPABASE_ANON_KEY", "")
    SUPABASE_BUCKET      = os.environ.get("SUPABASE_STORAGE_BUCKET", "mercx-assets")

    # ── Email ─────────────────────────────────────────────────
    MAIL_SERVER         = os.environ.get("MAIL_SERVER", "smtp.gmail.com")
    MAIL_PORT           = int(os.environ.get("MAIL_PORT", 587))
    MAIL_USE_TLS        = os.environ.get("MAIL_USE_TLS", "true").lower() == "true"
    MAIL_USERNAME       = os.environ.get("MAIL_USERNAME", "")
    MAIL_PASSWORD       = os.environ.get("MAIL_PASSWORD", "")
    MAIL_DEFAULT_SENDER = os.environ.get("MAIL_DEFAULT_SENDER", "MercX Digital <noreply@mercxdigital.com>")

    # ── Payment Gateways ──────────────────────────────────────
    STRIPE_SECRET_KEY        = os.environ.get("STRIPE_SECRET_KEY", "")
    STRIPE_PUBLISHABLE_KEY   = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")
    STRIPE_WEBHOOK_SECRET    = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
    FLUTTERWAVE_SECRET_KEY   = os.environ.get("FLUTTERWAVE_SECRET_KEY", "")
    FLUTTERWAVE_PUBLIC_KEY   = os.environ.get("FLUTTERWAVE_PUBLIC_KEY", "")
    FLUTTERWAVE_WEBHOOK_SECRET = os.environ.get("FLUTTERWAVE_WEBHOOK_SECRET", "")
    PAYSTACK_SECRET_KEY      = os.environ.get("PAYSTACK_SECRET_KEY", "")
    PAYSTACK_PUBLIC_KEY      = os.environ.get("PAYSTACK_PUBLIC_KEY", "")

    # ── Redis ─────────────────────────────────────────────────
    REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

    # ── Upload / File Limits ──────────────────────────────────
    MAX_CONTENT_LENGTH = int(os.environ.get("MAX_CONTENT_LENGTH", 52428800))  # 50 MB
    ALLOWED_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp", "svg"}
    ALLOWED_FILE_EXTENSIONS  = {"zip", "rar", "tar", "gz", "7z", "pdf", "docx",
                                 "figma", "sketch", "xd", "psd", "ai", "mp3", "mp4",
                                 "ttf", "otf", "woff", "woff2", "csv", "json", "xml"}

    # ── Business Rules ────────────────────────────────────────
    SITE_URL              = os.environ.get("SITE_URL", "http://localhost:5000")
    COMMISSION_RATE       = float(os.environ.get("COMMISSION_RATE", 10)) / 100
    MAX_CART_ITEMS        = 20
    MAX_DOWNLOADS         = 5
    DOWNLOAD_EXPIRY_DAYS  = 7
    MIN_WITHDRAWAL        = 10.00
    MAX_WITHDRAWAL        = 10_000.00
    REFERRAL_BONUS        = 5.00

class DevelopmentConfig(Config):
    DEBUG = True

class ProductionConfig(Config):
    DEBUG = False
    SESSION_COOKIE_SECURE = True

config_map = {
    "development": DevelopmentConfig,
    "production":  ProductionConfig,
    "default":     DevelopmentConfig,
}

def get_config():
    env = os.environ.get("FLASK_ENV", "development")
    return config_map.get(env, DevelopmentConfig)
