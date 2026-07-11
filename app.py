"""
AgroVest Pro - Agricultural Investment Platform
Backend: Flask + Supabase (supabase-py client — pure Python, no C extensions)
"""

import os
import re
import time
import uuid
import secrets
from datetime import datetime, timedelta, timezone
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
PLAN_IMAGE_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp'}
USE_SUPABASE_STORAGE = os.environ.get('USE_SUPABASE_STORAGE', '').strip().lower() == 'true'
SUPABASE_STORAGE_BUCKET = os.environ.get('SUPABASE_STORAGE_BUCKET', 'deposit-proofs').strip()
CONTACT_ATTACHMENTS_BUCKET = os.environ.get('CONTACT_ATTACHMENTS_BUCKET', 'contact-attachments').strip()
PLAN_IMAGES_BUCKET = os.environ.get('PLAN_IMAGES_BUCKET', 'plan-images').strip()
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

def notify(user_id, message, ntype='info', title=None):
    """Insert one notification row. `title` is optional and backward-compatible —
    every existing call site (which passes no title) behaves exactly as before.
    Requires a nullable `title` text column on `notifications`:
        alter table notifications add column if not exists title text;
    """
    payload = {'user_id': user_id, 'message': message, 'type': ntype}
    if title:
        payload['title'] = title
    sb_insert('notifications', payload)


def notify_bulk(user_ids, message, ntype='info'):
    """Insert one notification row per user in a single batched request —
    used for admin broadcast notifications so sending to every user costs
    one round trip instead of N calls to notify()."""
    user_ids = list(user_ids or [])
    if not user_ids:
        return 0
    rows = [{'user_id': uid, 'message': message, 'type': ntype} for uid in user_ids]
    def _run():
        r = get_sb().table('notifications').insert(rows).execute()
        return len(r.data or [])
    ok, result = _with_retry(_run, 'notify_bulk', retries=0)
    return result if ok else 0


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


# ─────────────────────────────────────────────
# Automatic Daily Profit Distribution
# ─────────────────────────────────────────────
def _parse_dt(value):
    """Parse a Supabase timestamp string (or datetime) into a naive UTC datetime."""
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace('Z', '+00:00')).replace(tzinfo=None)


def get_daily_profit_amount(investment):
    """The fixed daily profit for one investment. Uses the stored daily_profit
    if present (set at investment time from the plan), otherwise derives it
    from the investment's stored profit (or expected_return - amount) spread
    evenly across its duration. Falls back to the legacy ROI%-based formula
    only for very old rows created before the total_return-based plan model
    existed — this keeps historic investments working correctly."""
    stored = investment.get('daily_profit')
    if stored not in (None, ''):
        return r2(stored)

    duration = int(investment.get('duration_days') or 1) or 1
    amount   = float(investment.get('amount') or 0)

    profit = investment.get('profit')
    if profit not in (None, ''):
        return r2(float(profit) / duration)

    expected_return = investment.get('expected_return')
    if expected_return not in (None, ''):
        return r2((float(expected_return) - amount) / duration)

    # Legacy fallback for rows predating the total_return-based plan model.
    roi_pct = float(investment.get('roi_percent') or 0)
    return r2((amount * roi_pct / 100) / duration)


def _record_roi_transaction(user_id, investment_id, plan_name, amount):
    """Insert one row into the transactions table for a daily-profit credit.

    Reuses sb_insert() — the same helper used everywhere else in the codebase.
    A failed insert is logged but never allowed to abort the profit credit
    itself, so a transactions-table outage can never prevent users being paid.

    Table schema expected (create once in Supabase SQL editor if not present):
        create table if not exists transactions (
            id               bigserial primary key,
            user_id          bigint       not null,
            investment_id    bigint,
            plan_name        text,
            amount           numeric      not null,
            transaction_type text         not null default 'ROI',
            status           text         not null default 'Completed',
            description      text,
            created_at       timestamptz  not null default now()
        );
    """
    try:
        sb_insert('transactions', {
            'user_id':          user_id,
            'investment_id':    investment_id,
            'plan_name':        plan_name,
            'amount':           r2(amount),
            'transaction_type': 'ROI',
            'status':           'Completed',
            'description':      f'Daily profit credited from {plan_name}',
        })
    except Exception as e:
        print(f'⚠ ROI transaction record failed (inv {investment_id}): {e}')


def sb_credit_daily_profit(investment_id, user_id, credit_amount, new_last_profit_date,
                            mark_completed=False, principal_return=0):
    """Atomically credit accumulated daily profit (and, if the investment has
    matured, the principal) via a Postgres RPC function. The function takes a
    row lock on the investment and only applies the update if the investment
    is still 'active', which makes this safe to call repeatedly/concurrently
    (e.g. from a cron job and a dashboard page-load at the same moment)
    without ever double-crediting a user.

    Returns True if a credit was applied, False otherwise (already processed,
    investment not active, or the RPC call failed).
    """
    def _run():
        r = get_sb().rpc('agrovest_credit_daily_profit', {
            'p_investment_id': investment_id,
            'p_user_id': user_id,
            'p_credit_amount': r2(credit_amount),
            'p_new_last_profit_date': new_last_profit_date,
            'p_mark_completed': bool(mark_completed),
            'p_principal_return': r2(principal_return),
        }).execute()
        data = r.data
        if isinstance(data, list):
            data = data[0] if data else False
        return bool(data)
    ok, result = _with_retry(_run, 'sb_credit_daily_profit', retries=0)
    return result if ok else False


def process_investment_daily_profit(inv):
    """Credit any owed daily profit for a single active investment, covering
    every missed day since it was last credited (e.g. if the user/cron hasn't
    visited in 4 days, all 4 days are credited in one shot). Never credits
    past the investment's end_date. Marks the investment 'completed' and
    returns the principal once end_date is reached, and never credits profit
    again afterwards.

    After every successful credit:
      • Records a transactions row (type='ROI', status='Completed') so the
        credit appears immediately in the user's Recent Transactions page.
      • Sends a wallet notification so the user sees the exact amount credited.
      • On maturity, sends a separate maturity notification.

    Safety guarantees (unchanged from the original):
      • The underlying Postgres RPC takes a row lock — concurrent calls from
        cron + a dashboard visit at the same moment cannot double-credit.
      • elapsed_days is based on calendar dates, so calling this more than
        once per day simply finds elapsed_days == 0 and returns False.
      • Inactive / completed / cancelled investments are rejected immediately.
    """
    if inv.get('status') != 'active':
        return False

    end_date = _parse_dt(inv.get('end_date'))
    if not end_date:
        return False

    last_profit_date = _parse_dt(inv.get('last_profit_date')) or \
                        _parse_dt(inv.get('start_date')) or \
                        _parse_dt(inv.get('created_at')) or datetime.utcnow()

    now = datetime.utcnow()
    effective_now = min(now, end_date)
    elapsed_days  = (effective_now - last_profit_date).days

    matured = now >= end_date
    if elapsed_days <= 0 and not matured:
        return False  # not even 1 full day has passed yet — nothing owed

    daily_profit  = get_daily_profit_amount(inv)
    credit_amount = r2(daily_profit * elapsed_days)
    new_last_date = (last_profit_date + timedelta(days=elapsed_days)).isoformat()

    if credit_amount <= 0 and not matured:
        return False

    principal_return = r2(inv['amount']) if matured else 0

    credited = sb_credit_daily_profit(
        investment_id=inv['id'], user_id=inv['user_id'],
        credit_amount=credit_amount, new_last_profit_date=new_last_date,
        mark_completed=matured, principal_return=principal_return,
    )

    if not credited:
        # RPC returned False → investment already processed or not active.
        # Not an error — idempotent by design.
        return False

    plan_name = inv.get('plan_name', 'your investment')

    # ── Transaction record ───────────────────────────────────────────────
    # One ROI transaction per credit run. For catch-up runs (multiple missed
    # days), we record the combined amount in a single row rather than one
    # row per day — this keeps the transactions table lean and the
    # description accurate ("₦2,700 for 3 days" is clearer than 3 × "₦900").
    if credit_amount > 0:
        _record_roi_transaction(
            user_id=inv['user_id'],
            investment_id=inv['id'],
            plan_name=plan_name,
            amount=credit_amount,
        )

    # ── User notifications ───────────────────────────────────────────────
    if credit_amount > 0:
        notify(
            inv['user_id'],
            f'₦{credit_amount:,.2f} daily profit has been credited to your wallet '
            f'from your {plan_name} investment.',
            'success',
            title='Daily Profit Credited',
        )

    if matured:
        notify(
            inv['user_id'],
            f'Your {plan_name} investment has matured — your principal of '
            f'₦{principal_return:,.2f} has been returned to your wallet.',
            'success',
        )

    return True


def run_daily_profit_distribution(user_id=None):
    """Process daily profit for all active investments (optionally scoped to
    one user). Safe to call as often as needed — already-credited investments
    are simply skipped (elapsed_days == 0 → early return in
    process_investment_daily_profit). Call sites:

      1. Lazily on every dashboard page-load (per-user) — self-healing even
         if the external cron hasn't fired.
      2. From POST /cron/daily-profits (global) — the authoritative daily run.

    Errors for individual investments are caught, logged, and do NOT abort
    processing of the remaining investments.
    """
    filters = [('status', 'eq', 'active')]
    if user_id is not None:
        filters.append(('user_id', 'eq', user_id))

    investments = sb_all('investments', filters=filters)
    processed = 0

    for inv in investments:
        try:
            if process_investment_daily_profit(inv):
                processed += 1
        except Exception as e:
            print(f'⚠ daily profit error for investment {inv.get("id")} '
                  f'(user {inv.get("user_id")}): {e}')

    return processed


def _enrich_investment_progress(inv):
    """Add remaining_days / progress_percent to an investment dict for display."""
    inv = dict(inv)
    try:
        start = _parse_dt(inv.get('start_date') or inv.get('created_at'))
        end   = _parse_dt(inv.get('end_date'))
        now   = datetime.utcnow()
        total_days = max((end - start).days, 1)
        elapsed    = max((min(now, end) - start).days, 0)
        inv['remaining_days']   = max((end - now).days, 0)
        inv['progress_percent'] = round(min(elapsed / total_days * 100, 100), 1)
        inv['daily_profit']     = get_daily_profit_amount(inv)
        inv['total_profit']     = r2(inv.get('total_profit'))
        stored_profit = inv.get('profit')
        inv['profit'] = r2(stored_profit) if stored_profit not in (None, '') else \
            r2(float(inv.get('expected_return') or 0) - float(inv.get('amount') or 0))
    except Exception:
        inv['remaining_days'], inv['progress_percent'] = 0, 0
    return inv


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

def upload_contact_attachment(file_storage):
    """Save an uploaded contact-support attachment (receipt/screenshot/PDF).
    Same Supabase Storage → local-disk fallback pattern as upload_proof().
    Returns the value to store in contact_messages.attachment, or None.
    """
    ext = file_storage.filename.rsplit('.', 1)[-1].lower()
    stored_name = f"contact_{uuid.uuid4().hex}.{ext}"

    if USE_SUPABASE_STORAGE:
        try:
            content_type = {
                'png': 'image/png', 'jpg': 'image/jpeg',
                'jpeg': 'image/jpeg', 'pdf': 'application/pdf',
            }.get(ext, 'application/octet-stream')
            file_bytes = file_storage.read()
            get_sb().storage.from_(CONTACT_ATTACHMENTS_BUCKET).upload(
                stored_name, file_bytes, file_options={'content-type': content_type})
            return f"sb:{stored_name}"
        except Exception as e:
            print(f"⚠ Supabase Storage contact-attachment upload failed, falling back to local disk: {e}")
            try:
                file_storage.stream.seek(0)
            except Exception:
                pass

    file_storage.save(os.path.join(app.config['UPLOAD_FOLDER'], stored_name))
    return stored_name


def resolve_contact_attachment_url(attachment_filename):
    """Turn a stored contact_messages.attachment value into a viewable URL (or None)."""
    if not attachment_filename:
        return None
    if attachment_filename.startswith('sb:'):
        path = attachment_filename[3:]
        try:
            res = get_sb().storage.from_(CONTACT_ATTACHMENTS_BUCKET).create_signed_url(path, 3600)
            return res.get('signedURL') or res.get('signedUrl') or res.get('signed_url')
        except Exception as e:
            print(f'resolve_contact_attachment_url error: {e}')
            return None
    return url_for('uploaded_file', filename=attachment_filename)


def notify_admins(message, ntype='info'):
    """Fan a notification out to every admin user. Reuses the existing
    notify()/notifications table — just targets every is_admin=True user_id
    instead of a single one, so the current admin bell/notification UI picks
    it up with zero changes.
    """
    try:
        admins = sb_all('users', filters=[('is_admin', 'eq', True)])
        for admin in admins:
            notify(admin['id'], message, ntype)
    except Exception as e:
        print(f'notify_admins error: {e}')


def send_contact_confirmation_email(to_email, full_name):
    """Send the 'We've received your message' confirmation email.

    NOTE: app.py has no existing email-sending integration (no SMTP/provider
    configured anywhere in the codebase), so per your instructions this is a
    safe no-op placeholder rather than a new email system — it does not send
    anything yet. Once you add a provider (SMTP, Resend, SendGrid, etc.),
    fill this in; the route below already calls it with the right subject.
    """
    subject = "We've received your message"
    print(f"[contact] would send '{subject}' to {to_email} for {full_name} "
          f"— no email provider configured yet.")
    return False


def get_contact_stats():
    """Stats for the future admin support inbox."""
    return {
        'total_messages':   sb_count('contact_messages'),
        'unread_messages':  sb_count('contact_messages', [('status', 'eq', 'Unread')]),
        'read_messages':    sb_count('contact_messages', [('status', 'eq', 'Read')]),
        'replied_messages': sb_count('contact_messages', [('status', 'eq', 'Replied')]),
    }


def get_recent_contact_messages(limit=10):
    """Recent messages for the future admin support inbox."""
    return sb_all('contact_messages', order=('created_at', 'desc'), limit=limit)


