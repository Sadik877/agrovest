"""
AgroVest Pro - Agricultural Investment Platform
Backend: Flask + Supabase (supabase-py client — pure Python, no C extensions)
"""

import os
import re
import time
import uuid
import secrets
from datetime import datetime, timedelta
from functools import wraps

from flask import (Flask, render_template, request, redirect, url_for,
                   flash, session, g, jsonify, abort, send_from_directory)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.exceptions import RequestEntityTooLarge
from flask_wtf import CSRFProtect
from flask_wtf.csrf import CSRFError

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ─────────────────────────────────────────────
# Platform detection
# Render automatically sets RENDER=true on every
# service it runs — no manual env var needed.
# ─────────────────────────────────────────────
IS_RENDER = (os.environ.get('RENDER', '').strip().lower() == 'true' or
             os.environ.get('RENDER_TMP', '').strip().lower() == 'true')

# ─────────────────────────────────────────────
# App Configuration
# ─────────────────────────────────────────────
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY') or secrets.token_hex(32)
if not os.environ.get('SECRET_KEY'):
    print("⚠ SECRET_KEY not set — using a random key for this process only "
          "(sessions will not survive a restart). Set SECRET_KEY in your environment.")
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=2)
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024  # 5MB

# Session / cookie security
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = IS_RENDER  # HTTPS-only cookies in production
app.config['WTF_CSRF_TIME_LIMIT'] = None  # CSRF tokens don't expire mid-session

csrf = CSRFProtect(app)

# Upload folder — NOTE: Render's free/standard web service disk is EPHEMERAL.
# Anything written here is wiped on every deploy and every restart. This is
# fine as a zero-config default, but for real production use, set
# USE_SUPABASE_STORAGE=true (see upload_proof() below) to persist deposit
# proof files in a Supabase Storage bucket instead.
if IS_RENDER:
    _UPLOAD_DIR = '/tmp/agrovest_uploads'
else:
    _UPLOAD_DIR = os.path.join(app.root_path, 'static', 'uploads')
os.makedirs(_UPLOAD_DIR, exist_ok=True)
app.config['UPLOAD_FOLDER'] = _UPLOAD_DIR

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'pdf'}
USE_SUPABASE_STORAGE = os.environ.get('USE_SUPABASE_STORAGE', '').strip().lower() == 'true'
SUPABASE_STORAGE_BUCKET = os.environ.get('SUPABASE_STORAGE_BUCKET', 'deposit-proofs').strip()
REFERRAL_COMMISSION = 5  # percent


def r2(value):
    """Round a money value to 2dp and return a plain float (avoids float drift)."""
    try:
        return round(float(value or 0), 2)
    except (TypeError, ValueError):
        return 0.0

# ─────────────────────────────────────────────
# Supabase Client  — lazy init so env vars are
# read at request time, not at import time
# ─────────────────────────────────────────────
_sb = None

def _connect_sb():
    """Build a fresh Supabase client. Raises a clear, actionable error on bad config."""
    url = os.environ.get('SUPABASE_URL', '').strip()
    key = os.environ.get('SUPABASE_SECRET_KEY', '').strip()

    if not url or not key:
        missing = []
        if not url: missing.append('SUPABASE_URL')
        if not key: missing.append('SUPABASE_SECRET_KEY')
        raise RuntimeError(
            f"Missing environment variables: {', '.join(missing)}. "
            "Go to Render → your service → Environment → Add Environment Variable."
        )

    if not url.startswith('https://'):
        raise RuntimeError(
            f"SUPABASE_URL looks wrong: '{url[:50]}'. "
            "It must start with https:// e.g. https://abcxyz.supabase.co"
        )

    from supabase import create_client
    client = create_client(url, key)
    print(f"✓ Supabase connected to {url[:40]}...")
    return client


def get_sb():
    """Return a cached Supabase client (reads env vars lazily)."""
    global _sb
    if _sb is None:
        _sb = _connect_sb()
    return _sb

def reset_sb():
    """Force reconnect (used after env var changes)."""
    global _sb
    _sb = None

# ─────────────────────────────────────────────
# DB helper wrappers (thin layer over supabase-py v2)
# ─────────────────────────────────────────────
_TRANSIENT_HINTS = ('timeout', 'timed out', 'connection', 'temporarily unavailable',
                     'reset by peer', 'broken pipe', '502', '503', '504',
                     # DNS resolution failures — common for the first second or two
                     # after a fresh deploy/restart, before the container's network
                     # is fully up. Almost always resolves itself on retry.
                     'name or service not known', 'nodename nor servname',
                     'temporary failure in name resolution', 'getaddrinfo',
                     'no address associated with hostname', 'errno -2',
                     'network is unreachable')

def _is_transient(exc):
    msg = str(exc).lower()
    return any(hint in msg for hint in _TRANSIENT_HINTS)

def _with_retry(fn, what, retries=2, backoff=0.4):
    """Run fn() with a couple of retries for transient network errors only.
    Returns (ok, result_or_exception)."""
    last_exc = None
    for attempt in range(retries + 1):
        try:
            return True, fn()
        except Exception as e:
            last_exc = e
            if attempt < retries and _is_transient(e):
                time.sleep(backoff * (attempt + 1))
                reset_sb()  # reconnect — handles dropped connections
                continue
            break
    print(f'{what} error: {last_exc}')
    return False, last_exc

def _apply_filters(q, filters):
    """Apply a list of (col, op, val) filters to a supabase query."""
    for f in (filters or []):
        col, op, val = f
        if op == 'eq':    q = q.eq(col, val)
        elif op == 'neq': q = q.neq(col, val)
        elif op == 'lt':  q = q.lt(col, val)
        elif op == 'lte': q = q.lte(col, val)
        elif op == 'gt':  q = q.gt(col, val)
        elif op == 'gte': q = q.gte(col, val)
        elif op == 'in':  q = q.in_(col, val)
    return q

def sb_all(table, filters=None, order=None, limit=None):
    """Fetch all rows. order can be a single (col, dir) or list of them."""
    def _run():
        q = get_sb().table(table).select('*')
        q = _apply_filters(q, filters)
        nonlocal order
        if order:
            # Normalise to list of (col, dir) tuples
            if isinstance(order, (list, tuple)) and len(order) == 2 and isinstance(order[0], str):
                order = [order]   # single tuple e.g. ('created_at', 'desc')
            for item in order:
                col, direction = item
                q = q.order(col, desc=(str(direction).lower() == 'desc'))
        if limit:
            q = q.limit(limit)
        r = q.execute()
        return r.data or []
    ok, result = _with_retry(_run, f'sb_all({table})')
    return result if ok else []

def sb_one(table, filters):
    """Fetch a single row or None."""
    rows = sb_all(table, filters=filters, limit=1)
    return rows[0] if rows else None

def sb_insert(table, data):
    """Insert a row. Returns the inserted record, or None on failure."""
    def _run():
        r = get_sb().table(table).insert(data).execute()
        return r.data[0] if r.data else None
    ok, result = _with_retry(_run, f'sb_insert({table})', retries=0)
    return result if ok else None

