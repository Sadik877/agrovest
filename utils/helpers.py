import os
import re
import uuid
import hashlib
import secrets
import bleach
from datetime import datetime, timezone
from slugify import slugify as _slugify
from flask import current_app


# ── Text / Slug ───────────────────────────────────────────────

def make_slug(text: str, suffix: bool = True) -> str:
    """Create a URL-safe slug; optionally append a short uid to ensure uniqueness."""
    base = _slugify(text, max_length=200)
    if suffix:
        return f"{base}-{uuid.uuid4().hex[:6]}"
    return base


def sanitize_html(html: str, strip: bool = True) -> str:
    """Strip or escape HTML to prevent XSS. Pass strip=False to allow safe tags."""
    if strip:
        return bleach.clean(html, tags=[], strip=True)
    allowed_tags = ["b", "i", "u", "em", "strong", "a", "p", "br", "ul", "ol", "li",
                    "h2", "h3", "h4", "blockquote", "code", "pre", "span"]
    allowed_attrs = {"a": ["href", "title", "rel"], "span": ["class"]}
    return bleach.clean(html, tags=allowed_tags, attributes=allowed_attrs, strip=True)


def truncate(text: str, length: int = 120, suffix: str = "…") -> str:
    if not text:
        return ""
    return text if len(text) <= length else text[:length].rstrip() + suffix


# ── Crypto / Tokens ───────────────────────────────────────────

def generate_token(n: int = 48) -> str:
    """Cryptographically secure URL-safe token."""
    return secrets.token_urlsafe(n)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def generate_referral_code(username: str) -> str:
    prefix = re.sub(r"[^a-zA-Z0-9]", "", username).upper()[:6].ljust(4, "X")
    return f"{prefix}{secrets.token_hex(3).upper()}"


def generate_api_key() -> tuple[str, str, str]:
    """Returns (full_key, key_prefix, key_hash) for storage."""
    raw = f"mxk_{secrets.token_urlsafe(40)}"
    prefix = raw[:12]
    h = hashlib.sha256(raw.encode()).hexdigest()
    return raw, prefix, h


# ── Money / Numbers ───────────────────────────────────────────

def fmt_price(amount, currency_symbol: str = "$") -> str:
    try:
        return f"{currency_symbol}{float(amount):,.2f}"
    except (ValueError, TypeError):
        return f"{currency_symbol}0.00"


def calc_discount_pct(original: float, discounted: float) -> int:
    if not original or original <= 0:
        return 0
    return round((1 - discounted / original) * 100)


def calc_platform_fee(amount: float) -> tuple[float, float]:
    """Returns (fee, seller_earnings) based on config commission rate."""
    rate = current_app.config.get("COMMISSION_RATE", 0.10)
    fee = round(amount * rate, 2)
    earnings = round(amount - fee, 2)
    return fee, earnings


# ── Pagination ────────────────────────────────────────────────

class Paginator:
    def __init__(self, items: list, page: int, per_page: int = 20):
        self.per_page   = per_page
        self.total      = len(items)
        self.page       = max(1, min(page, self.pages or 1))
        start = (self.page - 1) * per_page
        self.items      = items[start: start + per_page]

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // self.per_page))

    @property
    def has_prev(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.pages

    @property
    def prev_num(self) -> int:
        return self.page - 1

    @property
    def next_num(self) -> int:
        return self.page + 1

    def iter_pages(self, left_edge=2, left_current=2, right_current=5, right_edge=2):
        last = 0
        for num in range(1, self.pages + 1):
            if (num <= left_edge
                    or (self.page - left_current - 1 < num < self.page + right_current)
                    or num > self.pages - right_edge):
                if last + 1 != num:
                    yield None
                yield num
                last = num


# ── File Validation ───────────────────────────────────────────

def allowed_image(filename: str) -> bool:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return ext in current_app.config.get("ALLOWED_IMAGE_EXTENSIONS", set())


def allowed_file(filename: str) -> bool:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return ext in current_app.config.get("ALLOWED_FILE_EXTENSIONS", set())


def safe_filename(filename: str) -> str:
    """Sanitize a filename, preserving extension."""
    name, _, ext = filename.rpartition(".")
    name = re.sub(r"[^a-zA-Z0-9_\-]", "_", name)[:80]
    return f"{name}.{ext.lower()}" if ext else name


def human_filesize(size_bytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


# ── Date / Time ───────────────────────────────────────────────

def time_ago(dt) -> str:
    if not dt:
        return "never"
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
    now  = datetime.now(timezone.utc)
    diff = now - dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else now - dt
    s    = int(diff.total_seconds())
    if s < 60:     return "just now"
    if s < 3600:   return f"{s // 60}m ago"
    if s < 86400:  return f"{s // 3600}h ago"
    if s < 604800: return f"{s // 86400}d ago"
    return dt.strftime("%b %d, %Y")


def fmt_date(dt, fmt: str = "%b %d, %Y") -> str:
    if not dt:
        return ""
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
    return dt.strftime(fmt)


# ── Order / Reference ─────────────────────────────────────────

def generate_reference(prefix: str = "REF") -> str:
    return f"{prefix}-{datetime.utcnow().strftime('%Y%m%d')}-{secrets.token_hex(4).upper()}"


# ── Audit logging helper ──────────────────────────────────────

def log_audit(user_id, action: str, resource_type: str = None,
              resource_id=None, details: dict = None, ip: str = None, ua: str = None):
    """Write a row to audit_logs. Import and call within request context."""
    from utils.supabase_client import db_insert
    db_insert("audit_logs", {
        "user_id":       str(user_id) if user_id else None,
        "action":        action,
        "resource_type": resource_type,
        "resource_id":   str(resource_id) if resource_id else None,
        "details":       details or {},
        "ip_address":    ip,
        "user_agent":    ua,
    })