def is_allowed_plan_image(filename):
    return '.' in filename and filename.rsplit('.', 1)[-1].lower() in PLAN_IMAGE_EXTENSIONS


def upload_plan_image(file_storage):
    """Save an uploaded plan image. Same fallback behavior as upload_proof():
    tries Supabase Storage (public bucket — these are marketing images, not
    sensitive) if USE_SUPABASE_STORAGE is on, otherwise/falls back to local
    disk. Returns the value to store in plans.image_filename, or None."""
    ext = file_storage.filename.rsplit('.', 1)[-1].lower()
    stored_name = f"plan_{uuid.uuid4().hex}.{ext}"

    if USE_SUPABASE_STORAGE:
        try:
            content_type = {
                'png': 'image/png', 'jpg': 'image/jpeg',
                'jpeg': 'image/jpeg', 'webp': 'image/webp',
            }.get(ext, 'application/octet-stream')
            file_bytes = file_storage.read()
            get_sb().storage.from_(PLAN_IMAGES_BUCKET).upload(
                stored_name, file_bytes, file_options={'content-type': content_type})
            return f"sb:{stored_name}"
        except Exception as e:
            print(f"⚠ Supabase Storage plan-image upload failed, falling back to local disk: {e}")
            try:
                file_storage.stream.seek(0)
            except Exception:
                pass

    file_storage.save(os.path.join(app.config['UPLOAD_FOLDER'], stored_name))
    return stored_name


def resolve_plan_image_url(image_filename):
    """Turn a stored plans.image_filename value into a viewable URL (or None).
    Unlike deposit proofs, plan images are public — a plain public URL is
    used instead of a short-lived signed one."""
    if not image_filename:
        return None
    if image_filename.startswith('sb:'):
        path = image_filename[3:]
        try:
            res = get_sb().storage.from_(PLAN_IMAGES_BUCKET).get_public_url(path)
            return res if isinstance(res, str) else (res.get('publicUrl') or res.get('publicURL'))
        except Exception as e:
            print(f'resolve_plan_image_url error: {e}')
            return None
    return url_for('uploaded_file', filename=image_filename)

# ─────────────────────────────────────────────
# Database Initializer — runs SQL via Supabase RPC
# We create tables using Supabase Dashboard SQL editor instead.
# This just seeds the admin account if missing. Plans are NEVER
# auto-seeded — they exist only if an admin creates them via
# Admin → Add Plan, and stay deleted once an admin deletes them.
# ─────────────────────────────────────────────
def init_db():
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

        print("✓ Database ready")
    except Exception as e:
        print(f"⚠ DB init warning: {e}")

# ─────────────────────────────────────────────
# Plan Helper
# ─────────────────────────────────────────────
def _enrich_plan(p):
    """Add computed display fields to a raw plan row: a resolved image URL
    and the flat profit this plan pays out (total_return - price)."""
    p = dict(p)
    p['price'] = float(p.get('price') or 0)
    p['total_return'] = float(p.get('total_return') or 0)
    p['profit'] = r2(p['total_return'] - p['price'])
    p['image_url'] = resolve_plan_image_url(p.get('image_filename'))

    # Investor quota — only queried when a plan actually has one set, so
    # unlimited plans (max_investors is NULL, the default) cost no extra
    # query and behave exactly as before this field existed.
    p['max_investors'] = p.get('max_investors')
    if p['max_investors'] is not None:
        p['investor_count'] = sb_count('investments', [('plan_id', 'eq', p['id'])])
        if p['max_investors'] > 0:
            p['quota_percent'] = min(100, r2(p['investor_count'] / p['max_investors'] * 100))
        else:
            # A deliberate 0-slot quota — always full, avoid dividing by zero.
            p['quota_percent'] = 100.0
        p['sold_out'] = p['investor_count'] >= p['max_investors']
    else:
        p['investor_count'] = None
        p['quota_percent'] = None
        p['sold_out'] = False
    return p

def get_plans(active_only=True):
    filters = [('is_active', 'eq', True)] if active_only else []
    rows = sb_all('plans', filters=filters, order=[('sort_order', 'asc'), ('id', 'asc')])
    plans = []
    for r in rows:
        p = _enrich_plan(r)
        p['features'] = [f.strip() for f in (p.get('features') or '').split('|') if f.strip()]
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
    user, unread, plans, notice, site_settings = None, 0, [], None, SITE_SETTINGS_DEFAULTS
    unread_announcements = 0
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
        if user:
            unread_announcements = get_unread_announcement_count(user['id'])
    except Exception as e:
        print(f"inject_globals unread_announcements error: {e}")
    try:
        plans = get_plans()
    except Exception as e:
        print(f"inject_globals get_plans error: {e}")
    try:
        notice = get_active_notice()
    except Exception as e:
        print(f"inject_globals active_notice error: {e}")
    try:
        site_settings = get_site_settings()
    except Exception as e:
        print(f"inject_globals site_settings error: {e}")
    return dict(current_user=user, unread_count=unread, plans=plans,
               active_notice=notice, site_settings=site_settings,
               unread_announcement_count=unread_announcements)