def sb_update(table, data, filters):
    """Update rows matching filters. Returns the updated rows (a list — empty
    if the filter matched nothing, e.g. someone already processed this row),
    or False if the request itself failed."""
    def _run():
        q = get_sb().table(table).update(data)
        q = _apply_filters(q, filters)
        r = q.execute()
        return r.data or []
    ok, result = _with_retry(_run, f'sb_update({table})', retries=0)
    return result if ok else False

def sb_delete(table, filters):
    """Delete rows matching filters. Returns True on success, False on failure."""
    def _run():
        q = get_sb().table(table).delete()
        q = _apply_filters(q, filters)
        q.execute()
        return True
    ok, _ = _with_retry(_run, f'sb_delete({table})', retries=0)
    return ok

def sb_count(table, filters=None):
    """Count matching rows using Supabase count API."""
    def _run():
        q = get_sb().table(table).select('*', count='exact').limit(1)
        q = _apply_filters(q, filters)
        r = q.execute()
        return r.count if r.count is not None else 0
    ok, result = _with_retry(_run, f'sb_count({table})')
    return result if ok else 0

def sb_sum(table, col, filters=None):
    """Sum a numeric column in Python (Supabase free tier has no SQL aggregates)."""
    try:
        rows = sb_all(table, filters=filters)
        return sum(float(r.get(col) or 0) for r in rows)
    except Exception as e:
        print(f'sb_sum error ({table}/{col}): {e}')
        return 0

def notify(user_id, message, ntype='info'):
    sb_insert('notifications', {'user_id': user_id, 'message': message, 'type': ntype})


def sb_adjust_balance(user_id, balance_delta=0, total_invested_delta=0,
                       total_earnings_delta=0, referral_earnings_delta=0,
                       require_sufficient_balance=False):
    """Atomically adjust a user's money fields via a Postgres RPC function
    (agrovest_adjust_balance — created by supabase_setup.sql). This avoids the
    classic 'read balance in Python, subtract, write it back' race condition,
    where two simultaneous requests (e.g. a double-clicked submit, or two
    browser tabs) could both read the same stale balance and overspend it.
    The Postgres function takes a row lock and checks sufficiency atomically.

    Returns True on success, False if require_sufficient_balance=True and the
    balance would go negative (or the user doesn't exist / the call failed).
    """
    def _run():
        r = get_sb().rpc('agrovest_adjust_balance', {
            'p_user_id': user_id,
            'p_balance_delta': r2(balance_delta),
            'p_total_invested_delta': r2(total_invested_delta),
            'p_total_earnings_delta': r2(total_earnings_delta),
            'p_referral_earnings_delta': r2(referral_earnings_delta),
            'p_require_sufficient_balance': bool(require_sufficient_balance),
        }).execute()
        data = r.data
        # PostgREST may return the scalar directly, or wrapped in a list —
        # handle both shapes rather than assuming one.
        if isinstance(data, list):
            data = data[0] if data else False
        return bool(data)
    ok, result = _with_retry(_run, 'sb_adjust_balance', retries=0)
    return result if ok else False


def is_allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[-1].lower() in ALLOWED_EXTENSIONS


def upload_proof(file_storage):
    """Save an uploaded deposit-proof file.

    By default saves to local disk (works everywhere, zero config — but on
    Render this disk is wiped on every deploy/restart). If USE_SUPABASE_STORAGE=true
    and a bucket named SUPABASE_STORAGE_BUCKET exists, the file is uploaded there
    instead and persists permanently. Falls back to local disk automatically if
    the Supabase upload fails for any reason, so this never blocks a deposit.

    Returns the value to store in deposits.proof_filename, or None.
    """
    ext = file_storage.filename.rsplit('.', 1)[-1].lower()
    stored_name = f"{uuid.uuid4().hex}.{ext}"

    if USE_SUPABASE_STORAGE:
        try:
            content_type = {
                'png': 'image/png', 'jpg': 'image/jpeg',
                'jpeg': 'image/jpeg', 'pdf': 'application/pdf',
            }.get(ext, 'application/octet-stream')
            file_bytes = file_storage.read()
            get_sb().storage.from_(SUPABASE_STORAGE_BUCKET).upload(
                stored_name, file_bytes, file_options={'content-type': content_type})
            return f"sb:{stored_name}"
        except Exception as e:
            print(f"⚠ Supabase Storage upload failed, falling back to local disk: {e}")
            try:
                file_storage.stream.seek(0)
            except Exception:
                pass

    file_storage.save(os.path.join(app.config['UPLOAD_FOLDER'], stored_name))
    return stored_name


def resolve_proof_url(proof_filename):
    """Turn a stored proof_filename value into a viewable URL (or None)."""
    if not proof_filename:
        return None
    if proof_filename.startswith('sb:'):
        path = proof_filename[3:]
        try:
            res = get_sb().storage.from_(SUPABASE_STORAGE_BUCKET).create_signed_url(path, 3600)
            return res.get('signedURL') or res.get('signedUrl') or res.get('signed_url')
        except Exception as e:
            print(f'resolve_proof_url error: {e}')
            return None
    return url_for('uploaded_file', filename=proof_filename)

# ─────────────────────────────────────────────
# Database Initializer — runs SQL via Supabase RPC
# We create tables using Supabase Dashboard SQL editor instead.
# This just seeds admin + default plans if missing.
# ─────────────────────────────────────────────
def init_db():
    default_plans = [
        {'name': 'Starter Farm',  'slug': 'starter',      'icon': '🌱',
         'description': 'Perfect entry point for new agricultural investors.',
         'min_amount': 10000, 'max_amount': 49999,  'roi_percent': 15, 'duration_days': 30,
         'features': 'Daily ROI updates|Email notifications|Basic support',
         'sort_order': 1, 'is_active': True},
        {'name': 'Green Harvest', 'slug': 'green-harvest','icon': '🌿',
         'description': 'Mid-range plan with diversified crop investments.',
         'min_amount': 50000, 'max_amount': 199999, 'roi_percent': 25, 'duration_days': 45,
         'features': 'Daily ROI updates|Priority support|Monthly reports|Referral bonus',
         'sort_order': 2, 'is_active': True},
        {'name': 'Premium Agro',  'slug': 'premium-agro', 'icon': '🌾',
         'description': 'Premium returns from large-scale farming operations.',
         'min_amount': 200000, 'max_amount': 999999, 'roi_percent': 40, 'duration_days': 60,
         'features': 'Daily ROI updates|24/7 VIP support|Weekly reports|Higher referral bonus|Early withdrawal option',
         'sort_order': 3, 'is_active': True},
        {'name': 'Elite Farm',    'slug': 'elite',        'icon': '👑',
         'description': 'Elite tier for serious investors seeking maximum returns.',
         'min_amount': 1000000, 'max_amount': None, 'roi_percent': 60, 'duration_days': 90,
         'features': 'Daily ROI updates|Dedicated account manager|Daily reports|Maximum referral bonus|Flexible withdrawal|Farm visit opportunity',
         'sort_order': 4, 'is_active': True},
    ]

    # Ride out the brief DNS/network window right after a fresh deploy or
    # restart, before retrying anything that needs real DB access below.
    connected = False
    for attempt in range(6):
        try:
            sb_count('users')  # cheapest possible call — just proves connectivity
            connected = True
            break
        except Exception as e:
            print(f"⚠ DB not reachable yet (attempt {attempt + 1}/6): {e}")
            time.sleep(1.5 * (attempt + 1))
            reset_sb()
    if not connected:
        print("⚠ DB init warning: could not reach Supabase after several attempts — "
              "will keep retrying on incoming requests.")
        return

    try:
        # Seed admin if not exists
        if not sb_one('users', [('email', 'eq', 'admin@agrovest.ng')]):
            admin = sb_insert('users', {
                'full_name': 'AgroVest Admin',
                'email': 'admin@agrovest.ng',
                'phone': '08000000000',
                'password_hash': generate_password_hash('Admin@2024!'),
                'referral_code': 'ADMIN001',
                'is_admin': True,
                'is_active': True,
                'balance': 0,
                'total_invested': 0,
                'total_earnings': 0,
                'referral_earnings': 0,
            })
            print("✓ Admin user seeded" if admin else
                  "⚠ Admin user seed FAILED — will retry on next restart")

        # Seed default plans one-by-one (not "only if the table is totally
        # empty") so a partial failure on a previous attempt gets healed
        # automatically instead of leaving the platform permanently short
        # a plan or two.
        seeded, failed = 0, 0
        for p in default_plans:
            if not sb_one('plans', [('slug', 'eq', p['slug'])]):
                if sb_insert('plans', p):
                    seeded += 1
                else:
                    failed += 1
        if seeded:
            print(f"✓ {seeded} plan(s) seeded")
        if failed:
            print(f"⚠ {failed} plan(s) FAILED to seed — will retry on next restart")
        if not seeded and not failed:
            print("✓ Plans already seeded")

        print("✓ Database ready")
    except Exception as e:
        print(f"⚠ DB init warning: {e}")