# ─────────────────────────────────────────────
# Serve Uploads
# ─────────────────────────────────────────────
@app.route('/uploads/<path:filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)


# ─────────────────────────────────────────────
# Settings — tiny key/value store for platform
# toggles that need to apply instantly (no
# redeploy), unlike env vars. Requires a
# `settings` table: key text primary key,
# value text, updated_at timestamptz default now().
# ─────────────────────────────────────────────
def _settings_cache():
    """One bulk fetch of the whole `settings` table per request, cached on
    flask.g. Replaces what used to be a separate DB round trip for every
    single get_setting() call — maintenance mode, payment settings, the
    notice banner, and now the full site-settings page all read many keys
    per request, so this keeps that at 1 query instead of a dozen."""
    if not hasattr(g, '_settings_cache'):
        try:
            rows = sb_all('settings')
            g._settings_cache = {r['key']: r.get('value') for r in rows}
        except Exception as e:
            print(f"_settings_cache error: {e}")
            g._settings_cache = {}
    return g._settings_cache


def get_setting(key, default=None):
    """Fetch a single setting value by key. Returns `default` if unset.
    Signature/behavior unchanged from before — just backed by the
    request-scoped cache above instead of a fresh query every call."""
    value = _settings_cache().get(key)
    return value if value is not None else default

def set_setting(key, value):
    """Set a setting value (update-if-exists else insert — no upsert
    helper exists in this codebase, so this follows the same
    sb_update/sb_insert pattern used everywhere else)."""
    value = str(value)
    existing = sb_one('settings', [('key', 'eq', key)])
    if existing:
        ok = sb_update('settings', {'value': value}, [('key', 'eq', key)]) is not False
    else:
        ok = sb_insert('settings', {'key': key, 'value': value}) is not None
    if ok and hasattr(g, '_settings_cache'):
        g._settings_cache[key] = value  # keep same-request reads consistent
    return ok

def is_maintenance_mode():
    """DB flag (instant, admin-toggleable) OR the MAINTENANCE_MODE env var
    (kept as a redeploy-based fallback/emergency switch) — either can turn
    maintenance mode on."""
    if (get_setting('maintenance_mode', 'false') or 'false').strip().lower() == 'true':
        return True
    return os.environ.get('MAINTENANCE_MODE', '').strip().lower() == 'true'

def get_maintenance_message():
    return (get_setting('maintenance_message', '') or '').strip() \
        or os.environ.get('MAINTENANCE_MESSAGE', '').strip() or None


# ─────────────────────────────────────────────
# Payment Settings & Bank Accounts — admin-editable,
# no redeploy needed. Backs the /admin/payment-settings
# page (Phase 2/3). `bank_accounts` supports unlimited rows;
# only the one row with is_active=True is ever shown on
# payment.html. `settings` holds the global payment toggles
# (deposit_instructions, withdrawal_fee_percent, payment_status).
# ─────────────────────────────────────────────
def get_bank_accounts():
    """All bank accounts, admin-managed, most recently added first isn't
    useful here — sort_order then id keeps admin ordering stable."""
    return sb_all('bank_accounts', order=[('sort_order', 'asc'), ('id', 'asc')])


def get_active_bank_account():
    """The single account shown to users on payment.html, or None if the
    admin hasn't configured one yet."""
    return sb_one('bank_accounts', [('is_active', 'eq', True)])


def is_payments_enabled():
    """Global on/off switch for deposits & withdrawals — separate from full
    Maintenance Mode, so an admin can pause just the money-movement flows
    (e.g. while updating bank details) without taking the whole site down."""
    return (get_setting('payment_status', 'enabled') or 'enabled').strip().lower() != 'disabled'


def get_deposit_instructions():
    return (get_setting('deposit_instructions', '') or '').strip()


def get_withdrawal_fee_percent():
    try:
        return max(0.0, float(get_setting('withdrawal_fee_percent', '0') or 0))
    except (TypeError, ValueError):
        return 0.0


# ─────────────────────────────────────────────
# Sitewide Announcement / Notice Banner (Phase 5) — admin publishes one
# active notice at a time (Network Issue, Maintenance, Holiday, System
# Upgrade, Emergency, or a plain General Notice). Stored in `settings`,
# surfaced everywhere via inject_globals() so Dashboard, Payment Page,
# and Home Page all pick it up automatically with no per-route wiring.
# ─────────────────────────────────────────────
NOTICE_TYPES = {
    'info':        {'label': 'General Notice',   'icon': 'info',            'color': 'blue'},
    'network':     {'label': 'Network Issue',    'icon': 'wifi-off',        'color': 'amber'},
    'maintenance': {'label': 'Maintenance',       'icon': 'wrench',          'color': 'amber'},
    'holiday':     {'label': 'Holiday Notice',   'icon': 'calendar-heart',  'color': 'emerald'},
    'upgrade':     {'label': 'System Upgrade',   'icon': 'arrow-up-circle', 'color': 'blue'},
    'emergency':   {'label': 'Emergency Notice', 'icon': 'siren',           'color': 'red'},
}


def get_active_notice():
    """Returns the currently published notice dict, or None if the admin
    hasn't turned one on (or left the message blank)."""
    enabled = (get_setting('notice_enabled', 'false') or 'false').strip().lower() == 'true'
    if not enabled:
        return None
    message = (get_setting('notice_message', '') or '').strip()
    if not message:
        return None
    ntype = get_setting('notice_type', 'info') or 'info'
    meta = NOTICE_TYPES.get(ntype, NOTICE_TYPES['info'])
    return {'type': ntype, 'message': message, **meta}


# ─────────────────────────────────────────────
# Site Settings (Phase 11) — everything admin-editable from
# /admin/settings, backed by the `settings` table via get_setting()/
# set_setting() above. Every value here has a sane default matching
# what was previously hardcoded, so nothing changes until an admin
# actually edits something.
# ─────────────────────────────────────────────
SITE_SETTINGS_DEFAULTS = {
    'site_name':          'AgroVest Pro',
    'logo_url':           '/static/images/logo.png',
    'favicon_url':        '/static/images/favicon.ico',
    'support_email':      'support@agrovestpro.com',
    'whatsapp_link':      '',
    'telegram_link':      '',
    'facebook_link':      '',
    'instagram_link':     '',
    'twitter_link':       '',
    'office_address':     '',
    'currency_code':      'NGN',
    'currency_symbol':    '₦',
    'timezone':           'Africa/Lagos',
    'referral_percent':   '5',
    'min_withdrawal':     '2000',
    'max_withdrawal':     '1000000',
    'seo_title':          'AgroVest Pro — Agricultural Investment Platform',
    'seo_description':    'Invest in profitable Nigerian agricultural projects and earn guaranteed returns.',
    'og_image_url':       'https://agrovest-ydif.onrender.com/static/images/logo.png',
}


def get_site_settings():
    """All Phase-11 settings as one dict, each falling back to its default
    if the admin hasn't set it yet. One query total (via _settings_cache),
    not one per field."""
    return {k: (get_setting(k, v) or v) for k, v in SITE_SETTINGS_DEFAULTS.items()}


def get_referral_percent():
    """Replaces the old hardcoded REFERRAL_COMMISSION constant — same
    default value (5%), now admin-editable from /admin/settings."""
    try:
        return max(0.0, float(get_setting('referral_percent', REFERRAL_COMMISSION) or REFERRAL_COMMISSION))
    except (TypeError, ValueError):
        return float(REFERRAL_COMMISSION)


# ─────────────────────────────────────────────
# Announcements (Phase 10) — a full, manageable announcement system.
# Distinct from the single sitewide banner (get_active_notice() /
# `settings.notice_*`, admin "Site Banner" page) built earlier: each
# announcement here is its own persistent, editable record with optional
# scheduling, "selected users" targeting, and per-user read tracking.
# ─────────────────────────────────────────────
def get_visible_announcements(user_id=None):
    """Announcements currently visible to `user_id`: published (no
    schedule, or the scheduled time has already passed) and targeted at
    this user (either 'all', or 'selected' with this user's id included).
    The announcements table is small (bounded by how many an admin
    actually creates, not per-transaction like notifications), so
    fetching all of them and filtering in Python is intentional — same
    style already used elsewhere in this app (e.g. sb_sum())."""
    now = datetime.utcnow()
    rows = sb_all('announcements', order=('created_at', 'desc'))
    visible = []
    for a in rows:
        sched = a.get('scheduled_at')
        if sched:
            try:
                sched_dt = datetime.fromisoformat(str(sched).replace('Z', '+00:00')).replace(tzinfo=None)
                if sched_dt > now:
                    continue  # scheduled for the future — not published yet
            except Exception:
                pass
        if a.get('target_type') == 'selected':
            target_ids = a.get('target_user_ids') or []
            if user_id not in target_ids:
                continue
        visible.append(a)
    return visible


def get_announcement_read_ids(user_id):
    rows = sb_all('announcement_reads', filters=[('user_id', 'eq', user_id)])
    return {r['announcement_id'] for r in rows}


def get_unread_announcement_count(user_id):
    if not user_id:
        return 0
    visible = get_visible_announcements(user_id)
    read_ids = get_announcement_read_ids(user_id)
    return sum(1 for a in visible if a['id'] not in read_ids)


def get_latest_announcement_for_popup(user_id):
    """The single most recent announcement visible to this user, enriched
    with a resolved image URL — used for the dashboard popup. Returns None
    if there's nothing currently visible (no popup shown in that case).
    Whether it's actually *displayed* (vs already dismissed) is decided
    client-side via localStorage, keyed by this announcement's id — so a
    newly published announcement (different id) always reappears even if
    an older one was dismissed."""
    visible = get_visible_announcements(user_id)
    if not visible:
        return None
    latest = dict(visible[0])  # get_visible_announcements() already orders newest-first
    latest['image_url'] = resolve_plan_image_url(latest.get('image_filename'))
    return latest


def mark_announcement_read(announcement_id, user_id):
    if sb_one('announcement_reads', [('announcement_id', 'eq', announcement_id), ('user_id', 'eq', user_id)]):
        return True
    return sb_insert('announcement_reads', {'announcement_id': announcement_id, 'user_id': user_id}) is not None


def get_active_banners():
    """Active banners for the dashboard slider (Phase 4's frontend, Phase 11's
    admin backend), ordered for display. Reuses the same public image-upload
    path as plan images — resolve_plan_image_url() works on any filename
    saved via upload_plan_image() regardless of which table stored it."""
    rows = sb_all('banners', filters=[('is_active', 'eq', True)],
                  order=[('sort_order', 'asc'), ('id', 'asc')])
    for b in rows:
        b['image_url'] = resolve_plan_image_url(b.get('image_filename'))
    return rows


def get_daily_trend(table, days=7, status_filter=None):
    """Sum of `amount` per calendar day (admin timezone) for the last
    `days` days, oldest first — powers the admin dashboard trend chart.
    One fetch, bucketed in Python, same style as sb_sum()."""
    tz = get_display_timezone()
    today_local = datetime.now(tz).date()
    start_local = today_local - timedelta(days=days - 1)
    start_utc = datetime.combine(start_local, datetime.min.time(), tzinfo=tz).astimezone(timezone.utc)

    filters = [('created_at', 'gte', start_utc.isoformat())]
    if status_filter:
        filters.append(('status', 'eq', status_filter))
    try:
        rows = sb_all(table, filters=filters)
    except Exception as e:
        print(f'get_daily_trend({table}) error: {e}')
        rows = []

    buckets = {start_local + timedelta(days=i): 0.0 for i in range(days)}
    for r in rows:
        try:
            dt = datetime.fromisoformat(str(r['created_at']).replace('Z', '+00:00')).astimezone(tz).date()
            if dt in buckets:
                buckets[dt] += float(r.get('amount') or 0)
        except Exception:
            continue
    return [{'label': d.strftime('%b %d'), 'amount': round(v, 2)} for d, v in sorted(buckets.items())]


def get_withdrawal_limits():
    """(min, max) withdrawal amounts — same defaults as the old hardcoded
    ₦2,000 minimum (no cap existed before, so the default max is generous)."""
    try:
        min_wd = float(get_setting('min_withdrawal', 2000) or 2000)
    except (TypeError, ValueError):
        min_wd = 2000.0
    try:
        max_wd = float(get_setting('max_withdrawal', 1000000) or 1000000)
    except (TypeError, ValueError):
        max_wd = 1000000.0
    return min_wd, max_wd


def get_display_timezone():
    """zoneinfo object for the admin-configured timezone, falling back to
    Africa/Lagos (Nigeria) — the timezone AgroVest Pro was already
    implicitly using. Falls back to UTC if the configured name is invalid."""
    from zoneinfo import ZoneInfo
    tz_name = get_setting('timezone', 'Africa/Lagos') or 'Africa/Lagos'
    try:
        return ZoneInfo(tz_name)
    except Exception:
        try:
            return ZoneInfo('Africa/Lagos')
        except Exception:
            from datetime import timezone as _tz
            return _tz.utc


def get_today_profit_total(user_id):
    """Sum of daily-profit (ROI) transactions actually credited *today*, in
    the admin-configured display timezone.

    This replaces the old approach of summing each active investment's
    `daily_profit` RATE, which showed the day's expected profit before it
    had actually been credited (e.g. at 7:30 AM, before the ~1 AM UTC cron
    / next dashboard visit has run). Sourcing this from real `transactions`
    rows means:
      - Before today's credit runs: 0 rows found today → ₦0.00, correctly.
      - Right after the credit runs: that ROI row is included immediately.
      - Multiple active investments: each credits its own ROI row, so the
        sum naturally includes all of them.
      - Midnight rollover: the query window is always "today, local time"
        so it resets on its own with no extra job needed — nothing is
        deleted, the transaction rows and notifications are untouched,
        only what counts as "today" shifts forward.
    """
    tz = get_display_timezone()
    start_of_day_local = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    start_of_day_utc = start_of_day_local.astimezone(timezone.utc).isoformat()

    try:
        rows = sb_all('transactions', filters=[
            ('user_id', 'eq', user_id),
            ('transaction_type', 'eq', 'ROI'),
            ('created_at', 'gte', start_of_day_utc),
        ])
    except Exception as e:
        print(f'get_today_profit_total error for user {user_id}: {e}')
        return 0.0

    return r2(sum(float(t.get('amount') or 0) for t in rows))


# ─────────────────────────────────────────────
# Daily Check-in — one row per check-in in `checkins`; streak and
# "already checked in today" are both derived from the most recent row
# rather than stored separately, so there's nothing to keep in sync.
# ─────────────────────────────────────────────
def get_checkin_reward_amount():
    try:
        return max(0.0, float(get_setting('checkin_reward_amount', 50) or 50))
    except (TypeError, ValueError):
        return 50.0


def get_checkin_status(user_id):
    """Returns {checked_in_today, current_streak, reward_amount} for display
    — does not perform a check-in. `current_streak` is the streak the user
    is currently sitting on: if they checked in today it's that day's streak
    number; if they checked in yesterday it's still shown (they can extend
    it today); if it's older than yesterday the streak has lapsed (shown as
    0 — the next check-in restarts at day 1)."""
    tz = get_display_timezone()
    today_local = datetime.now(tz).date()
    reward = get_checkin_reward_amount()
    rows = sb_all('checkins', filters=[('user_id', 'eq', user_id)],
                  order=('created_at', 'desc'), limit=1)
    if not rows:
        return {'checked_in_today': False, 'current_streak': 0, 'reward_amount': reward}

    row = rows[0]
    last_local = datetime.fromisoformat(str(row['created_at']).replace('Z', '+00:00')).astimezone(tz).date()
    checked_in_today = (last_local == today_local)
    streak = int(row.get('streak_day') or 0)
    if not checked_in_today and (today_local - last_local).days > 1:
        streak = 0  # lapsed — next check-in restarts at day 1
    return {'checked_in_today': checked_in_today, 'current_streak': streak, 'reward_amount': reward}


# ─────────────────────────────────────────────
# Gift Codes
# ─────────────────────────────────────────────
def generate_gift_code():
    """AGRO-XXXX-XXXX style code, regenerated if it happens to collide."""
    for _ in range(10):
        code = 'AGRO-' + secrets.token_hex(2).upper() + '-' + secrets.token_hex(2).upper()
        if not sb_one('gift_codes', [('code', 'eq', code)]):
            return code
    return 'AGRO-' + secrets.token_hex(6).upper()


# ─────────────────────────────────────────────
# Env-var health check — shown instead of 500
# when Supabase credentials are missing/wrong
# ─────────────────────────────────────────────
def check_env():
    """Return (ok, missing_list, errors_list)."""
    url = os.environ.get('SUPABASE_URL', '').strip()
    key = os.environ.get('SUPABASE_SECRET_KEY', '').strip()
    missing, errors = [], []
    if not url:
        missing.append('SUPABASE_URL')
    elif not url.startswith('https://'):
        errors.append(f'SUPABASE_URL must start with https:// — got: {url[:60]}')
    if not key:
        missing.append('SUPABASE_SECRET_KEY')
    return (len(missing) == 0 and len(errors) == 0), missing, errors

@app.before_request
def check_maintenance():
    """Show a maintenance page to everyone except logged-in admins.

    Instant toggle: admins flip this from the admin dashboard (persisted in
    the `settings` table via is_maintenance_mode()/set_setting()) — no
    redeploy needed. The MAINTENANCE_MODE env var still works too, as a
    redeploy-based fallback for when the dashboard/DB itself is unreachable.
    """
    if not is_maintenance_mode():
        return
    if request.endpoint in ('static', 'uploaded_file', 'login', 'logout', 'cron_daily_profits'):
        return
    if session.get('is_admin'):
        return  # admins can still use the site to fix things / lift maintenance
    return render_template('maintenance.html',
                           maintenance_message=get_maintenance_message()), 503

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
    try:
        plans = get_plans(active_only=True)
    except Exception as e:
        print(f'index() plans error: {e}')
        plans = []
    return render_template('index.html', stats=stats, plans=plans, referral_percent=get_referral_percent())

@app.route('/about')
def about():
    return render_template('about.html')

@app.route('/terms')
def terms():
    return render_template('terms.html')

@app.route('/privacy')
def privacy():
    return render_template('privacy.html')

@app.route('/risk-disclosure')
def risk_disclosure():
    return render_template('risk-disclosure.html')

@app.route('/plans')
def plans():
    return render_template('plans.html')

@app.route('/contact', methods=['GET', 'POST'])
def contact():
    if request.method == 'POST':
        full_name = request.form.get('full_name', '').strip()
        email     = request.form.get('email', '').strip().lower()
        phone     = request.form.get('phone', '').strip()
        subject   = request.form.get('subject', '').strip() or 'General Enquiry'
        message   = request.form.get('message', '').strip()
        attachment = request.files.get('attachment')
        has_attachment = bool(attachment and attachment.filename)

        wants_json = (request.headers.get('X-Requested-With') == 'XMLHttpRequest' or
                      'application/json' in (request.headers.get('Accept') or ''))

        errors = []
        if not full_name or len(full_name) < 3:
            errors.append('Please enter your full name (at least 3 characters).')
        if not email or not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email):
            errors.append('Enter a valid email address.')
        if not phone or not re.match(r'^[0-9+\-\s]{7,20}$', phone):
            errors.append('Enter a valid phone number.')
        if not subject:
            errors.append('Please select a subject.')
        if not message or len(message) < 10:
            errors.append('Please describe your issue (at least 10 characters).')

        if has_attachment:
            if not is_allowed_file(attachment.filename):
                errors.append('Only PNG, JPG, JPEG or PDF files are accepted for the attachment.')
            else:
                attachment.stream.seek(0, os.SEEK_END)
                att_size = attachment.stream.tell()
                attachment.stream.seek(0)
                if att_size <= 0:
                    errors.append('That attachment appears to be empty. Please choose a valid file.')
                elif att_size > 5 * 1024 * 1024:
                    errors.append('Attachment is too large. Maximum size is 5MB.')

        if errors:
            if wants_json:
                return jsonify({'status': 'validation_error', 'errors': errors}), 400
            for e in errors:
                flash(e, 'error')
            return redirect(url_for('contact'))

        attachment_filename = None
        if has_attachment:
            attachment_filename = upload_contact_attachment(attachment)
            if not attachment_filename:
                msg = 'We could not process your attachment. Please try again.'
                if wants_json:
                    return jsonify({'status': 'error', 'message': msg}), 500
                flash(msg, 'error')
                return redirect(url_for('contact'))

        try:
            current = get_current_user()
        except Exception:
            current = None

        created = sb_insert('contact_messages', {
            'user_id':    current['id'] if current else None,
            'full_name':  full_name,
            'email':      email,
            'phone':      phone,
            'subject':    subject,
            'message':    message,
            'attachment': attachment_filename,
            'status':     'Unread',
        })

        if not created:
            msg = 'We could not send your message right now. Please try again shortly.'
            if wants_json:
                return jsonify({'status': 'error', 'message': msg}), 500
            flash(msg, 'error')
            return redirect(url_for('contact'))

        notify_admins(f'New contact message from {full_name}: {subject}', 'info')
        send_contact_confirmation_email(email, full_name)

        if wants_json:
            return jsonify({'status': 'success',
                             'message': "We've received your message and will reply within 1 hour."})
        flash("Message sent! We'll reply to your email within 1 hour.", 'success')
        return redirect(url_for('contact'))

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
    uid = session['user_id']

    # Lazily catch up any daily profit owed on this user's active investments.
    # This is what makes the profit engine self-healing even if the external
    # /cron/daily-profits job hasn't fired recently — every dashboard visit
    # re-checks and credits anything owed. Safe to call as often as needed:
    # process_investment_daily_profit() is idempotent per elapsed day and the
    # underlying agrovest_credit_daily_profit RPC takes a row lock, so this
    # can never double-credit even under concurrent requests.
    run_daily_profit_distribution(user_id=uid)

    # Re-fetch the user fresh (bypassing the per-request g cache set by
    # get_current_user() earlier in login_required) so balance/total_earnings
    # reflect any profit that was just credited above.
    user = sb_one('users', [('id', 'eq', uid)])
    if not user:
        session.clear()
        flash('Your account could not be found. Please log in again.', 'error')
        return redirect(url_for('login'))
    g._current_user_cache = user

    active_investments = [
        _enrich_investment_progress(inv) for inv in sb_all('investments',
            filters=[('user_id','eq',uid),('status','eq','active')],
            order=('created_at','desc'))
    ]
    recent_deposits = sb_all('deposits',
        filters=[('user_id','eq',uid)], order=('created_at','desc'), limit=5)
    recent_withdrawals = sb_all('withdrawals',
        filters=[('user_id','eq',uid)], order=('created_at','desc'), limit=5)
    # Lifetime approved withdrawals total, for the dashboard's "Total
    # Withdrawals" stat card — a separate aggregate from recent_withdrawals
    # above (which is capped at 5 rows for the activity list).
    total_withdrawn = sb_sum('withdrawals', 'amount',
        [('user_id','eq',uid), ('status','eq','approved')])
    notifications = sb_all('notifications',
        filters=[('user_id','eq',uid)], order=('created_at','desc'), limit=10)

    # Referrals with referred user names
    raw_refs = sb_all('referrals', filters=[('referrer_id','eq',uid)],
                      order=('created_at','desc'))
    referrals = []
    for r in raw_refs:
        referred_user = sb_one('users', [('id','eq',r['referred_id'])])
        referrals.append({**r, 'full_name': referred_user['full_name'] if referred_user else 'Unknown'})

    # Real "Today's Profit": the sum of ROI transactions actually credited
    # TODAY (in the admin-configured timezone) — not each investment's daily
    # RATE, which would show the day's expected profit before the daily
    # profit engine has actually run and credited it. Before that credit
    # happens today, this correctly shows ₦0.00; after it runs, it reflects
    # the real amount(s) credited, summed across every investment; it stays
    # that way until local midnight, when "today" naturally rolls forward
    # and it resets on its own — no data is deleted.
    today_profit = get_today_profit_total(user['id'])
    total_invested = float(user.get('total_invested') or 0)
    profit_change_pct = r2((today_profit / total_invested * 100) if total_invested > 0 else 0)

    # Recent transactions (ROI credits + any other types) — fetched after
    # run_daily_profit_distribution() above so today's credit is already
    # in the table when the template renders.
    try:
        recent_transactions = sb_all('transactions',
            filters=[('user_id', 'eq', uid)],
            order=('created_at', 'desc'), limit=10)
    except Exception as e:
        print(f'dashboard: transactions fetch error: {e}')
        recent_transactions = []

    return render_template('dashboard.html',
        user=user, active_investments=active_investments,
        recent_deposits=recent_deposits, recent_withdrawals=recent_withdrawals,
        total_withdrawn=total_withdrawn,
        referrals=referrals, notifications=notifications,
        today_profit=today_profit, profit_change_pct=profit_change_pct,
        recent_transactions=recent_transactions,
        checkin_status=get_checkin_status(uid),
        popup_announcement=get_latest_announcement_for_popup(uid),
        now_utc=datetime.utcnow())