# ─────────────────────────────────────────────
# Plan Helper
# ─────────────────────────────────────────────
def get_plans(active_only=True):
    filters = [('is_active', 'eq', True)] if active_only else []
    rows = sb_all('plans', filters=filters, order=[('sort_order', 'asc'), ('id', 'asc')])
    plans = []
    for r in rows:
        p = dict(r)
        p['features'] = [f.strip() for f in (p.get('features') or '').split('|') if f.strip()]
        p['min_amount'] = float(p.get('min_amount') or 0)
        p['max_amount'] = float(p['max_amount']) if p.get('max_amount') else None
        p['roi_percent'] = float(p.get('roi_percent') or 0)
        plans.append(p)
    return plans

# ─────────────────────────────────────────────
# Auth Decorators
# ─────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            flash('Please log in to continue.', 'warning')
            return redirect(url_for('login'))
        user = get_current_user()
        if not user or not user.get('is_active'):
            session.clear()
            flash('Your account is no longer active. Please contact support.'
                  if user else 'Please log in to continue.', 'error')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        user = get_current_user()
        if not user or not user.get('is_active') or not user.get('is_admin'):
            abort(403)
        return f(*args, **kwargs)
    return decorated

def get_current_user():
    """Fetch the logged-in user, cached per-request (g) so repeated calls
    within the same request — decorator, route body, context processor —
    only hit the database once."""
    if 'user_id' not in session:
        return None
    if not hasattr(g, '_current_user_cache'):
        g._current_user_cache = sb_one('users', [('id', 'eq', session['user_id'])])
    return g._current_user_cache

@app.context_processor
def inject_globals():
    user, unread, plans = None, 0, []
    try:
        user = get_current_user()
    except Exception as e:
        print(f"inject_globals get_current_user error: {e}")
    try:
        if user:
            unread = sb_count('notifications',
                              [('user_id', 'eq', user['id']), ('is_read', 'eq', False)])
    except Exception as e:
        print(f"inject_globals unread count error: {e}")
    try:
        plans = get_plans()
    except Exception as e:
        print(f"inject_globals get_plans error: {e}")
    return dict(current_user=user, unread_count=unread, plans=plans)

# ─────────────────────────────────────────────
# Serve Uploads
# ─────────────────────────────────────────────
@app.route('/uploads/<path:filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)


# ─────────────────────────────────────────────
# Env-var health check — shown instead of 500
# when Supabase credentials are missing/wrong
# ─────────────────────────────────────────────
def check_env():
    """Return (ok, missing_list, errors_list)."""
    url = os.environ.get('SUPABASE_URL', '').strip()
    key = os.environ.get('SUPABASE_KEY', '').strip()
    missing, errors = [], []
    if not url:
        missing.append('SUPABASE_URL')
    elif not url.startswith('https://'):
        errors.append(f'SUPABASE_URL must start with https:// — got: {url[:60]}')
    if not key:
        missing.append('SUPABASE_KEY')
    return (len(missing) == 0 and len(errors) == 0), missing, errors

@app.before_request
def guard_env():
    """Block all routes with a setup page if env vars are missing."""
    # Allow static files always
    if request.endpoint in ('static', 'uploaded_file', 'setup_page'):
        return
    ok, missing, errors = check_env()
    if not ok:
        return render_template('setup.html', missing=missing, errors=errors), 503

@app.route('/setup')
def setup_page():
    ok, missing, errors = check_env()
    return render_template('setup.html', missing=missing, errors=errors,
                           ok=ok), 200 if ok else 503

# ─────────────────────────────────────────────
# Simple in-memory rate limiting for auth routes
# (per-process — fine for a single Render instance;
#  resets on restart/deploy, which is an acceptable
#  trade-off for a small investment platform)
# ─────────────────────────────────────────────
_attempt_log = {}

def rate_limit(max_attempts=8, window_seconds=300):
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if request.method == 'POST':
                ip = request.headers.get('X-Forwarded-For', request.remote_addr or 'unknown').split(',')[0].strip()
                key = f'{request.endpoint}:{ip}'
                now = time.time()
                attempts = [t for t in _attempt_log.get(key, []) if now - t < window_seconds]
                if len(attempts) >= max_attempts:
                    flash('Too many attempts. Please wait a few minutes and try again.', 'error')
                    return render_template(f'{f.__name__}.html'), 429
                attempts.append(now)
                _attempt_log[key] = attempts
                # Occasionally trim old IPs so this dict doesn't grow forever
                if len(_attempt_log) > 5000:
                    cutoff = now - window_seconds
                    for k in list(_attempt_log.keys()):
                        _attempt_log[k] = [t for t in _attempt_log[k] if t > cutoff]
                        if not _attempt_log[k]:
                            del _attempt_log[k]
            return f(*args, **kwargs)
        return decorated
    return decorator


@app.errorhandler(CSRFError)
def handle_csrf_error(e):
    flash('Your session expired or the form was tampered with — please try again.', 'error')
    return redirect(request.referrer or url_for('index')), 400


@app.errorhandler(RequestEntityTooLarge)
def handle_file_too_large(e):
    flash('That file is too large. Maximum upload size is 5MB.', 'error')
    return redirect(request.referrer or url_for('index')), 413

# ─────────────────────────────────────────────
# Public Pages
# ─────────────────────────────────────────────
@app.route('/')
def index():
    try:
        stats = {
            'total_users':        sb_count('users', [('is_admin', 'eq', False)]),
            'total_invested':     sb_sum('investments', 'amount'),
            'active_investments': sb_count('investments', [('status', 'eq', 'active')]),
            'total_paid':         sb_sum('withdrawals', 'amount', [('status', 'eq', 'approved')]),
        }
    except Exception:
        stats = {'total_users': 0, 'total_invested': 0, 'active_investments': 0, 'total_paid': 0}
    return render_template('index.html', stats=stats)

@app.route('/about')
def about():
    return render_template('about.html')

@app.route('/plans')
def plans():
    return render_template('plans.html')

@app.route('/contact')
def contact():
    return render_template('contact.html')

# ─────────────────────────────────────────────
# Authentication
# ─────────────────────────────────────────────
@app.route('/register', methods=['GET', 'POST'])
@rate_limit(max_attempts=10, window_seconds=600)
def register():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))

    ref_code = request.args.get('ref', '')

    if request.method == 'POST':
        full_name = request.form.get('full_name', '').strip()
        email     = request.form.get('email', '').strip().lower()
        phone     = request.form.get('phone', '').strip()
        password  = request.form.get('password', '')
        confirm   = request.form.get('confirm_password', '')
        ref_input = request.form.get('referral_code', '').strip().upper()

        errors = []
        if not full_name or len(full_name) < 3:
            errors.append('Full name must be at least 3 characters.')
        if not email or not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email):
            errors.append('Enter a valid email address.')
        if phone and not re.match(r'^[0-9+\-\s]{7,20}$', phone):
            errors.append('Enter a valid phone number.')
        if len(password) < 8:
            errors.append('Password must be at least 8 characters.')
        if password != confirm:
            errors.append('Passwords do not match.')
        if email and sb_one('users', [('email', 'eq', email)]):
            errors.append('Email already registered.')

        if errors:
            for e in errors:
                flash(e, 'error')
            return render_template('register.html', ref_code=ref_input)

        new_ref    = full_name.upper().replace(' ', '')[:4] + str(uuid.uuid4())[:6].upper()
        referred_by = None
        if ref_input:
            referrer = sb_one('users', [('referral_code', 'eq', ref_input)])
            if referrer:
                referred_by = referrer['id']

        new_user = sb_insert('users', {
            'full_name': full_name, 'email': email, 'phone': phone,
            'password_hash': generate_password_hash(password),
            'referral_code': new_ref, 'referred_by': referred_by,
            'balance': 0, 'total_invested': 0,
            'total_earnings': 0, 'referral_earnings': 0,
            'is_admin': False, 'is_active': True,
        })

        if not new_user:
            flash('We could not create your account right now. Please try again in a moment.', 'error')
            return render_template('register.html', ref_code=ref_input)

        new_id = new_user['id']
        if referred_by:
            sb_insert('referrals', {
                'referrer_id': referred_by, 'referred_id': new_id,
                'commission': 0, 'status': 'pending'
            })
        notify(new_id, f'Welcome to AgroVest, {full_name}! Your account is ready.', 'success')

        flash('Account created! Please log in.', 'success')
        return redirect(url_for('login'))

    return render_template('register.html', ref_code=ref_code)


@app.route('/login', methods=['GET', 'POST'])
@rate_limit(max_attempts=10, window_seconds=600)
def login():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        email    = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        user     = sb_one('users', [('email', 'eq', email)]) if email else None

        if user and check_password_hash(user['password_hash'], password):
            if not user.get('is_active'):
                flash('Your account has been suspended. Contact support.', 'error')
                return render_template('login.html')
            session.clear()
            session.permanent = True
            session['user_id']  = user['id']
            session['is_admin'] = bool(user.get('is_admin'))
            flash(f'Welcome back, {user["full_name"].split()[0]}!', 'success')
            return redirect(url_for('admin_dashboard') if user.get('is_admin') else url_for('dashboard'))
        else:
            flash('Invalid email or password.', 'error')

    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    flash('You have been logged out.', 'info')
    return redirect(url_for('index'))


# ─────────────────────────────────────────────
# User Dashboard
# ─────────────────────────────────────────────
@app.route('/dashboard')
@login_required
def dashboard():
    user = get_current_user()
    uid  = user['id']
    active_investments = sb_all('investments',
        filters=[('user_id','eq',uid),('status','eq','active')],
        order=('created_at','desc'))
    recent_deposits = sb_all('deposits',
        filters=[('user_id','eq',uid)], order=('created_at','desc'), limit=5)
    recent_withdrawals = sb_all('withdrawals',
        filters=[('user_id','eq',uid)], order=('created_at','desc'), limit=5)
    notifications = sb_all('notifications',
        filters=[('user_id','eq',uid)], order=('created_at','desc'), limit=10)

    # Referrals with referred user names
    raw_refs = sb_all('referrals', filters=[('referrer_id','eq',uid)],
                      order=('created_at','desc'))
    referrals = []
    for r in raw_refs:
        referred_user = sb_one('users', [('id','eq',r['referred_id'])])
        referrals.append({**r, 'full_name': referred_user['full_name'] if referred_user else 'Unknown'})

    return render_template('dashboard.html',
        user=user, active_investments=active_investments,
        recent_deposits=recent_deposits, recent_withdrawals=recent_withdrawals,
        referrals=referrals, notifications=notifications)


@app.route('/dashboard/invest', methods=['GET', 'POST'])
@login_required
def invest():
    user      = get_current_user()
    all_plans = get_plans()

    if request.method == 'POST':
        plan_id = request.form.get('plan_id', 0, type=int)
        amount  = r2(request.form.get('amount', 0, type=float))

        plan = sb_one('plans', [('id','eq',plan_id),('is_active','eq',True)])
        if not plan:
            flash('Invalid plan selected.', 'error')
            return redirect(url_for('invest'))

        min_a = float(plan['min_amount'])
        max_a = float(plan['max_amount']) if plan.get('max_amount') else None

        if amount <= 0 or amount < min_a:
            flash(f'Minimum investment for {plan["name"]} is ₦{min_a:,.0f}', 'error')
            return redirect(url_for('invest'))
        if max_a and amount > max_a:
            flash(f'Maximum investment for {plan["name"]} is ₦{max_a:,.0f}', 'error')
            return redirect(url_for('invest'))

        # Atomic debit — fails cleanly if balance is insufficient, even under
        # concurrent requests (e.g. a double-clicked submit button).
        debited = sb_adjust_balance(user['id'], balance_delta=-amount,
                                     total_invested_delta=amount,
                                     require_sufficient_balance=True)
        if not debited:
            flash('Insufficient balance. Please deposit funds first.', 'error')
            return redirect(url_for('deposit'))

        roi_pct         = float(plan['roi_percent'])
        expected_return = r2(amount + (amount * roi_pct / 100))
        end_date        = (datetime.utcnow() + timedelta(days=int(plan['duration_days']))).isoformat()

        new_investment = sb_insert('investments', {
            'user_id': user['id'], 'plan_id': plan_id,
            'plan_name': plan['name'], 'amount': amount,
            'roi_percent': roi_pct, 'expected_return': expected_return,
            'duration_days': int(plan['duration_days']),
            'end_date': end_date, 'status': 'active',
        })
        if not new_investment:
            rolled_back = sb_adjust_balance(user['id'], balance_delta=amount,
                                             total_invested_delta=-amount)
            if not rolled_back:
                print(f'⚠ CRITICAL: investment rollback failed for user {user["id"]}, '
                      f'amount {amount} — balance may be incorrect, investigate manually.')
            flash('We could not activate your investment right now. Please try again.', 'error')
            return redirect(url_for('invest'))

        # Referral commission
        if user.get('referred_by'):
            commission = r2(amount * REFERRAL_COMMISSION / 100)
            referrer   = sb_one('users', [('id','eq',user['referred_by'])])
            if referrer:
                sb_adjust_balance(referrer['id'], balance_delta=commission,
                                   referral_earnings_delta=commission)
                # Update referral record commission
                ref_row = sb_one('referrals', [
                    ('referrer_id','eq',user['referred_by']),
                    ('referred_id','eq',user['id'])
                ])
                if ref_row:
                    sb_update('referrals', {
                        'commission': r2(float(ref_row['commission']) + commission),
                        'status': 'active'
                    }, [('id','eq',ref_row['id'])])
                notify(referrer['id'], f'You earned ₦{commission:,.2f} referral commission!', 'success')

        notify(user['id'], f'Investment of ₦{amount:,.2f} in {plan["name"]} activated!', 'success')
        flash(f'Investment activated! Expected return: ₦{expected_return:,.2f}', 'success')
        return redirect(url_for('dashboard'))

    return render_template('invest.html', user=user, plans=all_plans)