@app.route('/transactions')
@login_required
def all_transactions_page():
    """Full transaction history (Phase 7) — unifies deposits, withdrawals,
    and daily-profit (ROI) credits into one normalized list for a proper
    searchable/filterable/paginated table. Referral commissions have their
    own dedicated history on the Referral page (Phase 8) rather than being
    synthesized into this list, since they aren't recorded as rows in any
    of these three tables today."""
    user = get_current_user()
    uid = user['id']

    deposits = sb_all('deposits', filters=[('user_id', 'eq', uid)], order=('created_at', 'desc'))
    withdrawals = sb_all('withdrawals', filters=[('user_id', 'eq', uid)], order=('created_at', 'desc'))
    roi_rows = sb_all('transactions', filters=[('user_id', 'eq', uid)], order=('created_at', 'desc'))

    unified = []
    for d in deposits:
        method = (d.get('payment_method') or '').replace('_', ' ').title()
        unified.append({
            'type': 'Deposit', 'category': 'deposit', 'amount': float(d.get('amount') or 0), 'sign': '+',
            'status': d.get('status', 'pending'),
            'description': f'Deposit via {method}' if method else 'Deposit',
            'reference': d.get('reference') or '—', 'created_at': d.get('created_at'),
        })
    for w in withdrawals:
        unified.append({
            'type': 'Withdrawal', 'category': 'withdrawal', 'amount': float(w.get('amount') or 0), 'sign': '-',
            'status': w.get('status', 'pending'),
            'description': f"Withdrawal to {w.get('bank_name', '')}".strip(),
            'reference': w.get('account_number') or '—', 'created_at': w.get('created_at'),
        })
    for t in roi_rows:
        unified.append({
            'type': t.get('transaction_type', 'ROI'), 'category': 'roi', 'amount': float(t.get('amount') or 0), 'sign': '+',
            'status': t.get('status', 'Completed'),
            'description': t.get('description') or f"Daily profit from {t.get('plan_name', 'your investment')}",
            'reference': '—', 'created_at': t.get('created_at'),
        })

    unified.sort(key=lambda x: x['created_at'] or '', reverse=True)
    return render_template('transactions.html', transactions=unified)


@app.route('/referral')
@login_required
def referral_page():
    """Dedicated Referral page (Phase 8) — link, copy button, stats,
    earnings, and full history. Same referrals data shape already used by
    the dashboard leaderboard widget, just with its own full page/table."""
    user = get_current_user()
    uid = user['id']

    raw_refs = sb_all('referrals', filters=[('referrer_id', 'eq', uid)],
                      order=('created_at', 'desc'))
    referrals = []
    for r in raw_refs:
        referred_user = sb_one('users', [('id', 'eq', r['referred_id'])])
        referrals.append({
            **r,
            'full_name': referred_user['full_name'] if referred_user else 'Unknown',
            'email': referred_user['email'] if referred_user else '',
        })

    total_referred = len(referrals)
    active_referrals = sum(1 for r in referrals if r.get('status') == 'active')
    pending_referrals = total_referred - active_referrals

    return render_template('referral.html', user=user, referrals=referrals,
                           total_referred=total_referred,
                           active_referrals=active_referrals,
                           pending_referrals=pending_referrals,
                           referral_percent=get_referral_percent())


@app.route('/profile')
@login_required
def profile():
    """Modern Profile page (Phase 9) — edit profile, change password, saved
    bank details, referral code, and an account-info summary. No KYC."""
    user = get_current_user()
    return render_template('profile.html', user=user)


@app.route('/profile/update', methods=['POST'])
@login_required
def profile_update():
    user = get_current_user()
    full_name = request.form.get('full_name', '').strip()
    phone     = request.form.get('phone', '').strip()

    if not full_name or len(full_name) < 3:
        flash('Full name must be at least 3 characters.', 'error')
        return redirect(url_for('profile'))
    if phone and not re.match(r'^[0-9+\-\s]{7,20}$', phone):
        flash('Enter a valid phone number.', 'error')
        return redirect(url_for('profile'))

    if sb_update('users', {'full_name': full_name, 'phone': phone}, [('id', 'eq', user['id'])]):
        flash('Profile updated successfully.', 'success')
    else:
        flash('Could not update your profile — please try again.', 'error')
    return redirect(url_for('profile'))


@app.route('/profile/password', methods=['POST'])
@login_required
def profile_change_password():
    user = get_current_user()
    current_password = request.form.get('current_password', '')
    new_password      = request.form.get('new_password', '')
    confirm_password  = request.form.get('confirm_new_password', '')

    if not check_password_hash(user['password_hash'], current_password):
        flash('Current password is incorrect.', 'error')
        return redirect(url_for('profile'))
    if len(new_password) < 8:
        flash('New password must be at least 8 characters.', 'error')
        return redirect(url_for('profile'))
    if new_password != confirm_password:
        flash('New passwords do not match.', 'error')
        return redirect(url_for('profile'))

    if sb_update('users', {'password_hash': generate_password_hash(new_password)},
                [('id', 'eq', user['id'])]):
        notify(user['id'], 'Your password was changed successfully.', 'security')
        flash('Password changed successfully.', 'success')
    else:
        flash('Could not change your password — please try again.', 'error')
    return redirect(url_for('profile'))


@app.route('/profile/bank-details', methods=['POST'])
@login_required
def profile_bank_details():
    """Saves the user's preferred payout bank details, purely for
    convenience — the withdraw form pre-fills from these if present, but
    users can still edit/override them at withdrawal time exactly as
    before this existed. Nothing about the withdraw flow itself changed."""
    user = get_current_user()
    bank_name      = request.form.get('bank_name', '').strip()
    account_number = request.form.get('account_number', '').strip()
    account_name   = request.form.get('account_name', '').strip()

    if not (bank_name and account_number and account_name):
        flash('Please fill in all bank detail fields.', 'error')
        return redirect(url_for('profile'))
    if not re.match(r'^\d{10}$', account_number):
        flash('Account number must be exactly 10 digits.', 'error')
        return redirect(url_for('profile'))

    if sb_update('users', {
        'bank_name': bank_name, 'bank_account_number': account_number,
        'bank_account_name': account_name,
    }, [('id', 'eq', user['id'])]):
        flash('Bank details saved.', 'success')
    else:
        flash('Could not save your bank details — please try again.', 'error')
    return redirect(url_for('profile'))


@app.route('/announcements')
@login_required
def announcement_list():
    """Full Announcements list (Phase 10) — every announcement currently
    visible to this user, newest first, with read/unread state."""
    user = get_current_user()
    visible = get_visible_announcements(user['id'])
    read_ids = get_announcement_read_ids(user['id'])
    for a in visible:
        a['is_read'] = a['id'] in read_ids
    return render_template('announcements.html', announcements=visible)


@app.route('/announcements/<int:announcement_id>')
@login_required
def announcement_detail(announcement_id):
    """Opening an announcement marks it read — same pattern as email."""
    user = get_current_user()
    announcement = sb_one('announcements', [('id', 'eq', announcement_id)])
    if not announcement:
        flash('Announcement not found.', 'error')
        return redirect(url_for('announcement_list'))

    # Make sure this announcement is actually visible to this user before
    # showing it (respects 'selected users' targeting).
    if announcement.get('target_type') == 'selected':
        target_ids = announcement.get('target_user_ids') or []
        if user['id'] not in target_ids:
            flash('Announcement not found.', 'error')
            return redirect(url_for('announcement_list'))

    mark_announcement_read(announcement_id, user['id'])
    announcement['image_url'] = resolve_plan_image_url(announcement.get('image_filename'))
    return render_template('announcement_detail.html', announcement=announcement)


@app.route('/checkin', methods=['POST'])
@login_required
def daily_checkin():
    """Credits today's check-in reward once per 24h (calendar day, in the
    admin-configured timezone) and extends the streak — or restarts it at 1
    if a day was missed. Everything needed to decide that is derived from
    the most recent row in `checkins`; nothing is stored on `users`."""
    user = get_current_user()
    tz = get_display_timezone()
    today_local = datetime.now(tz).date()

    last_rows = sb_all('checkins', filters=[('user_id', 'eq', user['id'])],
                       order=('created_at', 'desc'), limit=1)
    last_row = last_rows[0] if last_rows else None

    if last_row:
        last_local = datetime.fromisoformat(str(last_row['created_at']).replace('Z', '+00:00')).astimezone(tz).date()
        if last_local == today_local:
            flash("You've already checked in today — come back tomorrow!", 'error')
            return redirect(url_for('dashboard'))
        streak = int(last_row.get('streak_day') or 0) + 1 if (today_local - last_local).days == 1 else 1
    else:
        streak = 1

    reward = get_checkin_reward_amount()

    credited = sb_adjust_balance(user['id'], balance_delta=reward)
    if not credited:
        flash('Check-in failed — please try again.', 'error')
        return redirect(url_for('dashboard'))

    recorded = sb_insert('checkins', {'user_id': user['id'], 'reward_amount': reward, 'streak_day': streak})
    if not recorded:
        rolled_back = sb_adjust_balance(user['id'], balance_delta=-reward)
        if not rolled_back:
            print(f'⚠ CRITICAL: checkin rollback failed for user {user["id"]}, '
                  f'amount {reward} — balance may be incorrect, investigate manually.')
        flash('Check-in failed — please try again.', 'error')
        return redirect(url_for('dashboard'))

    notify(user['id'], f'Day {streak} check-in reward: ₦{reward:,.2f} credited to your wallet!', 'success')
    flash(f'Checked in! Day {streak} streak — ₦{reward:,.2f} credited.', 'success')
    return redirect(url_for('dashboard'))


@app.route('/gift-code/redeem', methods=['POST'])
@login_required
def redeem_gift_code():
    """Validates and redeems a gift code, crediting the reward to the
    user's wallet. Double-redemption is prevented two ways: an app-level
    lookup for a friendly error message, and — the real guarantee under a
    race — the UNIQUE(gift_code_id, user_id) constraint on
    gift_code_redemptions, which makes a second simultaneous insert fail
    and get rolled back below."""
    user = get_current_user()
    code_input = (request.form.get('code') or '').strip().upper()
    if not code_input:
        flash('Please enter a gift code.', 'error')
        return redirect(url_for('dashboard'))

    gift = sb_one('gift_codes', [('code', 'eq', code_input)])
    if not gift:
        flash('Invalid gift code.', 'error')
        return redirect(url_for('dashboard'))
    if not gift.get('is_active'):
        flash('This gift code is no longer active.', 'error')
        return redirect(url_for('dashboard'))

    expires_at = gift.get('expires_at')
    if expires_at:
        try:
            exp_dt = datetime.fromisoformat(str(expires_at).replace('Z', '+00:00'))
            if datetime.now(timezone.utc) > exp_dt:
                flash('This gift code has expired.', 'error')
                return redirect(url_for('dashboard'))
        except Exception:
            pass

    times_used  = int(gift.get('times_used') or 0)
    usage_limit = int(gift.get('usage_limit') or 0)
    if times_used >= usage_limit:
        flash('This gift code has reached its usage limit.', 'error')
        return redirect(url_for('dashboard'))

    if sb_one('gift_code_redemptions', [('gift_code_id', 'eq', gift['id']), ('user_id', 'eq', user['id'])]):
        flash("You've already redeemed this gift code.", 'error')
        return redirect(url_for('dashboard'))

    reward = r2(gift['reward_amount'])

    # Compare-and-swap on times_used — only succeeds if nobody else has
    # bumped it since we read it, narrowing (not fully eliminating, without
    # a DB-side lock) the race window on the usage_limit cap.
    bumped = sb_update('gift_codes', {'times_used': times_used + 1},
                       [('id', 'eq', gift['id']), ('times_used', 'eq', times_used)])
    if not bumped:
        flash('This code was just redeemed by someone else and hit its limit. Please try again.', 'error')
        return redirect(url_for('dashboard'))

    credited = sb_adjust_balance(user['id'], balance_delta=reward)
    if not credited:
        sb_update('gift_codes', {'times_used': times_used}, [('id', 'eq', gift['id'])])
        flash('Redemption failed — please try again.', 'error')
        return redirect(url_for('dashboard'))

    recorded = sb_insert('gift_code_redemptions', {
        'gift_code_id': gift['id'], 'user_id': user['id'], 'amount': reward,
    })
    if not recorded:
        rolled_back = sb_adjust_balance(user['id'], balance_delta=-reward)
        sb_update('gift_codes', {'times_used': times_used}, [('id', 'eq', gift['id'])])
        if not rolled_back:
            print(f'⚠ CRITICAL: gift code rollback failed for user {user["id"]}, '
                  f'code {code_input}, amount {reward} — balance may be incorrect, investigate manually.')
        flash('You may have already redeemed this code just now. Please contact support if your balance looks off.', 'error')
        return redirect(url_for('dashboard'))

    notify(user['id'], f'₦{reward:,.2f} gift code reward credited to your wallet!', 'success')
    flash(f'Success! ₦{reward:,.2f} has been credited to your wallet.', 'success')
    return redirect(url_for('dashboard'))