@app.route('/dashboard/deposit', methods=['GET', 'POST'])
@login_required
def deposit():
    user = get_current_user()

    if request.method == 'POST':
        amount         = r2(request.form.get('amount', 0, type=float))
        payment_method = request.form.get('payment_method', '').strip()
        proof          = request.files.get('proof')

        if amount < 1000:
            flash('Minimum deposit is ₦1,000.', 'error')
            return redirect(url_for('deposit'))
        if not payment_method:
            flash('Please select a payment method.', 'error')
            return redirect(url_for('deposit'))

        proof_filename = None
        if proof and proof.filename:
            if not is_allowed_file(proof.filename):
                flash('Only PNG, JPG, JPEG, PDF files allowed.', 'error')
                return redirect(url_for('deposit'))
            proof_filename = upload_proof(proof)

        reference = 'AGV' + uuid.uuid4().hex[:10].upper()
        created = sb_insert('deposits', {
            'user_id': user['id'], 'amount': amount,
            'payment_method': payment_method,
            'proof_filename': proof_filename,
            'reference': reference, 'status': 'pending',
        })
        if not created:
            flash('We could not submit your deposit right now. Please try again.', 'error')
            return redirect(url_for('deposit'))

        notify(user['id'], f'Deposit of ₦{amount:,.2f} submitted. Awaiting confirmation.', 'info')
        flash('Deposit submitted! Confirmed within 30 minutes.', 'success')
        return redirect(url_for('dashboard'))

    deposits = sb_all('deposits', filters=[('user_id','eq',user['id'])],
                      order=('created_at','desc'))
    return render_template('deposit.html', user=user, deposits=deposits)


@app.route('/dashboard/withdraw', methods=['GET', 'POST'])
@login_required
def withdraw():
    user = get_current_user()

    if request.method == 'POST':
        amount         = r2(request.form.get('amount', 0, type=float))
        bank_name      = request.form.get('bank_name', '').strip()
        account_number = request.form.get('account_number', '').strip()
        account_name   = request.form.get('account_name', '').strip()

        if amount < 2000:
            flash('Minimum withdrawal is ₦2,000.', 'error')
            return redirect(url_for('withdraw'))
        if not (bank_name and account_number and account_name):
            flash('Please fill in all bank details.', 'error')
            return redirect(url_for('withdraw'))
        if not re.match(r'^\d{10}$', account_number):
            flash('Account number must be exactly 10 digits.', 'error')
            return redirect(url_for('withdraw'))

        # Atomic debit — protects against double-submitting this form.
        debited = sb_adjust_balance(user['id'], balance_delta=-amount,
                                     require_sufficient_balance=True)
        if not debited:
            flash('Insufficient balance.', 'error')
            return redirect(url_for('withdraw'))

        created = sb_insert('withdrawals', {
            'user_id': user['id'], 'amount': amount,
            'bank_name': bank_name, 'account_number': account_number,
            'account_name': account_name, 'status': 'pending',
        })
        if not created:
            # Roll back the debit since the withdrawal record failed to save
            rolled_back = sb_adjust_balance(user['id'], balance_delta=amount)
            if rolled_back:
                flash('We could not submit your withdrawal right now. Please try again.', 'error')
            else:
                print(f'⚠ CRITICAL: withdrawal rollback failed for user {user["id"]}, '
                      f'amount {amount} — balance may be incorrect, investigate manually.')
                flash('Something went wrong submitting your withdrawal. '
                      'Please contact support before trying again.', 'error')
            return redirect(url_for('withdraw'))

        notify(user['id'], f'Withdrawal of ₦{amount:,.2f} submitted.', 'info')
        flash('Withdrawal submitted! Processing within 24 hours.', 'success')
        return redirect(url_for('dashboard'))

    withdrawals = sb_all('withdrawals', filters=[('user_id','eq',user['id'])],
                         order=('created_at','desc'))
    return render_template('withdraw.html', user=user, withdrawals=withdrawals)


@app.route('/dashboard/notifications')
@login_required
def list_notifications():
    """JSON feed for the notification bell dropdown (see dashboard_base.html)."""
    user = get_current_user()
    rows = sb_all('notifications', filters=[('user_id','eq',user['id'])],
                  order=('created_at','desc'), limit=10)
    return jsonify({'notifications': [{
        'id': n['id'],
        'message': n['message'],
        'type': n.get('type', 'info'),
        'is_read': bool(n.get('is_read')),
        'created_at': date_fmt(n.get('created_at'), '%d %b, %I:%M %p'),
    } for n in rows]})


@app.route('/dashboard/notifications/read', methods=['POST'])
@login_required
def mark_notifications_read():
    user = get_current_user()
    sb_update('notifications', {'is_read': True}, [('user_id','eq',user['id'])])
    return jsonify({'status': 'ok'})


# ═════════════════════════════════════════════
# ADMIN — Dashboard
# ═════════════════════════════════════════════
@app.route('/admin')
@admin_required
def admin_dashboard():
    try:
        stats = {
            'total_users':         sb_count('users',       [('is_admin','eq',False)]),
            'total_invested':      sb_sum('investments',   'amount'),
            'pending_deposits':    sb_count('deposits',    [('status','eq','pending')]),
            'pending_withdrawals': sb_count('withdrawals', [('status','eq','pending')]),
            'total_paid_out':      sb_sum('withdrawals',   'amount', [('status','eq','approved')]),
            'active_investments':  sb_count('investments', [('status','eq','active')]),
            'total_plans':         sb_count('plans'),
        }
    except Exception as e:
        print(f'Admin stats error: {e}')
        stats = {k: 0 for k in ['total_users','total_invested','pending_deposits',
                                  'pending_withdrawals','total_paid_out',
                                  'active_investments','total_plans']}
    try:
        recent_users = sb_all('users', filters=[('is_admin','eq',False)],
                              order=('created_at','desc'), limit=10)
    except Exception:
        recent_users = []
    try:
        pending_deps = _join_users(
            sb_all('deposits', filters=[('status','eq','pending')],
                   order=('created_at','desc')))
    except Exception:
        pending_deps = []
    try:
        pending_wds = _join_users(
            sb_all('withdrawals', filters=[('status','eq','pending')],
                   order=('created_at','desc')))
    except Exception:
        pending_wds = []
    return render_template('admin/dashboard.html',
        stats=stats, recent_users=recent_users,
        pending_deps=pending_deps, pending_wds=pending_wds)


def _join_users(rows):
    """Enrich rows with full_name and email from users table."""
    if not rows:
        return []
    # Batch: collect unique user_ids and fetch them all at once
    user_ids = list({r.get('user_id') for r in rows if r.get('user_id')})
    users_map = {}
    if user_ids:
        try:
            q = get_sb().table('users').select('id,full_name,email')
            q = q.in_('id', user_ids)
            r = q.execute()
            for u in (r.data or []):
                users_map[u['id']] = u
        except Exception as e:
            print(f'_join_users fetch error: {e}')
    result = []
    for row in rows:
        u = users_map.get(row.get('user_id'), {})
        result.append({**row,
            'full_name': u.get('full_name', 'Unknown'),
            'email':     u.get('email', '—')})
    return result


# ═════════════════════════════════════════════
# ADMIN — Plans CRUD
# ═════════════════════════════════════════════
@app.route('/admin/plans')
@admin_required
def admin_plans():
    all_plans = sb_all('plans', order=[('sort_order','asc'),('id','asc')])
    return render_template('admin/plans.html', plans=all_plans)


@app.route('/admin/plans/add', methods=['GET', 'POST'])
@admin_required
def admin_plan_add():
    if request.method == 'POST':
        data, error = _plan_from_form(request.form)
        if error:
            flash(error, 'error')
            return render_template('admin/plan_form.html', plan=None, action='add')
        if sb_one('plans', [('slug','eq', data['slug'])]):
            flash('A plan with that slug already exists.', 'error')
            return render_template('admin/plan_form.html', plan=None, action='add')
        sb_insert('plans', data)
        flash(f'Plan "{data["name"]}" created!', 'success')
        return redirect(url_for('admin_plans'))
    return render_template('admin/plan_form.html', plan=None, action='add')


@app.route('/admin/plans/<int:plan_id>/edit', methods=['GET', 'POST'])
@admin_required
def admin_plan_edit(plan_id):
    plan = sb_one('plans', [('id','eq',plan_id)])
    if not plan:
        flash('Plan not found.', 'error')
        return redirect(url_for('admin_plans'))

    if request.method == 'POST':
        data, error = _plan_from_form(request.form)
        if error:
            flash(error, 'error')
            plan_d = dict(plan)
            plan_d['features_text'] = (plan_d.get('features') or '').replace('|','\n')
            return render_template('admin/plan_form.html', plan=plan_d, action='edit')

        # Check slug uniqueness (exclude self)
        existing = sb_all('plans', filters=[('slug','eq',data['slug'])])
        clash = [p for p in existing if p['id'] != plan_id]
        if clash:
            flash('That slug is used by another plan.', 'error')
            return redirect(url_for('admin_plan_edit', plan_id=plan_id))

        sb_update('plans', data, [('id','eq',plan_id)])
        flash(f'Plan "{data["name"]}" updated!', 'success')
        return redirect(url_for('admin_plans'))

    plan_d = dict(plan)
    plan_d['features_text'] = (plan_d.get('features') or '').replace('|','\n')
    return render_template('admin/plan_form.html', plan=plan_d, action='edit')


def _plan_from_form(form):
    """Parse and validate plan form. Returns (data_dict, error_str)."""
    name          = form.get('name','').strip()
    slug          = form.get('slug','').strip().lower().replace(' ','-')
    icon          = form.get('icon','🌱').strip() or '🌱'
    description   = form.get('description','').strip()
    min_amount    = r2(form.get('min_amount', 0, type=float))
    max_amount    = form.get('max_amount', '').strip()
    roi_percent   = r2(form.get('roi_percent', 0, type=float))
    duration_days = form.get('duration_days', 30, type=int)
    features_raw  = form.get('features','').strip()
    sort_order    = form.get('sort_order', 0, type=int)
    is_active     = form.get('is_active') == 'on'

    if not name or not slug:
        return None, 'Name and slug are required.'
    if not re.match(r'^[a-z0-9-]+$', slug):
        return None, 'Slug may only contain lowercase letters, numbers and hyphens.'
    if roi_percent <= 0:
        return None, 'ROI % must be greater than 0.'
    if min_amount <= 0:
        return None, 'Minimum amount must be greater than 0.'
    if duration_days <= 0:
        return None, 'Duration must be at least 1 day.'

    max_amt = None
    if max_amount:
        try:
            max_amt = r2(float(max_amount))
        except ValueError:
            return None, 'Maximum amount must be a valid number.'
        if max_amt <= min_amount:
            return None, 'Maximum amount must be greater than the minimum amount.'

    features = '|'.join([f.strip() for f in features_raw.splitlines() if f.strip()])

    return {
        'name': name, 'slug': slug, 'icon': icon,
        'description': description, 'min_amount': min_amount,
        'max_amount': max_amt, 'roi_percent': roi_percent,
        'duration_days': duration_days, 'features': features,
        'sort_order': sort_order, 'is_active': is_active,
    }, None


@app.route('/admin/plans/<int:plan_id>/toggle', methods=['POST'])
@admin_required
def admin_plan_toggle(plan_id):
    plan = sb_one('plans', [('id','eq',plan_id)])
    if plan:
        new_status = not bool(plan['is_active'])
        sb_update('plans', {'is_active': new_status}, [('id','eq',plan_id)])
        flash(f'Plan {"activated" if new_status else "deactivated"}.', 'success')
    return redirect(url_for('admin_plans'))


@app.route('/admin/plans/<int:plan_id>/delete', methods=['POST'])
@admin_required
def admin_plan_delete(plan_id):
    active = sb_count('investments', [('plan_id','eq',plan_id),('status','eq','active')])
    if active > 0:
        flash(f'Cannot delete — {active} active investment(s) use this plan. Deactivate it instead.', 'error')
        return redirect(url_for('admin_plans'))
    plan = sb_one('plans', [('id','eq',plan_id)])
    sb_delete('plans', [('id','eq',plan_id)])
    flash(f'Plan "{plan["name"] if plan else plan_id}" deleted.', 'success')
    return redirect(url_for('admin_plans'))