@app.route('/dashboard/invest', methods=['GET', 'POST'])
@login_required
def invest():
    user      = get_current_user()
    all_plans = get_plans()

    if request.method == 'POST':
        plan_id = request.form.get('plan_id', 0, type=int)

        plan = sb_one('plans', [('id','eq',plan_id),('is_active','eq',True)])
        if not plan:
            flash('Invalid plan selected.', 'error')
            return redirect(url_for('invest'))

        # Investor quota (optional, admin-set). Best-effort check — not a
        # hard atomic guarantee like the balance debit below, since this is
        # a marketing cap rather than a scarce financial resource; a rare
        # simultaneous last-slot race could let it go one over, which is an
        # acceptable trade-off for not adding transaction-level locking here.
        if plan.get('max_investors') is not None:
            current_count = sb_count('investments', [('plan_id', 'eq', plan_id)])
            if current_count >= plan['max_investors']:
                flash('This plan has reached its investor quota and is no longer accepting new investments.', 'error')
                return redirect(url_for('invest'))

        # The investment amount is always the plan's fixed price — users
        # never enter or choose a custom amount.
        amount          = r2(float(plan['price']))
        total_return    = r2(float(plan['total_return']))
        duration_days   = int(plan['duration_days'])
        profit          = r2(total_return - amount)
        expected_return = total_return

        # Atomic debit — fails cleanly if balance is insufficient, even under
        # concurrent requests (e.g. a double-clicked submit button).
        debited = sb_adjust_balance(user['id'], balance_delta=-amount,
                                     total_invested_delta=amount,
                                     require_sufficient_balance=True)
        if not debited:
            flash('Insufficient balance. Please deposit funds first.', 'error')
            return redirect(url_for('deposit'))

        start_dt = datetime.utcnow()
        end_date = (start_dt + timedelta(days=duration_days)).isoformat()
        # Fixed daily payout for this investment, locked in at creation time
        # so it never has to be recomputed/guessed later — this is the exact
        # amount the automatic daily-profit engine will credit every 24h.
        daily_profit = r2(profit / duration_days)

        new_investment = sb_insert('investments', {
            'user_id': user['id'], 'plan_id': plan_id,
            'plan_name': plan['name'], 'amount': amount,
            'expected_return': expected_return, 'profit': profit,
            'duration_days': duration_days,
            'start_date': start_dt.isoformat(),
            'end_date': end_date, 'status': 'active',
            'daily_profit': daily_profit,
            'last_profit_date': start_dt.isoformat(),
            'total_profit': 0,
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
            commission = r2(amount * get_referral_percent() / 100)
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
        if not is_payments_enabled():
            flash('Deposits and withdrawals are temporarily unavailable due to maintenance.', 'error')
            return redirect(url_for('deposit'))

        amount         = r2(request.form.get('amount', 0, type=float))
        payment_method = request.form.get('payment_method', '').strip()
        proof          = request.files.get('proof')

        if amount < 1000:
            flash('Minimum deposit is ₦1,000.', 'error')
            return redirect(url_for('deposit'))
        if not payment_method:
            flash('Please select a payment method.', 'error')
            return redirect(url_for('deposit'))

        # Payment receipt is mandatory. Every check below is enforced here
        # server-side regardless of what the frontend already validated,
        # since a client-side check can always be bypassed.
        if 'proof' not in request.files or not proof or not proof.filename:
            flash('A payment receipt is required. Please upload your proof of payment.', 'error')
            return redirect(url_for('deposit'))

        if not is_allowed_file(proof.filename):
            flash('Only PNG, JPG, JPEG or PDF files are accepted for the receipt.', 'error')
            return redirect(url_for('deposit'))

        proof.stream.seek(0, os.SEEK_END)
        proof_size = proof.stream.tell()
        proof.stream.seek(0)
        if proof_size <= 0:
            flash('That receipt file appears to be empty. Please upload a valid file.', 'error')
            return redirect(url_for('deposit'))
        if proof_size > 5 * 1024 * 1024:
            flash('Receipt file is too large. Maximum size is 5MB.', 'error')
            return redirect(url_for('deposit'))

        proof_filename = upload_proof(proof)
        if not proof_filename:
            flash('We could not process your receipt upload. Please try again.', 'error')
            return redirect(url_for('deposit'))

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
    return render_template('deposit.html', user=user, deposits=deposits,
                           payments_enabled=is_payments_enabled())


@app.route('/dashboard/payment')
@login_required
def payment():
    """Step 2 of the deposit flow — shows bank accounts to pay into.
    Amount/method are passed via query string from deposit.html and are
    purely for display; the actual deposit record is still created by the
    existing /dashboard/deposit POST handler above, untouched."""
    user = get_current_user()
    active_account = get_active_bank_account()
    return render_template('payment.html', user=user,
                           active_account=active_account,
                           deposit_instructions=get_deposit_instructions(),
                           payments_enabled=is_payments_enabled())


@app.route('/dashboard/withdraw', methods=['GET', 'POST'])
@login_required
def withdraw():
    user = get_current_user()

    if request.method == 'POST':
        if not is_payments_enabled():
            flash('Deposits and withdrawals are temporarily unavailable due to maintenance.', 'error')
            return redirect(url_for('withdraw'))

        amount         = r2(request.form.get('amount', 0, type=float))
        bank_name      = request.form.get('bank_name', '').strip()
        account_number = request.form.get('account_number', '').strip()
        account_name   = request.form.get('account_name', '').strip()
        min_wd, max_wd = get_withdrawal_limits()

        if amount < min_wd:
            flash(f'Minimum withdrawal is {naira_filter(min_wd)}.', 'error')
            return redirect(url_for('withdraw'))
        if amount > max_wd:
            flash(f'Maximum withdrawal is {naira_filter(max_wd)}.', 'error')
            return redirect(url_for('withdraw'))
        if not (bank_name and account_number and account_name):
            flash('Please fill in all bank details.', 'error')
            return redirect(url_for('withdraw'))
        if not re.match(r'^\d{10}$', account_number):
            flash('Account number must be exactly 10 digits.', 'error')
            return redirect(url_for('withdraw'))

        # Withdrawal fee (admin-configured %, applied to the payout — the
        # user's balance is still debited the full requested `amount`,
        # unchanged from before; the fee is only deducted from what admin
        # actually transfers out, recorded here for their reference).
        fee_percent = get_withdrawal_fee_percent()
        fee_amount  = r2(amount * fee_percent / 100)
        net_amount  = r2(amount - fee_amount)

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
            'fee_amount': fee_amount, 'net_amount': net_amount,
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

        if fee_amount > 0:
            notify(user['id'], f'Withdrawal of ₦{amount:,.2f} submitted (₦{fee_amount:,.2f} fee '
                                f'applies — you will receive ₦{net_amount:,.2f}).', 'info')
        else:
            notify(user['id'], f'Withdrawal of ₦{amount:,.2f} submitted.', 'info')
        flash('Withdrawal submitted! Processing within 24 hours.', 'success')
        return redirect(url_for('dashboard'))

    withdrawals = sb_all('withdrawals', filters=[('user_id','eq',user['id'])],
                         order=('created_at','desc'))
    min_wd, max_wd = get_withdrawal_limits()
    return render_template('withdraw.html', user=user, withdrawals=withdrawals,
                           payments_enabled=is_payments_enabled(),
                           withdrawal_fee_percent=get_withdrawal_fee_percent(),
                           min_withdrawal=min_wd, max_withdrawal=max_wd)


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


@app.route('/notifications')
@login_required
def all_notifications():
    """Dedicated Notifications page (Phase 8) — the bell dropdown only ever
    shows the latest 10; this shows everything with mark-read/delete controls."""
    user = get_current_user()
    rows = sb_all('notifications', filters=[('user_id', 'eq', user['id'])],
                  order=('created_at', 'desc'), limit=200)
    return render_template('notifications.html', user=user, notifications=rows)


@app.route('/notifications/<int:notification_id>/read', methods=['POST'])
@login_required
def mark_one_notification_read(notification_id):
    user = get_current_user()
    sb_update('notifications', {'is_read': True},
             [('id', 'eq', notification_id), ('user_id', 'eq', user['id'])])
    return redirect(url_for('all_notifications'))


@app.route('/notifications/<int:notification_id>/delete', methods=['POST'])
@login_required
def delete_one_notification(notification_id):
    user = get_current_user()
    sb_delete('notifications', [('id', 'eq', notification_id), ('user_id', 'eq', user['id'])])
    return redirect(url_for('all_notifications'))


@app.route('/notifications/delete-all', methods=['POST'])
@login_required
def delete_all_notifications():
    user = get_current_user()
    sb_delete('notifications', [('user_id', 'eq', user['id'])])
    flash('All notifications deleted.', 'success')
    return redirect(url_for('all_notifications'))

# ═════════════════════════════════════════════
# ADMIN — Dashboard
# ═════════════════════════════════════════════
def _get_plan_subscriber_counts():
    """Count active+completed investments per plan_id in one query
    (avoids N+1 sb_count calls per plan)."""
    rows = sb_all('investments', filters=[('status', 'in', ['active', 'completed'])])
    counts = {}
    for r in rows:
        pid = r.get('plan_id')
        counts[pid] = counts.get(pid, 0) + 1
    return counts


def _get_recent_activity(limit=8):
    """Build a merged, time-sorted activity feed from existing tables only
    (users, deposits, withdrawals, investments, contact_messages). Reuses
    _join_users for display names instead of duplicating that lookup logic,
    and reuses get_recent_contact_messages() for the contact-support feed
    instead of querying contact_messages a second time."""
    recent_signups     = sb_all('users', filters=[('is_admin', 'eq', False)],
                                 order=('created_at', 'desc'), limit=limit)
    recent_deposits     = _join_users(sb_all('deposits', order=('created_at', 'desc'), limit=limit))
    recent_withdrawals  = _join_users(sb_all('withdrawals', order=('created_at', 'desc'), limit=limit))
    recent_investments  = _join_users(sb_all('investments', order=('created_at', 'desc'), limit=limit))
    recent_messages     = get_recent_contact_messages(limit=limit)

    events = []
    for u in recent_signups:
        events.append({'type': 'registration', 'title': 'New user registered',
                        'description': u.get('full_name', 'Unknown'),
                        'created_at': u.get('created_at')})
    for d in recent_deposits:
        events.append({'type': 'deposit', 'title': f"Deposit — {d.get('status','pending').title()}",
                        'description': f"{d.get('full_name','Unknown')} · ₦{r2(d.get('amount')):,.2f}",
                        'created_at': d.get('created_at')})
    for w in recent_withdrawals:
        events.append({'type': 'withdrawal', 'title': f"Withdrawal — {w.get('status','pending').title()}",
                        'description': f"{w.get('full_name','Unknown')} · ₦{r2(w.get('amount')):,.2f}",
                        'created_at': w.get('created_at')})
    for inv in recent_investments:
        events.append({'type': 'investment', 'title': f"Investment — {inv.get('plan_name','Plan')}",
                        'description': f"{inv.get('full_name','Unknown')} · ₦{r2(inv.get('amount')):,.2f}",
                        'created_at': inv.get('created_at')})
    for m in recent_messages:
        events.append({'type': 'contact', 'title': f"Contact message — {m.get('status','Unread')}",
                        'description': f"{m.get('full_name','Unknown')} · {m.get('subject','')}",
                        'created_at': m.get('created_at')})

    events.sort(key=lambda e: e.get('created_at') or '', reverse=True)
    return events[:limit]


@app.route('/admin')
@admin_required
def admin_dashboard():
    try:
        stats = {
            'total_users':         sb_count('users',       [('is_admin','eq',False)]),
            'total_invested':      sb_sum('investments',   'amount'),
            'total_deposits':      sb_sum('deposits',      'amount', [('status','eq','approved')]),
            'total_withdrawals':   sb_sum('withdrawals',   'amount', [('status','eq','approved')]),
            'pending_deposits':    sb_count('deposits',    [('status','eq','pending')]),
            'pending_withdrawals': sb_count('withdrawals', [('status','eq','pending')]),
            'total_paid_out':      sb_sum('withdrawals',   'amount', [('status','eq','approved')]),
            'active_investments':  sb_count('investments', [('status','eq','active')]),
            'total_plans':         sb_count('plans'),
            'referral_payout':     sb_sum('referrals',     'commission'),
            # pending_kyc / today_revenue / monthly_revenue intentionally
            # omitted — no KYC or platform-revenue data exists in the schema
            # yet. The template defaults these stat cards to 0 rather than
            # showing an invented number.

            # ── Additional real counts (not yet in the fixed admin_stats
            # card list, but made available on `stats` for any card/table
            # that references them) — all backed by real columns already
            # in the schema. 'Verified Users' is intentionally NOT included
            # here for the same reason as pending_kyc above: there is no
            # is_verified column anywhere in the schema, so it would be an
            # invented number rather than real data.
            'active_users':          sb_count('users', [('is_admin','eq',False),('is_active','eq',True)]),
            'total_investments':     sb_count('investments'),
            'completed_investments': sb_count('investments', [('status','eq','completed')]),
            'approved_deposits':     sb_count('deposits',    [('status','eq','approved')]),
            'rejected_deposits':     sb_count('deposits',    [('status','eq','rejected')]),
            'approved_withdrawals':  sb_count('withdrawals', [('status','eq','approved')]),
            'rejected_withdrawals':  sb_count('withdrawals', [('status','eq','rejected')]),
            'wallet_balance_total':  sb_sum('users', 'balance', [('is_admin','eq',False)]),
            'referral_earnings_total': sb_sum('users', 'referral_earnings', [('is_admin','eq',False)]),
        }
        # inactive_users derived in Python — avoids a 3rd users query
        stats['inactive_users'] = max(stats['total_users'] - stats['active_users'], 0)

        # Contact Support stats — reuses the existing get_contact_stats()
        # helper (already implemented) instead of duplicating its queries.
        stats.update(get_contact_stats())
    except Exception as e:
        print(f'Admin stats error: {e}')
        stats = {k: 0 for k in ['total_users','total_invested','total_deposits',
                                  'total_withdrawals','pending_deposits',
                                  'pending_withdrawals','total_paid_out',
                                  'active_investments','total_plans','referral_payout',
                                  'active_users','inactive_users','total_investments',
                                  'completed_investments','approved_deposits','rejected_deposits',
                                  'approved_withdrawals','rejected_withdrawals',
                                  'wallet_balance_total','referral_earnings_total',
                                  'total_messages','unread_messages','read_messages','replied_messages']}
    try:
        recent_users = sb_all('users', filters=[('is_admin','eq',False)],
                              order=('created_at','desc'), limit=10)
    except Exception:
        recent_users = []
    try:
        all_users = sb_all('users', filters=[('is_admin','eq',False)],
                           order=('created_at','desc'))
    except Exception:
        all_users = []
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
    try:
        raw_plans = sb_all('plans', order=[('sort_order','asc'),('id','asc')])
        sub_counts = _get_plan_subscriber_counts()
        dashboard_plans = []
        for p in raw_plans:
            ep = _enrich_plan(p)
            ep['subscribers'] = sub_counts.get(p['id'], 0)
            dashboard_plans.append(ep)
    except Exception as e:
        print(f'Admin plans error: {e}')
        dashboard_plans = []
    try:
        activities = _get_recent_activity(limit=8)
    except Exception as e:
        print(f'Admin activity error: {e}')
        activities = []

    # ── "Latest" lists (Latest Deposits / Withdrawals / Investments).
    # Same _join_users pattern already used for pending_deps/pending_wds
    # above — no new helper needed, just the existing building blocks
    # called with no status filter and a limit instead.
    try:
        latest_deposits = _join_users(sb_all('deposits', order=('created_at','desc'), limit=10))
    except Exception:
        latest_deposits = []
    try:
        latest_withdrawals = _join_users(sb_all('withdrawals', order=('created_at','desc'), limit=10))
    except Exception:
        latest_withdrawals = []
    try:
        latest_investments = _join_users(sb_all('investments', order=('created_at','desc'), limit=10))
    except Exception:
        latest_investments = []

    # ── Contact Support: latest messages for a future support-inbox
    # widget on this dashboard. Reuses the existing get_recent_contact_messages()
    # helper — same one _get_recent_activity() uses internally, so this is
    # a second, cheap call rather than a duplicated query implementation.
    try:
        recent_contact_messages = get_recent_contact_messages(limit=10)
    except Exception as e:
        print(f'Admin contact messages error: {e}')
        recent_contact_messages = []

    # ── Maintenance mode current state, for the dashboard toggle to
    # reflect reality (button label, status badge).
    try:
        maintenance_mode = is_maintenance_mode()
        maintenance_message = get_maintenance_message()
    except Exception as e:
        print(f'Admin maintenance-state error: {e}')
        maintenance_mode, maintenance_message = False, None

    try:
        deposit_trend = get_daily_trend('deposits', 7, status_filter='approved')
        withdrawal_trend = get_daily_trend('withdrawals', 7, status_filter='approved')
    except Exception as e:
        print(f'Admin chart trend error: {e}')
        deposit_trend, withdrawal_trend = [], []

    investment_breakdown = {
        'active': stats.get('active_investments', 0),
        'completed': stats.get('completed_investments', 0),
    }
    investment_breakdown['other'] = max(
        stats.get('total_investments', 0) - investment_breakdown['active'] - investment_breakdown['completed'], 0)

    return render_template('admin/dashboard.html',
        stats=stats, recent_users=recent_users, users=all_users,
        pending_deps=pending_deps, pending_wds=pending_wds,
        plans=dashboard_plans, activities=activities,
        latest_deposits=latest_deposits, latest_withdrawals=latest_withdrawals,
        latest_investments=latest_investments,
        recent_contact_messages=recent_contact_messages,
        maintenance_mode=maintenance_mode, maintenance_message=maintenance_message,
        deposit_trend=deposit_trend, withdrawal_trend=withdrawal_trend,
        investment_breakdown=investment_breakdown)


@app.route('/admin/messages')
@admin_required
def admin_messages():
    """Admin inbox for Contact Support messages. THIS ROUTE WAS MISSING —
    admin/base.html's nav already links to url_for('admin_messages'), which
    raised werkzeug.routing.exceptions.BuildError on every admin page render
    (including right after admin login), producing the reported HTTP 500.
    Normal users never touch admin/base.html, so they were unaffected.

    Reuses get_contact_stats() and resolve_contact_attachment_url() — both
    already existed as data helpers, just never had a route wired to them.
    """
    messages = sb_all('contact_messages', order=('created_at', 'desc'))
    for m in messages:
        m['attachment_url'] = resolve_contact_attachment_url(m.get('attachment'))
    stats = get_contact_stats()
    return render_template('admin/messages.html', messages=messages, stats=stats)


@app.route('/admin/messages/<int:msg_id>/read', methods=['POST'])
@admin_required
def admin_mark_message_read(msg_id):
    sb_update('contact_messages', {'status': 'Read'},
              [('id', 'eq', msg_id), ('status', 'eq', 'Unread')])
    return redirect(url_for('admin_messages'))


@app.route('/admin/messages/<int:msg_id>/replied', methods=['POST'])
@admin_required
def admin_mark_message_replied(msg_id):
    sb_update('contact_messages', {'status': 'Replied'}, [('id', 'eq', msg_id)])
    return redirect(url_for('admin_messages'))


@app.route('/admin/maintenance/toggle', methods=['POST'])
@admin_required
def admin_toggle_maintenance():
    """Flip maintenance mode on/off instantly (DB-backed, no redeploy).
    Reuses set_setting()/is_maintenance_mode() — see check_maintenance()."""
    turning_on = not is_maintenance_mode()
    message = request.form.get('maintenance_message', '').strip()
    ok = set_setting('maintenance_mode', 'true' if turning_on else 'false')
    if ok and turning_on and message:
        set_setting('maintenance_message', message)
    if ok:
        flash('Maintenance mode turned ON — the site is now unavailable to non-admins.'
              if turning_on else
              'Maintenance mode turned OFF — the site is live again.',
              'warning' if turning_on else 'success')
    else:
        flash('Could not update maintenance mode — check the logs.', 'error')
    return redirect(url_for('admin_dashboard'))


# ═════════════════════════════════════════════
# ADMIN — Payment Settings & Bank Accounts
# ═════════════════════════════════════════════
@app.route('/admin/payment-settings', methods=['GET', 'POST'])
@admin_required
def admin_payment_settings():
    """Single page: global payment toggles (deposit instructions, withdrawal
    fee %, payment status) plus the list of bank accounts. Everything here
    is stored in Supabase (`settings` + `bank_accounts`) — nothing is
    hardcoded, and changes apply instantly with no redeploy."""
    if request.method == 'POST':
        deposit_instructions = request.form.get('deposit_instructions', '').strip()
        fee_raw = request.form.get('withdrawal_fee_percent', '0').strip()
        payment_status = 'disabled' if request.form.get('payment_status') == 'disabled' else 'enabled'

        try:
            fee_percent = max(0.0, float(fee_raw or 0))
        except ValueError:
            flash('Withdrawal fee must be a number.', 'error')
            return redirect(url_for('admin_payment_settings'))

        set_setting('deposit_instructions', deposit_instructions)
        set_setting('withdrawal_fee_percent', fee_percent)
        set_setting('payment_status', payment_status)
        flash('Payment settings saved.', 'success')
        return redirect(url_for('admin_payment_settings'))

    return render_template('admin/payment_settings.html',
                           bank_accounts=get_bank_accounts(),
                           deposit_instructions=get_deposit_instructions(),
                           withdrawal_fee_percent=get_withdrawal_fee_percent(),
                           payments_enabled=is_payments_enabled())


@app.route('/admin/payment-settings/bank-accounts/add', methods=['GET', 'POST'])
@admin_required
def admin_bank_account_add():
    if request.method == 'POST':
        data, error = _bank_account_from_form(request.form)
        if error:
            flash(error, 'error')
            return render_template('admin/bank_account_form.html', account=None, action='add')

        # First account ever added becomes active automatically so
        # payment.html always has something to show.
        if not sb_count('bank_accounts'):
            data['is_active'] = True

        sb_insert('bank_accounts', data)
        flash(f'{data["bank_name"]} account added!', 'success')
        return redirect(url_for('admin_payment_settings'))

    return render_template('admin/bank_account_form.html', account=None, action='add')


@app.route('/admin/payment-settings/bank-accounts/<int:account_id>/edit', methods=['GET', 'POST'])
@admin_required
def admin_bank_account_edit(account_id):
    account = sb_one('bank_accounts', [('id', 'eq', account_id)])
    if not account:
        flash('Bank account not found.', 'error')
        return redirect(url_for('admin_payment_settings'))

    if request.method == 'POST':
        data, error = _bank_account_from_form(request.form)
        if error:
            flash(error, 'error')
            return render_template('admin/bank_account_form.html', account=account, action='edit')

        sb_update('bank_accounts', data, [('id', 'eq', account_id)])
        flash(f'{data["bank_name"]} account updated!', 'success')
        return redirect(url_for('admin_payment_settings'))

    return render_template('admin/bank_account_form.html', account=account, action='edit')


def _bank_account_from_form(form):
    """Parse and validate the bank account form. Returns (data_dict, error_str)."""
    bank_name      = form.get('bank_name', '').strip()
    account_number = form.get('account_number', '').strip()
    account_name   = form.get('account_name', '').strip()
    logo_color     = form.get('logo_color', '').strip() or '#0f3d2e'
    sort_order     = form.get('sort_order', 0, type=int)

    if not bank_name:
        return None, 'Bank name is required.'
    if not account_number or not re.match(r'^\d{10}$', account_number):
        return None, 'Account number must be exactly 10 digits.'
    if not account_name:
        return None, 'Account name is required.'

    return {
        'bank_name': bank_name, 'account_number': account_number,
        'account_name': account_name, 'logo_color': logo_color,
        'sort_order': sort_order,
    }, None


@app.route('/admin/payment-settings/bank-accounts/<int:account_id>/activate', methods=['POST'])
@admin_required
def admin_bank_account_activate(account_id):
    """Marks one account active and every other account inactive, so
    payment.html (which only ever shows the active account) always has
    exactly one unambiguous choice."""
    account = sb_one('bank_accounts', [('id', 'eq', account_id)])
    if not account:
        flash('Bank account not found.', 'error')
        return redirect(url_for('admin_payment_settings'))

    sb_update('bank_accounts', {'is_active': False}, [('is_active', 'eq', True)])
    sb_update('bank_accounts', {'is_active': True}, [('id', 'eq', account_id)])
    flash(f'{account["bank_name"]} is now the active account shown to users.', 'success')
    return redirect(url_for('admin_payment_settings'))


@app.route('/admin/payment-settings/bank-accounts/<int:account_id>/delete', methods=['POST'])
@admin_required
def admin_bank_account_delete(account_id):
    account = sb_one('bank_accounts', [('id', 'eq', account_id)])
    if not account:
        flash('Bank account not found.', 'error')
        return redirect(url_for('admin_payment_settings'))

    sb_delete('bank_accounts', [('id', 'eq', account_id)])

    # If the deleted account was the active one, promote the next available
    # account automatically so users are never left with no account shown.
    if account.get('is_active'):
        remaining = sb_all('bank_accounts', order=[('sort_order', 'asc'), ('id', 'asc')], limit=1)
        if remaining:
            sb_update('bank_accounts', {'is_active': True}, [('id', 'eq', remaining[0]['id'])])

    flash(f'{account["bank_name"]} account deleted.', 'success')
    return redirect(url_for('admin_payment_settings'))


# ═════════════════════════════════════════════
# ADMIN — Gift Codes
# ═════════════════════════════════════════════
@app.route('/admin/gift-codes')
@admin_required
def admin_gift_codes():
    codes = sb_all('gift_codes', order=('created_at', 'desc'))
    return render_template('admin/gift_codes.html', codes=codes)


def _gift_code_from_form(form):
    """Parse and validate the gift code form. Returns (data_dict, error_str)."""
    code = (form.get('code') or '').strip().upper() or generate_gift_code()
    try:
        reward_amount = float(form.get('reward_amount', 0) or 0)
    except ValueError:
        return None, 'Reward amount must be a number.'
    if reward_amount <= 0:
        return None, 'Reward amount must be greater than zero.'

    try:
        usage_limit = int(form.get('usage_limit', 1) or 1)
    except ValueError:
        return None, 'Usage limit must be a whole number.'
    if usage_limit < 1:
        return None, 'Usage limit must be at least 1.'

    expires_raw = (form.get('expires_at') or '').strip()
    expires_at = None
    if expires_raw:
        try:
            # <input type="datetime-local"> submits a naive string (no
            # timezone offset). Treat it as the admin-configured display
            # timezone — not UTC — so "expires at midnight" means midnight
            # in the admin's own timezone, then store as UTC like every
            # other timestamp in this app.
            naive_dt = datetime.fromisoformat(expires_raw)
            local_dt = naive_dt.replace(tzinfo=get_display_timezone())
            expires_at = local_dt.astimezone(timezone.utc).isoformat()
        except ValueError:
            return None, 'Expiry date is invalid.'

    return {
        'code': code, 'reward_amount': r2(reward_amount),
        'usage_limit': usage_limit, 'expires_at': expires_at,
    }, None


@app.route('/admin/gift-codes/add', methods=['GET', 'POST'])
@admin_required
def admin_gift_code_add():
    if request.method == 'POST':
        data, error = _gift_code_from_form(request.form)
        if error:
            flash(error, 'error')
            return render_template('admin/gift_code_form.html', code=None, action='add')

        created = sb_insert('gift_codes', data)
        if not created:
            flash('That code already exists — try a different one.', 'error')
            return render_template('admin/gift_code_form.html', code=None, action='add')

        flash(f'Gift code {data["code"]} created!', 'success')
        return redirect(url_for('admin_gift_codes'))

    return render_template('admin/gift_code_form.html', code=None, action='add', suggested_code=generate_gift_code())


@app.route('/admin/gift-codes/<int:code_id>/edit', methods=['GET', 'POST'])
@admin_required
def admin_gift_code_edit(code_id):
    code = sb_one('gift_codes', [('id', 'eq', code_id)])
    if not code:
        flash('Gift code not found.', 'error')
        return redirect(url_for('admin_gift_codes'))

    if request.method == 'POST':
        data, error = _gift_code_from_form(request.form)
        if error:
            flash(error, 'error')
            return render_template('admin/gift_code_form.html', code=code, action='edit')

        sb_update('gift_codes', data, [('id', 'eq', code_id)])
        flash(f'Gift code {data["code"]} updated!', 'success')
        return redirect(url_for('admin_gift_codes'))

    return render_template('admin/gift_code_form.html', code=code, action='edit')


@app.route('/admin/gift-codes/<int:code_id>/toggle', methods=['POST'])
@admin_required
def admin_gift_code_toggle(code_id):
    code = sb_one('gift_codes', [('id', 'eq', code_id)])
    if not code:
        flash('Gift code not found.', 'error')
        return redirect(url_for('admin_gift_codes'))

    sb_update('gift_codes', {'is_active': not code.get('is_active')}, [('id', 'eq', code_id)])
    flash(f'{code["code"]} is now {"active" if not code.get("is_active") else "inactive"}.', 'success')
    return redirect(url_for('admin_gift_codes'))


@app.route('/admin/gift-codes/<int:code_id>/delete', methods=['POST'])
@admin_required
def admin_gift_code_delete(code_id):
    code = sb_one('gift_codes', [('id', 'eq', code_id)])
    if not code:
        flash('Gift code not found.', 'error')
        return redirect(url_for('admin_gift_codes'))

    sb_delete('gift_codes', [('id', 'eq', code_id)])
    flash(f'Gift code {code["code"]} deleted.', 'success')
    return redirect(url_for('admin_gift_codes'))


# ═════════════════════════════════════════════
# ADMIN — Daily Check-in monitoring
# ═════════════════════════════════════════════
@app.route('/admin/checkins', methods=['GET', 'POST'])
@admin_required
def admin_checkins():
    """Configure the daily reward amount and see recent check-in activity."""
    if request.method == 'POST':
        try:
            amount = max(0.0, float(request.form.get('checkin_reward_amount', 50) or 50))
            set_setting('checkin_reward_amount', amount)
            flash('Check-in reward updated.', 'success')
        except ValueError:
            flash('Reward amount must be a number.', 'error')
        return redirect(url_for('admin_checkins'))

    recent = sb_all('checkins', order=('created_at', 'desc'), limit=100)
    user_ids = list({r['user_id'] for r in recent})
    users_by_id = {}
    if user_ids:
        for u in sb_all('users', filters=[('id', 'in', user_ids)]):
            users_by_id[u['id']] = u
    for r in recent:
        u = users_by_id.get(r['user_id'])
        r['user_name'] = u['full_name'] if u else 'Unknown user'
        r['user_email'] = u['email'] if u else ''

    return render_template('admin/checkins.html', checkins=recent,
                           checkin_reward_amount=get_checkin_reward_amount(),
                           total_checkins=sb_count('checkins'))


@app.route('/admin/announcements', methods=['GET', 'POST'])
@admin_required
def admin_announcements():
    """One active announcement at a time, shown sitewide via the banner in
    base.html. Publishing a new one replaces whatever was there before."""
    if request.method == 'POST':
        if request.form.get('action') == 'clear':
            set_setting('notice_enabled', 'false')
            flash('Announcement cleared.', 'success')
            return redirect(url_for('admin_announcements'))

        message = request.form.get('notice_message', '').strip()
        ntype = request.form.get('notice_type', 'info').strip()
        if ntype not in NOTICE_TYPES:
            ntype = 'info'
        if not message:
            flash('Announcement message is required.', 'error')
            return redirect(url_for('admin_announcements'))

        set_setting('notice_message', message)
        set_setting('notice_type', ntype)
        set_setting('notice_enabled', 'true')
        flash('Announcement published — now visible sitewide.', 'success')
        return redirect(url_for('admin_announcements'))

    is_enabled = (get_setting('notice_enabled', 'false') or 'false').strip().lower() == 'true'
    return render_template('admin/announcements.html',
                           notice_types=NOTICE_TYPES,
                           notice_enabled=is_enabled,
                           notice_message=get_setting('notice_message', '') or '',
                           notice_type=get_setting('notice_type', 'info') or 'info')


# ═════════════════════════════════════════════
# ADMIN — Announcements CRUD (Phase 10)
# ═════════════════════════════════════════════
@app.route('/admin/announcement-list')
@admin_required
def admin_announcement_list():
    all_announcements = sb_all('announcements', order=('created_at', 'desc'))
    now = datetime.utcnow()
    for a in all_announcements:
        sched = a.get('scheduled_at')
        a['is_scheduled_future'] = False
        if sched:
            try:
                sched_dt = datetime.fromisoformat(str(sched).replace('Z', '+00:00')).replace(tzinfo=None)
                a['is_scheduled_future'] = sched_dt > now
            except Exception:
                pass
        reads = sb_count('announcement_reads', [('announcement_id', 'eq', a['id'])])
        a['read_count'] = reads
        a['image_url'] = resolve_plan_image_url(a.get('image_filename'))
    users = sb_all('users', filters=[('is_admin', 'eq', False)], order=('full_name', 'asc'))
    return render_template('admin/announcement_list.html', announcements=all_announcements, users=users)


@app.route('/admin/announcement-list/add', methods=['POST'])
@admin_required
def admin_announcement_add():
    data, error = _announcement_from_form(request.form, request.files)
    if error:
        flash(error, 'error')
        return redirect(url_for('admin_announcement_list'))
    data['created_by'] = session.get('user_id')
    sb_insert('announcements', data)
    flash(f'Announcement "{data["title"]}" published — now live on every user\'s dashboard.', 'success')
    return redirect(url_for('admin_announcement_list'))


@app.route('/admin/announcement-list/<int:announcement_id>/edit', methods=['POST'])
@admin_required
def admin_announcement_edit(announcement_id):
    existing = sb_one('announcements', [('id', 'eq', announcement_id)])
    if not existing:
        flash('Announcement not found.', 'error')
        return redirect(url_for('admin_announcement_list'))
    data, error = _announcement_from_form(request.form, request.files, existing=existing)
    if error:
        flash(error, 'error')
        return redirect(url_for('admin_announcement_list'))
    sb_update('announcements', data, [('id', 'eq', announcement_id)])
    flash(f'Announcement "{data["title"]}" updated.', 'success')
    return redirect(url_for('admin_announcement_list'))


@app.route('/admin/announcement-list/<int:announcement_id>/delete', methods=['POST'])
@admin_required
def admin_announcement_delete(announcement_id):
    announcement = sb_one('announcements', [('id', 'eq', announcement_id)])
    if not announcement:
        flash('Announcement not found.', 'error')
        return redirect(url_for('admin_announcement_list'))
    sb_delete('announcements', [('id', 'eq', announcement_id)])
    flash(f'Announcement "{announcement["title"]}" deleted.', 'success')
    return redirect(url_for('admin_announcement_list'))


def _announcement_from_form(form, files=None, existing=None):
    """Parse and validate the announcement create/edit form. Returns
    (data_dict, error_str). `existing` is the current row when editing —
    used to keep the current image if no new one is uploaded, same
    pattern as _plan_from_form()."""
    title   = form.get('title', '').strip()
    message = form.get('message', '').strip()
    is_important = form.get('is_important') == 'on'
    target_type = form.get('target_type', 'all').strip()
    if target_type not in ('all', 'selected'):
        target_type = 'all'

    if not title:
        return None, 'Title is required.'
    if not message:
        return None, 'Message is required.'

    target_user_ids = None
    if target_type == 'selected':
        raw_ids = form.getlist('target_user_ids')
        try:
            target_user_ids = [int(i) for i in raw_ids]
        except ValueError:
            return None, 'Invalid recipient selection.'
        if not target_user_ids:
            return None, 'Select at least one recipient for a targeted announcement.'

    scheduled_raw = form.get('scheduled_at', '').strip()
    scheduled_at = None
    if scheduled_raw:
        try:
            naive_dt = datetime.fromisoformat(scheduled_raw)
            local_dt = naive_dt.replace(tzinfo=get_display_timezone())
            scheduled_at = local_dt.astimezone(timezone.utc).isoformat()
        except ValueError:
            return None, 'Scheduled date/time is invalid.'

    # Optional popup image — keeps the current one on edit unless a new
    # file is uploaded, same pattern as plan images.
    image_filename = existing.get('image_filename') if existing else None
    image_file = files.get('image') if files else None
    if image_file and image_file.filename:
        if not is_allowed_plan_image(image_file.filename):
            return None, 'Announcement image must be a PNG, JPG, JPEG or WEBP file.'
        image_filename = upload_plan_image(image_file)

    telegram_channel_url = form.get('telegram_channel_url', '').strip() or None
    telegram_group_url   = form.get('telegram_group_url', '').strip() or None
    learn_more_url        = form.get('learn_more_url', '').strip() or None

    return {
        'title': title, 'message': message, 'is_important': is_important,
        'target_type': target_type, 'target_user_ids': target_user_ids,
        'scheduled_at': scheduled_at, 'image_filename': image_filename,
        'telegram_channel_url': telegram_channel_url,
        'telegram_group_url': telegram_group_url,
        'learn_more_url': learn_more_url,
    }, None


# ═════════════════════════════════════════════
# ADMIN — Banner Manager (Phase 11)
# ═════════════════════════════════════════════
@app.route('/admin/banners')
@admin_required
def admin_banners():
    all_banners = sb_all('banners', order=[('sort_order', 'asc'), ('id', 'asc')])
    for b in all_banners:
        b['image_url'] = resolve_plan_image_url(b.get('image_filename'))
    return render_template('admin/banners.html', banners=all_banners)


@app.route('/admin/banners/add', methods=['POST'])
@admin_required
def admin_banner_add():
    image_file = request.files.get('image')
    if not image_file or not image_file.filename:
        flash('Please choose an image to upload.', 'error')
        return redirect(url_for('admin_banners'))
    if not is_allowed_plan_image(image_file.filename):
        flash('Banner image must be a PNG, JPG, JPEG or WEBP file.', 'error')
        return redirect(url_for('admin_banners'))

    image_filename = upload_plan_image(image_file)
    if not image_filename:
        flash('Could not upload that image — please try again.', 'error')
        return redirect(url_for('admin_banners'))

    title    = request.form.get('title', '').strip()
    link_url = request.form.get('link_url', '').strip()
    sort_order = request.form.get('sort_order', 0, type=int)

    # First banner ever added is active automatically so the dashboard
    # slider has something to show as soon as one exists.
    is_active = not sb_count('banners')

    sb_insert('banners', {
        'image_filename': image_filename, 'title': title or None,
        'link_url': link_url or None, 'sort_order': sort_order,
        'is_active': is_active,
    })
    flash('Banner added.', 'success')
    return redirect(url_for('admin_banners'))


@app.route('/admin/banners/<int:banner_id>/toggle', methods=['POST'])
@admin_required
def admin_banner_toggle(banner_id):
    banner = sb_one('banners', [('id', 'eq', banner_id)])
    if not banner:
        flash('Banner not found.', 'error')
        return redirect(url_for('admin_banners'))
    sb_update('banners', {'is_active': not banner.get('is_active')}, [('id', 'eq', banner_id)])
    flash(f'Banner {"activated" if not banner.get("is_active") else "deactivated"}.', 'success')
    return redirect(url_for('admin_banners'))


@app.route('/admin/banners/<int:banner_id>/reorder', methods=['POST'])
@admin_required
def admin_banner_reorder(banner_id):
    """Move a banner up or down in slide order by swapping sort_order with
    its neighbor — simple and safe, avoids needing to renumber the whole
    list on every move."""
    direction = request.form.get('direction')
    all_banners = sb_all('banners', order=[('sort_order', 'asc'), ('id', 'asc')])
    idx = next((i for i, b in enumerate(all_banners) if b['id'] == banner_id), None)
    if idx is None:
        flash('Banner not found.', 'error')
        return redirect(url_for('admin_banners'))

    swap_idx = idx - 1 if direction == 'up' else idx + 1
    if 0 <= swap_idx < len(all_banners):
        a, b = all_banners[idx], all_banners[swap_idx]
        sb_update('banners', {'sort_order': b['sort_order']}, [('id', 'eq', a['id'])])
        sb_update('banners', {'sort_order': a['sort_order']}, [('id', 'eq', b['id'])])
    return redirect(url_for('admin_banners'))


@app.route('/admin/banners/<int:banner_id>/delete', methods=['POST'])
@admin_required
def admin_banner_delete(banner_id):
    if not sb_one('banners', [('id', 'eq', banner_id)]):
        flash('Banner not found.', 'error')
        return redirect(url_for('admin_banners'))
    sb_delete('banners', [('id', 'eq', banner_id)])
    flash('Banner deleted.', 'success')
    return redirect(url_for('admin_banners'))


@app.route('/admin/settings', methods=['GET', 'POST'])
@admin_required
def admin_settings():
    """Full site settings (Phase 11) — every field is stored in Supabase via
    set_setting() and read back via get_site_settings()/get_setting(), so
    nothing here is ever hardcoded. Saving simply upserts each changed key;
    unrecognised/blank fields silently keep their previous value."""
    if request.method == 'POST':
        text_fields = [
            'site_name', 'logo_url', 'favicon_url', 'support_email',
            'whatsapp_link', 'telegram_link', 'facebook_link',
            'instagram_link', 'twitter_link', 'office_address',
            'currency_code', 'currency_symbol', 'timezone',
            'seo_title', 'seo_description', 'og_image_url',
        ]
        for field in text_fields:
            set_setting(field, request.form.get(field, '').strip())

        # Numeric fields validated before saving so a typo can't silently
        # corrupt referral payouts or withdrawal limits.
        try:
            referral_percent = max(0.0, float(request.form.get('referral_percent', '5') or 5))
            set_setting('referral_percent', referral_percent)
        except ValueError:
            flash('Referral % must be a number — that field was not saved.', 'error')

        try:
            min_wd = max(0.0, float(request.form.get('min_withdrawal', '2000') or 2000))
            max_wd = max(0.0, float(request.form.get('max_withdrawal', '1000000') or 1000000))
            if max_wd < min_wd:
                flash('Maximum withdrawal must be greater than the minimum — withdrawal limits were not saved.', 'error')
            else:
                set_setting('min_withdrawal', min_wd)
                set_setting('max_withdrawal', max_wd)
        except ValueError:
            flash('Withdrawal limits must be numbers — those fields were not saved.', 'error')

        flash('Settings saved.', 'success')
        return redirect(url_for('admin_settings'))

    return render_template('admin/settings.html',
                           settings=get_site_settings(),
                           maintenance_mode=is_maintenance_mode())


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


@app.route('/admin/notifications/send', methods=['POST'])
@admin_required
@csrf.exempt
# Exempt rather than requiring a CSRF token in the fetch() call: this is a
# JSON POST (Content-Type: application/json), which browsers refuse to send
# cross-origin without a CORS preflight — and this app sends no CORS headers,
# so a third-party site can't trigger this request in the first place. Still
# guarded by @admin_required (session auth), same as every other admin route.
def admin_send_notification():
    """Backs the dashboard's 'Send Notification' panel. Sends either to a
    single selected user or broadcasts to every non-admin user, reusing
    notify()/notify_bulk() so this creates rows exactly like every other
    notification in the app — same table, same shape, same bell dropdown."""
    data = request.get_json(silent=True) or {}
    title          = (data.get('title') or '').strip()
    message        = (data.get('message') or '').strip()
    ntype          = (data.get('type') or 'info').strip() or 'info'
    recipient_mode = data.get('recipient_mode') or 'all'
    target_user    = data.get('user') or {}

    if not title or not message:
        return jsonify({'status': 'error', 'error': 'Title and message are required.'}), 400

    # The notifications table only has a `message` column (no separate
    # `title`) — reusing it as-is rather than adding a column, so the title
    # is folded into the stored message text.
    full_message = f'{title}: {message}'

    if recipient_mode == 'single':
        uid = target_user.get('id')
        if not uid:
            return jsonify({'status': 'error', 'error': 'No recipient selected.'}), 400
        if not sb_one('users', [('id', 'eq', uid)]):
            return jsonify({'status': 'error', 'error': 'User not found.'}), 404
        notify(uid, full_message, ntype)
        return jsonify({'status': 'ok', 'sent_count': 1})

    recipients = sb_all('users', filters=[('is_admin', 'eq', False)])
    sent_count = notify_bulk([u['id'] for u in recipients], full_message, ntype)
    return jsonify({'status': 'ok', 'sent_count': sent_count})


# ═════════════════════════════════════════════
# ADMIN — Plans CRUD
# ═════════════════════════════════════════════
@app.route('/admin/plans')
@admin_required
def admin_plans():
    all_plans = [_enrich_plan(p) for p in sb_all('plans', order=[('sort_order','asc'),('id','asc')])]
    return render_template('admin/plans.html', plans=all_plans)


@app.route('/admin/plans/add', methods=['GET', 'POST'])
@admin_required
def admin_plan_add():
    if request.method == 'POST':
        data, error = _plan_from_form(request.form, request.files)
        if error:
            flash(error, 'error')
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
        data, error = _plan_from_form(request.form, request.files, existing=plan)
        if error:
            flash(error, 'error')
            plan_d = _enrich_plan(plan)
            plan_d['features_text'] = (plan_d.get('features') or '').replace('|','\n')
            return render_template('admin/plan_form.html', plan=plan_d, action='edit')

        sb_update('plans', data, [('id','eq',plan_id)])
        flash(f'Plan "{data["name"]}" updated!', 'success')
        return redirect(url_for('admin_plans'))

    plan_d = _enrich_plan(plan)
    plan_d['features_text'] = (plan_d.get('features') or '').replace('|','\n')
    return render_template('admin/plan_form.html', plan=plan_d, action='edit')


def _plan_from_form(form, files=None, existing=None):
    """Parse and validate plan form. Returns (data_dict, error_str).

    `existing` is the current plan row (when editing) — used to keep the
    current image if no new one was uploaded, and to clear it if requested.
    `files` is request.files, used for the optional image upload.
    """
    name          = form.get('name','').strip()
    description   = form.get('description','').strip()
    price         = r2(form.get('price', 0, type=float))
    total_return  = r2(form.get('total_return', 0, type=float))
    duration_days = form.get('duration_days', 0, type=int)
    features_raw  = form.get('features','').strip()
    sort_order    = form.get('sort_order', 0, type=int)
    is_active     = form.get('is_active') == 'on'
    is_popular    = form.get('is_popular') == 'on'
    is_featured   = form.get('is_featured') == 'on'
    remove_image  = form.get('remove_image') == 'on'

    # Investor quota — optional. Blank means unlimited (no quota/progress
    # bar shown on the plans page at all, matching current behavior exactly
    # for any plan an admin doesn't set this on).
    max_investors_raw = form.get('max_investors', '').strip()
    max_investors = None
    if max_investors_raw:
        try:
            max_investors = max(0, int(max_investors_raw))
        except ValueError:
            return None, 'Investor quota must be a whole number.'

    if not name:
        return None, 'Plan name is required.'
    if price <= 0:
        return None, 'Price must be greater than 0.'
    if total_return <= 0:
        return None, 'Total return must be greater than 0.'
    if duration_days <= 0:
        return None, 'Duration must be at least 1 day.'

    # Image: keep the current one by default, replace it if a new file was
    # uploaded, or clear it if "remove image" was checked with no replacement.
    image_filename = existing.get('image_filename') if existing else None
    image_file = files.get('image') if files else None
    if image_file and image_file.filename:
        if not is_allowed_plan_image(image_file.filename):
            return None, 'Plan image must be a PNG, JPG, JPEG or WEBP file.'
        image_filename = upload_plan_image(image_file)
    elif remove_image:
        image_filename = None

    features = '|'.join([f.strip() for f in features_raw.splitlines() if f.strip()])

    return {
        'name': name, 'description': description,
        'price': price, 'total_return': total_return,
        'image_filename': image_filename,
        'duration_days': duration_days, 'features': features,
        'sort_order': sort_order, 'is_active': is_active,
        'is_popular': is_popular, 'is_featured': is_featured,
        'max_investors': max_investors,
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


@app.route('/admin/withdrawals/bulk-approve', methods=['POST'])
@admin_required
def admin_bulk_approve_withdrawals():
    """Bulk Actions (Phase 11) — approves multiple pending withdrawals in
    one request. Reuses the exact same atomic compare-and-swap pattern as
    the single-approve route above (one at a time, in a loop) rather than
    a different bulk-specific code path, so the safety guarantees are
    identical: each withdrawal can only be approved once, and a failure
    on one doesn't roll back the others."""
    ids = request.form.getlist('withdrawal_ids', type=int)
    if not ids:
        flash('No withdrawals selected.', 'error')
        return redirect(url_for('admin_withdrawals'))

    approved_count = 0
    for wd_id in ids:
        wd = sb_one('withdrawals', [('id', 'eq', wd_id)])
        if not wd or wd['status'] != 'pending':
            continue
        amount = r2(wd['amount'])
        updated = sb_update('withdrawals', {'status': 'approved'},
                             [('id', 'eq', wd_id), ('status', 'eq', 'pending')])
        if updated:
            if sb_adjust_balance(wd['user_id'], total_earnings_delta=amount):
                notify(wd['user_id'], f'Withdrawal of ₦{amount:,.2f} approved and sent!', 'success')
                approved_count += 1

    flash(f'{approved_count} of {len(ids)} withdrawal(s) approved.',
          'success' if approved_count == len(ids) else 'warning')
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
        expected_return     = r2(inv['expected_return'])
        already_paid_profit = r2(inv.get('total_profit'))
        # Only pay out what the daily-profit engine hasn't already credited,
        # so early/manual completion never double-pays profit on top of the
        # automatic daily credits.
        remaining_payout = r2(expected_return - already_paid_profit)
        profit = r2(remaining_payout - float(inv['amount']))
        updated = sb_update('investments', {'status':'completed'},
                             [('id','eq',inv_id), ('status','eq','active')])
        if updated:
            if sb_adjust_balance(inv['user_id'], balance_delta=remaining_payout,
                                  total_earnings_delta=profit):
                notify(inv['user_id'],
                       f'{inv["plan_name"]} matured! ₦{remaining_payout:,.2f} credited.',
                       'success')
                flash('Investment completed and balance credited.', 'success')
            else:
                flash('Investment marked completed, but crediting the balance failed — '
                      'please credit the user manually and check the logs.', 'error')
        else:
            flash('This investment was already processed.', 'warning')
    return redirect(url_for('admin_investments'))


# ═════════════════════════════════════════════
# Daily Profit Cron Endpoint
#
# HOW TO SCHEDULE (pick any one):
#
# Option A — Render Cron Jobs (recommended for Render deploys)
#   Render Dashboard → your service → "Cron Jobs" tab → Add Cron Job
#   Command : curl -s -o /dev/null "https://<your-domain>/cron/daily-profits?key=$CRON_SECRET"
#   Schedule: 0 1 * * *    (runs at 01:00 UTC every day)
#
# Option B — GitHub Actions (free, reliable)
#   .github/workflows/daily_profits.yml:
#     on:
#       schedule:
#         - cron: '0 1 * * *'
#     jobs:
#       trigger:
#         runs-on: ubuntu-latest
#         steps:
#           - run: curl -fsS "$APP_URL/cron/daily-profits?key=$CRON_SECRET"
#             env:
#               APP_URL: ${{ secrets.APP_URL }}
#               CRON_SECRET: ${{ secrets.CRON_SECRET }}
#
# Option C — cron-job.org (free external pinger, zero infrastructure)
#   URL  : https://<your-domain>/cron/daily-profits?key=<CRON_SECRET>
#   Time : Daily at 01:00 UTC
#   Method: GET
#
# The endpoint is intentionally idempotent — it is safe to call it
# more than once a day. investments already credited for the day are
# simply skipped (elapsed_days == 0). The dashboard lazy-catch-up in
# run_daily_profit_distribution(user_id=uid) is a belt-and-suspenders
# fallback for users who don't rely on the cron alone.
#
# REQUIRED ENV VAR: CRON_SECRET — set in Render → Environment.
# Requests without the correct key receive a 403.
# ═════════════════════════════════════════════
@app.route('/cron/daily-profits', methods=['GET', 'POST'])
@csrf.exempt
def cron_daily_profits():
    secret = os.environ.get('CRON_SECRET', '').strip()
    provided = request.args.get('key', '') or request.headers.get('X-Cron-Secret', '')
    if not secret or provided != secret:
        abort(403)
    started_at = datetime.utcnow()
    processed  = run_daily_profit_distribution()
    elapsed_ms = int((datetime.utcnow() - started_at).total_seconds() * 1000)
    return jsonify({
        'status':                'ok',
        'investments_processed': processed,
        'elapsed_ms':            elapsed_ms,
        'run_at':                started_at.isoformat() + 'Z',
    })



@app.route('/admin/debug')
@admin_required
def admin_debug():
    """Shows live connection status and any errors. Check Render logs for detail."""
    results = {}
    url = os.environ.get('SUPABASE_URL', '').strip()
    key = os.environ.get('SUPABASE_SECRET_KEY', '').strip()
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
    symbol = get_setting('currency_symbol', '₦') or '₦'
    try:
        return f'{symbol}{float(value or 0):,.2f}'
    except (TypeError, ValueError):
        return f'{symbol}0.00'

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
        if value.tzinfo is not None:
            value = value.astimezone(get_display_timezone())
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
    key = os.environ.get('SUPABASE_SECRET_KEY', '').strip()
    if url and key and url.startswith('https://'):
        try:
            init_db()
            print("✓ AgroVest Pro started with Supabase connection")
        except Exception as _e:
            print(f"⚠ DB seed skipped (tables may not exist yet — run supabase_setup.sql): {_e}")
    else:
        print("⚠ SUPABASE_URL / SUPABASE_SECRET_KEY not set — visit /setup for instructions")

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=5000)