# ═════════════════════════════════════════════
# ADMIN — Users CRUD
# ═════════════════════════════════════════════
@app.route('/admin/users')
@admin_required
def admin_users():
    try:
        users = sb_all('users', filters=[('is_admin','eq',False)],
                       order=('created_at','desc'))
    except Exception:
        users = []

    # Batch-fetch all investments and referrals, count in Python
    try:
        all_investments = sb_all('investments')
        all_referrals   = sb_all('referrals')
    except Exception:
        all_investments, all_referrals = [], []

    inv_map = {}
    for inv in all_investments:
        uid = inv.get('user_id')
        inv_map[uid] = inv_map.get(uid, 0) + 1
    ref_map = {}
    for ref in all_referrals:
        uid = ref.get('referrer_id')
        ref_map[uid] = ref_map.get(uid, 0) + 1

    enriched = [{**u, 'inv_count': inv_map.get(u['id'], 0),
                      'ref_count': ref_map.get(u['id'], 0)} for u in users]
    return render_template('admin/users.html', users=enriched)


@app.route('/admin/users/<int:user_id>/edit', methods=['GET', 'POST'])
@admin_required
def admin_user_edit(user_id):
    user = sb_one('users', [('id','eq',user_id)])
    if not user:
        flash('User not found.', 'error')
        return redirect(url_for('admin_users'))

    if request.method == 'POST':
        full_name    = request.form.get('full_name','').strip()
        email        = request.form.get('email','').strip().lower()
        phone        = request.form.get('phone','').strip()
        balance      = r2(request.form.get('balance', 0, type=float))
        is_active    = request.form.get('is_active') == 'on'
        is_admin     = request.form.get('is_admin') == 'on'
        new_password = request.form.get('new_password','').strip()

        if not full_name or len(full_name) < 3:
            flash('Full name must be at least 3 characters.', 'error')
            return render_template('admin/user_form.html', user=dict(user))
        if not email or not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email):
            flash('Enter a valid email address.', 'error')
            return render_template('admin/user_form.html', user=dict(user))

        # Email uniqueness check
        existing = sb_all('users', filters=[('email','eq',email)])
        if any(u['id'] != user_id for u in existing):
            flash('Email already used by another account.', 'error')
            return render_template('admin/user_form.html', user=dict(user))

        # Don't let an admin lock themselves out of their own account
        if user_id == session.get('user_id') and (not is_admin or not is_active):
            flash('You cannot remove your own admin access or deactivate your own account.', 'error')
            return render_template('admin/user_form.html', user=dict(user))

        update_data = {
            'full_name': full_name, 'email': email, 'phone': phone,
            'balance': balance, 'is_active': is_active, 'is_admin': is_admin,
        }
        if new_password:
            if len(new_password) < 8:
                flash('Password must be at least 8 characters.', 'error')
                return render_template('admin/user_form.html', user=dict(user))
            update_data['password_hash'] = generate_password_hash(new_password)

        if sb_update('users', update_data, [('id','eq',user_id)]):
            notify(user_id, 'Your account details have been updated by admin.', 'info')
            flash(f'User "{full_name}" updated.', 'success')
        else:
            flash('Could not update user — please try again.', 'error')
        return redirect(url_for('admin_users'))

    return render_template('admin/user_form.html', user=dict(user))


@app.route('/admin/users/<int:user_id>/toggle', methods=['POST'])
@admin_required
def admin_toggle_user(user_id):
    if user_id == session.get('user_id'):
        flash('You cannot suspend your own account.', 'error')
        return redirect(url_for('admin_users'))
    user = sb_one('users', [('id','eq',user_id)])
    if user:
        new_status = not bool(user['is_active'])
        sb_update('users', {'is_active': new_status}, [('id','eq',user_id)])
        flash(f'User {"activated" if new_status else "suspended"}.', 'success')
    return redirect(url_for('admin_users'))


@app.route('/admin/users/<int:user_id>/credit', methods=['POST'])
@admin_required
def admin_credit_user(user_id):
    amount = r2(request.form.get('amount', 0, type=float))
    note   = request.form.get('note', 'Admin credit').strip()
    if amount > 0:
        user = sb_one('users', [('id','eq',user_id)])
        if user:
            if sb_adjust_balance(user_id, balance_delta=amount):
                notify(user_id, f'Your account was credited ₦{amount:,.2f}. {note}', 'success')
                flash(f'₦{amount:,.2f} credited.', 'success')
            else:
                flash('Could not credit user — please try again.', 'error')
        else:
            flash('User not found.', 'error')
    else:
        flash('Enter an amount greater than zero.', 'error')
    return redirect(url_for('admin_users'))


@app.route('/admin/users/<int:user_id>/delete', methods=['POST'])
@admin_required
def admin_user_delete(user_id):
    user = sb_one('users', [('id','eq',user_id)])
    if not user:
        flash('User not found.', 'error')
        return redirect(url_for('admin_users'))
    if user.get('is_admin'):
        flash('Cannot delete admin accounts.', 'error')
        return redirect(url_for('admin_users'))
    active_inv = sb_count('investments', [('user_id','eq',user_id),('status','eq','active')])
    if active_inv > 0:
        flash(f'Cannot delete — {active_inv} active investment(s). Suspend instead.', 'error')
        return redirect(url_for('admin_users'))

    for tbl, col in [('notifications','user_id'),('deposits','user_id'),
                     ('withdrawals','user_id'),('investments','user_id'),
                     ('referrals','referrer_id'),('referrals','referred_id')]:
        try:
            sb_delete(tbl, [(col,'eq',user_id)])
        except Exception:
            pass

    sb_delete('users', [('id','eq',user_id)])
    flash(f'User "{user["full_name"]}" deleted permanently.', 'success')
    return redirect(url_for('admin_users'))


# ═════════════════════════════════════════════
# ADMIN — Deposits
# ═════════════════════════════════════════════
@app.route('/admin/deposits')
@admin_required
def admin_deposits():
    deposits = _join_users(sb_all('deposits', order=('created_at','desc')))
    for d in deposits:
        d['proof_url'] = resolve_proof_url(d.get('proof_filename'))
    return render_template('admin/deposits.html', deposits=deposits)


@app.route('/admin/deposits/<int:dep_id>/approve', methods=['POST'])
@admin_required
def admin_approve_deposit(dep_id):
    dep = sb_one('deposits', [('id','eq',dep_id)])
    if dep and dep['status'] == 'pending':
        amount = r2(dep['amount'])
        updated = sb_update('deposits', {'status':'approved'},
                             [('id','eq',dep_id), ('status','eq','pending')])
        if updated:
            if sb_adjust_balance(dep['user_id'], balance_delta=amount):
                notify(dep['user_id'], f'Your deposit of ₦{amount:,.2f} has been approved!', 'success')
                flash('Deposit approved.', 'success')
            else:
                flash('Deposit marked approved, but crediting the balance failed — '
                      'please credit the user manually and check the logs.', 'error')
        else:
            flash('This deposit was already processed.', 'warning')
    return redirect(url_for('admin_deposits'))


@app.route('/admin/deposits/<int:dep_id>/reject', methods=['POST'])
@admin_required
def admin_reject_deposit(dep_id):
    note = request.form.get('note', 'Rejected by admin').strip() or 'Rejected by admin'
    dep  = sb_one('deposits', [('id','eq',dep_id)])
    if dep and dep['status'] == 'pending':
        updated = sb_update('deposits', {'status':'rejected','admin_note':note},
                             [('id','eq',dep_id), ('status','eq','pending')])
        if updated:
            notify(dep['user_id'], f'Deposit of ₦{r2(dep["amount"]):,.2f} rejected. Reason: {note}', 'error')
            flash('Deposit rejected.', 'warning')
        else:
            flash('This deposit was already processed.', 'warning')
    return redirect(url_for('admin_deposits'))


# ═════════════════════════════════════════════
# ADMIN — Withdrawals
# ═════════════════════════════════════════════
@app.route('/admin/withdrawals')
@admin_required
def admin_withdrawals():
    withdrawals = _join_users(sb_all('withdrawals', order=('created_at','desc')))
    return render_template('admin/withdrawals.html', withdrawals=withdrawals)


@app.route('/admin/withdrawals/<int:wd_id>/approve', methods=['POST'])
@admin_required
def admin_approve_withdrawal(wd_id):
    wd = sb_one('withdrawals', [('id','eq',wd_id)])
    if wd and wd['status'] == 'pending':
        amount = r2(wd['amount'])
        updated = sb_update('withdrawals', {'status':'approved'},
                             [('id','eq',wd_id), ('status','eq','pending')])
        if updated:
            if sb_adjust_balance(wd['user_id'], total_earnings_delta=amount):
                notify(wd['user_id'], f'Withdrawal of ₦{amount:,.2f} approved and sent!', 'success')
                flash('Withdrawal approved.', 'success')
            else:
                flash('Withdrawal marked approved, but updating earnings failed — '
                      'please check the logs.', 'error')
        else:
            flash('This withdrawal was already processed.', 'warning')
    return redirect(url_for('admin_withdrawals'))


@app.route('/admin/withdrawals/<int:wd_id>/reject', methods=['POST'])
@admin_required
def admin_reject_withdrawal(wd_id):
    note = request.form.get('note', 'Rejected').strip() or 'Rejected'
    wd   = sb_one('withdrawals', [('id','eq',wd_id)])
    if wd and wd['status'] == 'pending':
        amount = r2(wd['amount'])
        updated = sb_update('withdrawals', {'status':'rejected','admin_note':note},
                             [('id','eq',wd_id), ('status','eq','pending')])
        if updated:
            if sb_adjust_balance(wd['user_id'], balance_delta=amount):
                notify(wd['user_id'], f'Withdrawal of ₦{amount:,.2f} rejected. Refunded. Reason: {note}', 'warning')
                flash('Withdrawal rejected and balance refunded.', 'warning')
            else:
                flash('Withdrawal marked rejected, but the refund failed — '
                      'please refund the user manually and check the logs.', 'error')
        else:
            flash('This withdrawal was already processed.', 'warning')
    return redirect(url_for('admin_withdrawals'))


# ═════════════════════════════════════════════
# ADMIN — Investments
# ═════════════════════════════════════════════
@app.route('/admin/investments')
@admin_required
def admin_investments():
    investments = _join_users(sb_all('investments', order=('created_at','desc')))
    return render_template('admin/investments.html', investments=investments)


@app.route('/admin/investments/<int:inv_id>/complete', methods=['POST'])
@admin_required
def admin_complete_investment(inv_id):
    inv = sb_one('investments', [('id','eq',inv_id)])
    if inv and inv['status'] == 'active':
        expected_return = r2(inv['expected_return'])
        profit = r2(expected_return - float(inv['amount']))
        updated = sb_update('investments', {'status':'completed'},
                             [('id','eq',inv_id), ('status','eq','active')])
        if updated:
            if sb_adjust_balance(inv['user_id'], balance_delta=expected_return,
                                  total_earnings_delta=profit):
                notify(inv['user_id'],
                       f'{inv["plan_name"]} matured! ₦{expected_return:,.2f} credited.',
                       'success')
                flash('Investment completed and balance credited.', 'success')
            else:
                flash('Investment marked completed, but crediting the balance failed — '
                      'please credit the user manually and check the logs.', 'error')
        else:
            flash('This investment was already processed.', 'warning')
    return redirect(url_for('admin_investments'))


# ═════════════════════════════════════════════
# Debug Route (admin only — check Render logs)
# ═════════════════════════════════════════════
@app.route('/admin/debug')
@admin_required
def admin_debug():
    """Shows live connection status and any errors. Check Render logs for detail."""
    results = {}
    url = os.environ.get('SUPABASE_URL', '').strip()
    key = os.environ.get('SUPABASE_KEY', '').strip()
    results['supabase_url'] = (url[:40] + '...') if url else 'NOT SET'
    results['supabase_key_set'] = bool(key)
    results['is_render'] = IS_RENDER
    results['use_supabase_storage'] = USE_SUPABASE_STORAGE
    try:
        results['users_count']    = sb_count('users')
        results['plans_count']    = sb_count('plans')
        results['deposits_count'] = sb_count('deposits')
        results['status'] = 'OK - Database connection working'
    except Exception as e:
        import traceback
        results['error'] = str(e)
        results['traceback'] = traceback.format_exc()
        results['status'] = 'ERROR'
    return jsonify(results)

# ═════════════════════════════════════════════
# Error Handlers
# ═════════════════════════════════════════════
@app.errorhandler(403)
def forbidden(e):
    return render_template('errors/403.html'), 403

@app.errorhandler(404)
def not_found(e):
    return render_template('errors/404.html'), 404

@app.errorhandler(500)
def server_error(e):
    return render_template('errors/500.html'), 500


# ═════════════════════════════════════════════
# Template Filters
# ═════════════════════════════════════════════
@app.template_filter('naira')
def naira_filter(value):
    try:
        return f'₦{float(value or 0):,.2f}'
    except (TypeError, ValueError):
        return '₦0.00'

@app.template_filter('date_fmt')
def date_fmt(value, fmt='%d %b %Y'):
    if not value:
        return '—'
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace('Z','+00:00'))
        except Exception:
            return value
    try:
        return value.strftime(fmt)
    except Exception:
        return str(value)

@app.template_filter('status_badge')
def status_badge(status):
    return {
        'pending':   'badge-warning',
        'approved':  'badge-success',
        'rejected':  'badge-error',
        'active':    'badge-info',
        'completed': 'badge-success',
    }.get(str(status or '').lower(), 'badge-neutral')


# ═════════════════════════════════════════════
# Startup — only seed DB if env vars are present
# ═════════════════════════════════════════════
with app.app_context():
    url = os.environ.get('SUPABASE_URL', '').strip()
    key = os.environ.get('SUPABASE_KEY', '').strip()
    if url and key and url.startswith('https://'):
        try:
            init_db()
            print("✓ AgroVest Pro started with Supabase connection")
        except Exception as _e:
            print(f"⚠ DB seed skipped (tables may not exist yet — run supabase_setup.sql): {_e}")
    else:
        print("⚠ SUPABASE_URL / SUPABASE_KEY not set — visit /setup for instructions")

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=5000)
