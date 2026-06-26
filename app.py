"""
AgroVest Pro - Agricultural Investment Platform
Backend: Flask + Supabase (PostgreSQL via psycopg2)
"""

import os
import uuid
import traceback
from datetime import datetime, timedelta
from functools import wraps

import psycopg2
import psycopg2.extras
from flask import (Flask, render_template, request, redirect, url_for,
                   flash, session, g, jsonify, abort, send_from_directory)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

# Load .env in development
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ─────────────────────────────────────────────
# App Configuration
# ─────────────────────────────────────────────
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'agrovest-dev-secret-change-in-prod')
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=2)
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024  # 5MB

# Upload folder — /tmp on Render, local otherwise
_DATA_DIR = '/tmp/agrovest_data' if os.environ.get('RENDER_TMP') else os.path.join(app.root_path, 'static', 'uploads')
os.makedirs(_DATA_DIR if os.environ.get('RENDER_TMP') else _DATA_DIR, exist_ok=True)
app.config['UPLOAD_FOLDER'] = _DATA_DIR if os.environ.get('RENDER_TMP') else os.path.join(app.root_path, 'static', 'uploads')
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'pdf'}
REFERRAL_COMMISSION = 5  # percent

# ─────────────────────────────────────────────
# Supabase / PostgreSQL Connection
# ─────────────────────────────────────────────
DATABASE_URL = os.environ.get('DATABASE_URL', '')

def get_db():
    """Return a per-request psycopg2 connection."""
    if 'db' not in g:
        if not DATABASE_URL:
            raise RuntimeError(
                "DATABASE_URL environment variable is not set. "
                "Add it to your .env file or Render environment variables."
            )
        g.db = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    return g.db

@app.teardown_appcontext
def close_db(error):
    db = g.pop('db', None)
    if db is not None:
        db.close()

def query_db(query, args=(), one=False, commit=False):
    """Execute a query. Returns last-inserted id if commit=True."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute(query, args)
    if commit:
        conn.commit()
        # For INSERT ... RETURNING id — or fallback to lastrowid
        try:
            row = cur.fetchone()
            return row['id'] if row else None
        except Exception:
            return None
    rv = cur.fetchall()
    return (rv[0] if rv else None) if one else rv

# ─────────────────────────────────────────────
# Database Schema Initializer
# ─────────────────────────────────────────────
def init_db():
    """Create tables and seed default data in Supabase."""
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            full_name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            phone TEXT,
            password_hash TEXT NOT NULL,
            referral_code TEXT UNIQUE NOT NULL,
            referred_by INTEGER REFERENCES users(id),
            balance NUMERIC DEFAULT 0,
            total_invested NUMERIC DEFAULT 0,
            total_earnings NUMERIC DEFAULT 0,
            referral_earnings NUMERIC DEFAULT 0,
            is_admin BOOLEAN DEFAULT FALSE,
            is_active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS plans (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            slug TEXT UNIQUE NOT NULL,
            icon TEXT DEFAULT '🌱',
            description TEXT,
            min_amount NUMERIC NOT NULL DEFAULT 10000,
            max_amount NUMERIC,
            roi_percent NUMERIC NOT NULL DEFAULT 10,
            duration_days INTEGER NOT NULL DEFAULT 30,
            features TEXT DEFAULT '',
            is_active BOOLEAN DEFAULT TRUE,
            sort_order INTEGER DEFAULT 0,
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS investments (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            plan_id INTEGER REFERENCES plans(id),
            plan_name TEXT NOT NULL,
            amount NUMERIC NOT NULL,
            roi_percent NUMERIC NOT NULL,
            expected_return NUMERIC NOT NULL,
            duration_days INTEGER NOT NULL,
            start_date TIMESTAMPTZ DEFAULT NOW(),
            end_date TIMESTAMPTZ NOT NULL,
            status TEXT DEFAULT 'active',
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS deposits (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            amount NUMERIC NOT NULL,
            payment_method TEXT NOT NULL,
            proof_filename TEXT,
            reference TEXT UNIQUE NOT NULL,
            status TEXT DEFAULT 'pending',
            admin_note TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        );
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS withdrawals (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            amount NUMERIC NOT NULL,
            bank_name TEXT NOT NULL,
            account_number TEXT NOT NULL,
            account_name TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            admin_note TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        );
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS referrals (
            id SERIAL PRIMARY KEY,
            referrer_id INTEGER NOT NULL REFERENCES users(id),
            referred_id INTEGER NOT NULL REFERENCES users(id),
            commission NUMERIC DEFAULT 0,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS notifications (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            message TEXT NOT NULL,
            type TEXT DEFAULT 'info',
            is_read BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
    """)

    conn.commit()

    # ── Seed admin user ──
    cur.execute("SELECT id FROM users WHERE email='admin@agrovest.ng'")
    if not cur.fetchone():
        cur.execute("""
            INSERT INTO users (full_name, email, phone, password_hash, referral_code, is_admin)
            VALUES (%s,%s,%s,%s,%s,%s)
        """, ('AgroVest Admin', 'admin@agrovest.ng', '08000000000',
              generate_password_hash('Admin@2024!'), 'ADMIN001', True))
        conn.commit()

    # ── Seed default plans if none exist ──
    cur.execute("SELECT COUNT(*) as c FROM plans")
    row = cur.fetchone()
    if not row or row['c'] == 0:
        default_plans = [
            ('Starter Farm',  'starter',      '🌱', 'Perfect entry point for new agricultural investors.',
             10000, 49999,  15, 30, 'Daily ROI updates|Email notifications|Basic support', 1),
            ('Green Harvest', 'green-harvest','🌿', 'Mid-range plan with diversified crop investments.',
             50000, 199999, 25, 45, 'Daily ROI updates|Priority support|Monthly reports|Referral bonus', 2),
            ('Premium Agro',  'premium-agro', '🌾', 'Premium returns from large-scale farming operations.',
             200000,999999, 40, 60, 'Daily ROI updates|24/7 VIP support|Weekly reports|Higher referral bonus|Early withdrawal option', 3),
            ('Elite Farm',    'elite',        '👑', 'Elite tier for serious investors seeking maximum returns.',
             1000000, None, 60, 90, 'Daily ROI updates|Dedicated account manager|Daily reports|Maximum referral bonus|Flexible withdrawal|Farm visit opportunity', 4),
        ]
        for p in default_plans:
            cur.execute("""
                INSERT INTO plans (name,slug,icon,description,min_amount,max_amount,roi_percent,duration_days,features,sort_order)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, p)
        conn.commit()

# ─────────────────────────────────────────────
# Plan Helper — always fetch live from DB
# ─────────────────────────────────────────────
def get_plans(active_only=True):
    q = "SELECT * FROM plans"
    q += " WHERE is_active=TRUE" if active_only else ""
    q += " ORDER BY sort_order ASC, id ASC"
    rows = query_db(q)
    plans = []
    for r in (rows or []):
        p = dict(r)
        p['features'] = [f.strip() for f in (p.get('features') or '').split('|') if f.strip()]
        p['min_amount'] = float(p['min_amount'] or 0)
        p['max_amount'] = float(p['max_amount']) if p.get('max_amount') else None
        p['roi_percent'] = float(p['roi_percent'] or 0)
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
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        user = query_db("SELECT * FROM users WHERE id=%s", [session['user_id']], one=True)
        if not user or not user['is_admin']:
            abort(403)
        return f(*args, **kwargs)
    return decorated

def get_current_user():
    if 'user_id' in session:
        return query_db("SELECT * FROM users WHERE id=%s", [session['user_id']], one=True)
    return None

@app.context_processor
def inject_globals():
    user, unread, plans = None, 0, []
    try:
        user = get_current_user()
        if user:
            row = query_db(
                "SELECT COUNT(*) as c FROM notifications WHERE user_id=%s AND is_read=FALSE",
                [user['id']], one=True)
            unread = row['c'] if row else 0
        plans = get_plans()
    except Exception:
        pass
    return dict(current_user=user, unread_count=unread, plans=plans)

# ─────────────────────────────────────────────
# Serve Uploaded Files
# ─────────────────────────────────────────────
@app.route('/uploads/<path:filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

# ─────────────────────────────────────────────
# Public Pages
# ─────────────────────────────────────────────
@app.route('/')
def index():
    try:
        stats = {
            'total_users':        query_db("SELECT COUNT(*) as c FROM users WHERE is_admin=FALSE", one=True)['c'],
            'total_invested':     query_db("SELECT COALESCE(SUM(amount),0) as s FROM investments", one=True)['s'],
            'active_investments': query_db("SELECT COUNT(*) as c FROM investments WHERE status='active'", one=True)['c'],
            'total_paid':         query_db("SELECT COALESCE(SUM(amount),0) as s FROM withdrawals WHERE status='approved'", one=True)['s'],
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
        if not email or '@' not in email:
            errors.append('Enter a valid email address.')
        if len(password) < 8:
            errors.append('Password must be at least 8 characters.')
        if password != confirm:
            errors.append('Passwords do not match.')
        if query_db("SELECT id FROM users WHERE email=%s", [email], one=True):
            errors.append('Email already registered.')

        if errors:
            for e in errors:
                flash(e, 'error')
            return render_template('register.html', ref_code=ref_input)

        new_ref = full_name.upper().replace(' ', '')[:4] + str(uuid.uuid4())[:6].upper()
        referred_by = None
        if ref_input:
            ref_row = query_db("SELECT id FROM users WHERE referral_code=%s", [ref_input], one=True)
            if ref_row:
                referred_by = ref_row['id']

        new_id = query_db("""
            INSERT INTO users (full_name,email,phone,password_hash,referral_code,referred_by)
            VALUES (%s,%s,%s,%s,%s,%s) RETURNING id
        """, [full_name, email, phone, generate_password_hash(password), new_ref, referred_by],
        commit=True)

        if referred_by and new_id:
            query_db("INSERT INTO referrals (referrer_id,referred_id) VALUES (%s,%s) RETURNING id",
                     [referred_by, new_id], commit=True)

        if new_id:
            query_db("INSERT INTO notifications (user_id,message,type) VALUES (%s,%s,%s) RETURNING id",
                     [new_id, f'Welcome to AgroVest, {full_name}! Your account is ready.', 'success'],
                     commit=True)

        flash('Account created! Please log in.', 'success')
        return redirect(url_for('login'))

    return render_template('register.html', ref_code=ref_code)


@app.route('/login', methods=['GET', 'POST'])
def login():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        email    = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        user     = query_db("SELECT * FROM users WHERE email=%s", [email], one=True)

        if user and check_password_hash(user['password_hash'], password):
            if not user['is_active']:
                flash('Your account has been suspended. Contact support.', 'error')
                return render_template('login.html')
            session.permanent = True
            session['user_id']  = user['id']
            session['is_admin'] = bool(user['is_admin'])
            flash(f'Welcome back, {user["full_name"].split()[0]}!', 'success')
            return redirect(url_for('admin_dashboard') if user['is_admin'] else url_for('dashboard'))
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
    active_investments = query_db(
        "SELECT * FROM investments WHERE user_id=%s AND status='active' ORDER BY created_at DESC",
        [user['id']])
    recent_deposits = query_db(
        "SELECT * FROM deposits WHERE user_id=%s ORDER BY created_at DESC LIMIT 5", [user['id']])
    recent_withdrawals = query_db(
        "SELECT * FROM withdrawals WHERE user_id=%s ORDER BY created_at DESC LIMIT 5", [user['id']])
    referrals = query_db("""
        SELECT u.full_name, u.created_at, r.commission, r.status
        FROM referrals r JOIN users u ON u.id=r.referred_id
        WHERE r.referrer_id=%s ORDER BY r.created_at DESC
    """, [user['id']])
    notifications = query_db(
        "SELECT * FROM notifications WHERE user_id=%s ORDER BY created_at DESC LIMIT 10", [user['id']])

    return render_template('dashboard.html',
        user=user,
        active_investments=active_investments,
        recent_deposits=recent_deposits,
        recent_withdrawals=recent_withdrawals,
        referrals=referrals,
        notifications=notifications)


@app.route('/dashboard/invest', methods=['GET', 'POST'])
@login_required
def invest():
    user  = get_current_user()
    all_plans = get_plans()

    if request.method == 'POST':
        plan_id = request.form.get('plan_id', 0, type=int)
        amount  = request.form.get('amount',  0, type=float)

        plan = query_db("SELECT * FROM plans WHERE id=%s AND is_active=TRUE", [plan_id], one=True)
        if not plan:
            flash('Invalid plan selected.', 'error')
            return redirect(url_for('invest'))

        min_a = float(plan['min_amount'])
        max_a = float(plan['max_amount']) if plan['max_amount'] else None

        if amount < min_a:
            flash(f'Minimum investment for {plan["name"]} is ₦{min_a:,.0f}', 'error')
            return redirect(url_for('invest'))
        if max_a and amount > max_a:
            flash(f'Maximum investment for {plan["name"]} is ₦{max_a:,.0f}', 'error')
            return redirect(url_for('invest'))
        if float(user['balance']) < amount:
            flash('Insufficient balance. Please deposit funds first.', 'error')
            return redirect(url_for('deposit'))

        roi_pct         = float(plan['roi_percent'])
        expected_return = amount + (amount * roi_pct / 100)
        end_date        = datetime.utcnow() + timedelta(days=int(plan['duration_days']))

        query_db("UPDATE users SET balance=balance-%s, total_invested=total_invested+%s WHERE id=%s RETURNING id",
                 [amount, amount, user['id']], commit=True)

        query_db("""
            INSERT INTO investments (user_id,plan_id,plan_name,amount,roi_percent,expected_return,duration_days,end_date)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
        """, [user['id'], plan_id, plan['name'], amount, roi_pct, expected_return,
              int(plan['duration_days']), end_date], commit=True)

        # Referral commission
        if user['referred_by']:
            commission = amount * REFERRAL_COMMISSION / 100
            query_db("UPDATE users SET balance=balance+%s, referral_earnings=referral_earnings+%s WHERE id=%s RETURNING id",
                     [commission, commission, user['referred_by']], commit=True)
            query_db("UPDATE referrals SET commission=commission+%s, status='active' WHERE referrer_id=%s AND referred_id=%s RETURNING id",
                     [commission, user['referred_by'], user['id']], commit=True)
            query_db("INSERT INTO notifications (user_id,message,type) VALUES (%s,%s,%s) RETURNING id",
                     [user['referred_by'], f'You earned ₦{commission:,.2f} referral commission!', 'success'],
                     commit=True)

        query_db("INSERT INTO notifications (user_id,message,type) VALUES (%s,%s,%s) RETURNING id",
                 [user['id'], f'Investment of ₦{amount:,.2f} in {plan["name"]} activated!', 'success'],
                 commit=True)

        flash(f'Investment activated! Expected return: ₦{expected_return:,.2f}', 'success')
        return redirect(url_for('dashboard'))

    return render_template('invest.html', user=user, plans=all_plans)


@app.route('/dashboard/deposit', methods=['GET', 'POST'])
@login_required
def deposit():
    user = get_current_user()

    if request.method == 'POST':
        amount         = request.form.get('amount', 0, type=float)
        payment_method = request.form.get('payment_method', '')
        proof          = request.files.get('proof')

        if amount < 1000:
            flash('Minimum deposit is ₦1,000.', 'error')
            return redirect(url_for('deposit'))

        proof_filename = None
        if proof and proof.filename:
            ext = proof.filename.rsplit('.', 1)[-1].lower()
            if ext not in ALLOWED_EXTENSIONS:
                flash('Only PNG, JPG, JPEG, PDF files allowed.', 'error')
                return redirect(url_for('deposit'))
            proof_filename = f"{uuid.uuid4().hex}.{ext}"
            proof.save(os.path.join(app.config['UPLOAD_FOLDER'], proof_filename))

        reference = 'AGV' + uuid.uuid4().hex[:10].upper()
        query_db("""
            INSERT INTO deposits (user_id,amount,payment_method,proof_filename,reference)
            VALUES (%s,%s,%s,%s,%s) RETURNING id
        """, [user['id'], amount, payment_method, proof_filename, reference], commit=True)

        query_db("INSERT INTO notifications (user_id,message,type) VALUES (%s,%s,%s) RETURNING id",
                 [user['id'], f'Deposit request of ₦{amount:,.2f} submitted. Awaiting confirmation.', 'info'],
                 commit=True)

        flash('Deposit submitted! Confirmed within 30 minutes.', 'success')
        return redirect(url_for('dashboard'))

    deposits = query_db("SELECT * FROM deposits WHERE user_id=%s ORDER BY created_at DESC", [user['id']])
    return render_template('deposit.html', user=user, deposits=deposits)


@app.route('/dashboard/withdraw', methods=['GET', 'POST'])
@login_required
def withdraw():
    user = get_current_user()

    if request.method == 'POST':
        amount         = request.form.get('amount', 0, type=float)
        bank_name      = request.form.get('bank_name', '').strip()
        account_number = request.form.get('account_number', '').strip()
        account_name   = request.form.get('account_name', '').strip()

        if amount < 2000:
            flash('Minimum withdrawal is ₦2,000.', 'error')
            return redirect(url_for('withdraw'))
        if float(user['balance']) < amount:
            flash('Insufficient balance.', 'error')
            return redirect(url_for('withdraw'))

        query_db("UPDATE users SET balance=balance-%s WHERE id=%s RETURNING id",
                 [amount, user['id']], commit=True)
        query_db("""
            INSERT INTO withdrawals (user_id,amount,bank_name,account_number,account_name)
            VALUES (%s,%s,%s,%s,%s) RETURNING id
        """, [user['id'], amount, bank_name, account_number, account_name], commit=True)

        query_db("INSERT INTO notifications (user_id,message,type) VALUES (%s,%s,%s) RETURNING id",
                 [user['id'], f'Withdrawal request of ₦{amount:,.2f} submitted.', 'info'],
                 commit=True)

        flash('Withdrawal submitted! Processing within 24 hours.', 'success')
        return redirect(url_for('dashboard'))

    withdrawals = query_db("SELECT * FROM withdrawals WHERE user_id=%s ORDER BY created_at DESC", [user['id']])
    return render_template('withdraw.html', user=user, withdrawals=withdrawals)


@app.route('/dashboard/notifications/read', methods=['POST'])
@login_required
def mark_notifications_read():
    user = get_current_user()
    query_db("UPDATE notifications SET is_read=TRUE WHERE user_id=%s RETURNING id", [user['id']], commit=True)
    return jsonify({'status': 'ok'})


# ═════════════════════════════════════════════
# ADMIN — Dashboard
# ═════════════════════════════════════════════
@app.route('/admin')
@admin_required
def admin_dashboard():
    stats = {
        'total_users':        query_db("SELECT COUNT(*) as c FROM users WHERE is_admin=FALSE", one=True)['c'],
        'total_invested':     query_db("SELECT COALESCE(SUM(amount),0) as s FROM investments", one=True)['s'],
        'pending_deposits':   query_db("SELECT COUNT(*) as c FROM deposits WHERE status='pending'", one=True)['c'],
        'pending_withdrawals':query_db("SELECT COUNT(*) as c FROM withdrawals WHERE status='pending'", one=True)['c'],
        'total_paid_out':     query_db("SELECT COALESCE(SUM(amount),0) as s FROM withdrawals WHERE status='approved'", one=True)['s'],
        'active_investments': query_db("SELECT COUNT(*) as c FROM investments WHERE status='active'", one=True)['c'],
        'total_plans':        query_db("SELECT COUNT(*) as c FROM plans", one=True)['c'],
    }
    recent_users  = query_db("SELECT * FROM users WHERE is_admin=FALSE ORDER BY created_at DESC LIMIT 10")
    pending_deps  = query_db("""
        SELECT d.*,u.full_name,u.email FROM deposits d
        JOIN users u ON u.id=d.user_id WHERE d.status='pending' ORDER BY d.created_at DESC
    """)
    pending_wds   = query_db("""
        SELECT w.*,u.full_name,u.email FROM withdrawals w
        JOIN users u ON u.id=w.user_id WHERE w.status='pending' ORDER BY w.created_at DESC
    """)
    return render_template('admin/dashboard.html',
        stats=stats, recent_users=recent_users,
        pending_deps=pending_deps, pending_wds=pending_wds)


# ═════════════════════════════════════════════
# ADMIN — Plans CRUD
# ═════════════════════════════════════════════
@app.route('/admin/plans')
@admin_required
def admin_plans():
    all_plans = query_db("SELECT * FROM plans ORDER BY sort_order ASC, id ASC")
    return render_template('admin/plans.html', plans=all_plans)


@app.route('/admin/plans/add', methods=['GET', 'POST'])
@admin_required
def admin_plan_add():
    if request.method == 'POST':
        name         = request.form.get('name', '').strip()
        slug         = request.form.get('slug', '').strip().lower().replace(' ', '-')
        icon         = request.form.get('icon', '🌱').strip()
        description  = request.form.get('description', '').strip()
        min_amount   = request.form.get('min_amount', 0, type=float)
        max_amount   = request.form.get('max_amount', None)
        roi_percent  = request.form.get('roi_percent', 0, type=float)
        duration_days= request.form.get('duration_days', 30, type=int)
        features_raw = request.form.get('features', '').strip()
        sort_order   = request.form.get('sort_order', 0, type=int)
        is_active    = request.form.get('is_active') == 'on'

        # Clean features — one per line → pipe-separated
        features = '|'.join([f.strip() for f in features_raw.splitlines() if f.strip()])
        max_amt  = float(max_amount) if max_amount and str(max_amount).strip() else None

        # Check slug unique
        if query_db("SELECT id FROM plans WHERE slug=%s", [slug], one=True):
            flash('A plan with that slug already exists. Choose a different name/slug.', 'error')
            return render_template('admin/plan_form.html', plan=None, action='add')

        if not name or not slug or roi_percent <= 0 or min_amount <= 0:
            flash('Name, slug, ROI % and min amount are required.', 'error')
            return render_template('admin/plan_form.html', plan=None, action='add')

        query_db("""
            INSERT INTO plans (name,slug,icon,description,min_amount,max_amount,roi_percent,duration_days,features,sort_order,is_active)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
        """, [name, slug, icon, description, min_amount, max_amt, roi_percent,
              duration_days, features, sort_order, is_active], commit=True)

        flash(f'Plan "{name}" created successfully!', 'success')
        return redirect(url_for('admin_plans'))

    return render_template('admin/plan_form.html', plan=None, action='add')


@app.route('/admin/plans/<int:plan_id>/edit', methods=['GET', 'POST'])
@admin_required
def admin_plan_edit(plan_id):
    plan = query_db("SELECT * FROM plans WHERE id=%s", [plan_id], one=True)
    if not plan:
        flash('Plan not found.', 'error')
        return redirect(url_for('admin_plans'))

    if request.method == 'POST':
        name         = request.form.get('name', '').strip()
        slug         = request.form.get('slug', '').strip().lower().replace(' ', '-')
        icon         = request.form.get('icon', '🌱').strip()
        description  = request.form.get('description', '').strip()
        min_amount   = request.form.get('min_amount', 0, type=float)
        max_amount   = request.form.get('max_amount', None)
        roi_percent  = request.form.get('roi_percent', 0, type=float)
        duration_days= request.form.get('duration_days', 30, type=int)
        features_raw = request.form.get('features', '').strip()
        sort_order   = request.form.get('sort_order', 0, type=int)
        is_active    = request.form.get('is_active') == 'on'

        features = '|'.join([f.strip() for f in features_raw.splitlines() if f.strip()])
        max_amt  = float(max_amount) if max_amount and str(max_amount).strip() else None

        # Check slug unique (exclude self)
        existing = query_db("SELECT id FROM plans WHERE slug=%s AND id!=%s", [slug, plan_id], one=True)
        if existing:
            flash('That slug is already used by another plan.', 'error')
            return render_template('admin/plan_form.html', plan=dict(plan), action='edit')

        query_db("""
            UPDATE plans SET name=%s,slug=%s,icon=%s,description=%s,min_amount=%s,
            max_amount=%s,roi_percent=%s,duration_days=%s,features=%s,sort_order=%s,is_active=%s
            WHERE id=%s RETURNING id
        """, [name, slug, icon, description, min_amount, max_amt, roi_percent,
              duration_days, features, sort_order, is_active, plan_id], commit=True)

        flash(f'Plan "{name}" updated successfully!', 'success')
        return redirect(url_for('admin_plans'))

    plan_dict = dict(plan)
    # Convert pipe features back to newlines for the textarea
    plan_dict['features_text'] = '\n'.join((plan_dict.get('features') or '').split('|'))
    return render_template('admin/plan_form.html', plan=plan_dict, action='edit')


@app.route('/admin/plans/<int:plan_id>/toggle', methods=['POST'])
@admin_required
def admin_plan_toggle(plan_id):
    plan = query_db("SELECT * FROM plans WHERE id=%s", [plan_id], one=True)
    if plan:
        new_status = not bool(plan['is_active'])
        query_db("UPDATE plans SET is_active=%s WHERE id=%s RETURNING id",
                 [new_status, plan_id], commit=True)
        flash(f'Plan {"activated" if new_status else "deactivated"}.', 'success')
    return redirect(url_for('admin_plans'))


@app.route('/admin/plans/<int:plan_id>/delete', methods=['POST'])
@admin_required
def admin_plan_delete(plan_id):
    # Check if any active investments reference this plan
    active = query_db(
        "SELECT COUNT(*) as c FROM investments WHERE plan_id=%s AND status='active'", [plan_id], one=True)
    if active and active['c'] > 0:
        flash(f'Cannot delete — {active["c"]} active investment(s) use this plan. Deactivate it instead.', 'error')
        return redirect(url_for('admin_plans'))

    plan = query_db("SELECT name FROM plans WHERE id=%s", [plan_id], one=True)
    query_db("DELETE FROM plans WHERE id=%s RETURNING id", [plan_id], commit=True)
    flash(f'Plan "{plan["name"] if plan else plan_id}" deleted.', 'success')
    return redirect(url_for('admin_plans'))


# ═════════════════════════════════════════════
# ADMIN — Users CRUD
# ═════════════════════════════════════════════
@app.route('/admin/users')
@admin_required
def admin_users():
    users = query_db("""
        SELECT u.*,
          (SELECT COUNT(*) FROM investments  WHERE user_id=u.id) as inv_count,
          (SELECT COUNT(*) FROM referrals    WHERE referrer_id=u.id) as ref_count
        FROM users u WHERE u.is_admin=FALSE ORDER BY u.created_at DESC
    """)
    return render_template('admin/users.html', users=users)


@app.route('/admin/users/<int:user_id>/edit', methods=['GET', 'POST'])
@admin_required
def admin_user_edit(user_id):
    user = query_db("SELECT * FROM users WHERE id=%s", [user_id], one=True)
    if not user:
        flash('User not found.', 'error')
        return redirect(url_for('admin_users'))

    if request.method == 'POST':
        full_name  = request.form.get('full_name', '').strip()
        email      = request.form.get('email', '').strip().lower()
        phone      = request.form.get('phone', '').strip()
        balance    = request.form.get('balance', 0, type=float)
        is_active  = request.form.get('is_active') == 'on'
        is_admin   = request.form.get('is_admin') == 'on'
        new_password = request.form.get('new_password', '').strip()

        # Check email unique (exclude self)
        if query_db("SELECT id FROM users WHERE email=%s AND id!=%s", [email, user_id], one=True):
            flash('Email already used by another account.', 'error')
            return render_template('admin/user_form.html', user=dict(user))

        if new_password:
            if len(new_password) < 8:
                flash('New password must be at least 8 characters.', 'error')
                return render_template('admin/user_form.html', user=dict(user))
            query_db("UPDATE users SET password_hash=%s WHERE id=%s RETURNING id",
                     [generate_password_hash(new_password), user_id], commit=True)

        query_db("""
            UPDATE users SET full_name=%s,email=%s,phone=%s,balance=%s,is_active=%s,is_admin=%s
            WHERE id=%s RETURNING id
        """, [full_name, email, phone, balance, is_active, is_admin, user_id], commit=True)

        query_db("INSERT INTO notifications (user_id,message,type) VALUES (%s,%s,%s) RETURNING id",
                 [user_id, 'Your account details have been updated by admin.', 'info'], commit=True)

        flash(f'User "{full_name}" updated.', 'success')
        return redirect(url_for('admin_users'))

    return render_template('admin/user_form.html', user=dict(user))


@app.route('/admin/users/<int:user_id>/toggle', methods=['POST'])
@admin_required
def admin_toggle_user(user_id):
    user = query_db("SELECT * FROM users WHERE id=%s", [user_id], one=True)
    if user:
        new_status = not bool(user['is_active'])
        query_db("UPDATE users SET is_active=%s WHERE id=%s RETURNING id",
                 [new_status, user_id], commit=True)
        flash(f'User {"activated" if new_status else "suspended"}.', 'success')
    return redirect(url_for('admin_users'))


@app.route('/admin/users/<int:user_id>/credit', methods=['POST'])
@admin_required
def admin_credit_user(user_id):
    amount = request.form.get('amount', 0, type=float)
    note   = request.form.get('note', 'Admin credit').strip()
    if amount > 0:
        query_db("UPDATE users SET balance=balance+%s WHERE id=%s RETURNING id",
                 [amount, user_id], commit=True)
        query_db("INSERT INTO notifications (user_id,message,type) VALUES (%s,%s,%s) RETURNING id",
                 [user_id, f'Your account has been credited ₦{amount:,.2f}. {note}', 'success'],
                 commit=True)
        flash(f'₦{amount:,.2f} credited successfully.', 'success')
    return redirect(url_for('admin_users'))


@app.route('/admin/users/<int:user_id>/delete', methods=['POST'])
@admin_required
def admin_user_delete(user_id):
    user = query_db("SELECT * FROM users WHERE id=%s", [user_id], one=True)
    if not user:
        flash('User not found.', 'error')
        return redirect(url_for('admin_users'))
    if user['is_admin']:
        flash('Cannot delete admin accounts.', 'error')
        return redirect(url_for('admin_users'))

    active_inv = query_db(
        "SELECT COUNT(*) as c FROM investments WHERE user_id=%s AND status='active'", [user_id], one=True)
    if active_inv and active_inv['c'] > 0:
        flash(f'Cannot delete — user has {active_inv["c"]} active investment(s). Suspend instead.', 'error')
        return redirect(url_for('admin_users'))

    # Cascade delete related records
    for tbl in ['notifications', 'deposits', 'withdrawals', 'investments', 'referrals']:
        col = 'referrer_id' if tbl == 'referrals' else 'user_id'
        try:
            query_db(f"DELETE FROM {tbl} WHERE {col}=%s RETURNING id", [user_id], commit=True)
            if tbl == 'referrals':
                query_db("DELETE FROM referrals WHERE referred_id=%s RETURNING id", [user_id], commit=True)
        except Exception:
            pass

    query_db("DELETE FROM users WHERE id=%s RETURNING id", [user_id], commit=True)
    flash(f'User "{user["full_name"]}" deleted permanently.', 'success')
    return redirect(url_for('admin_users'))


# ═════════════════════════════════════════════
# ADMIN — Deposits
# ═════════════════════════════════════════════
@app.route('/admin/deposits')
@admin_required
def admin_deposits():
    deposits = query_db("""
        SELECT d.*,u.full_name,u.email FROM deposits d
        JOIN users u ON u.id=d.user_id ORDER BY d.created_at DESC
    """)
    return render_template('admin/deposits.html', deposits=deposits)


@app.route('/admin/deposits/<int:dep_id>/approve', methods=['POST'])
@admin_required
def admin_approve_deposit(dep_id):
    dep = query_db("SELECT * FROM deposits WHERE id=%s", [dep_id], one=True)
    if dep and dep['status'] == 'pending':
        query_db("UPDATE deposits SET status='approved', updated_at=NOW() WHERE id=%s RETURNING id",
                 [dep_id], commit=True)
        query_db("UPDATE users SET balance=balance+%s WHERE id=%s RETURNING id",
                 [dep['amount'], dep['user_id']], commit=True)
        query_db("INSERT INTO notifications (user_id,message,type) VALUES (%s,%s,%s) RETURNING id",
                 [dep['user_id'], f'Your deposit of ₦{float(dep["amount"]):,.2f} has been approved!', 'success'],
                 commit=True)
        flash('Deposit approved.', 'success')
    return redirect(url_for('admin_deposits'))


@app.route('/admin/deposits/<int:dep_id>/reject', methods=['POST'])
@admin_required
def admin_reject_deposit(dep_id):
    note = request.form.get('note', 'Rejected by admin')
    dep  = query_db("SELECT * FROM deposits WHERE id=%s", [dep_id], one=True)
    if dep and dep['status'] == 'pending':
        query_db("UPDATE deposits SET status='rejected',admin_note=%s,updated_at=NOW() WHERE id=%s RETURNING id",
                 [note, dep_id], commit=True)
        query_db("INSERT INTO notifications (user_id,message,type) VALUES (%s,%s,%s) RETURNING id",
                 [dep['user_id'], f'Deposit of ₦{float(dep["amount"]):,.2f} rejected. Reason: {note}', 'error'],
                 commit=True)
        flash('Deposit rejected.', 'warning')
    return redirect(url_for('admin_deposits'))


# ═════════════════════════════════════════════
# ADMIN — Withdrawals
# ═════════════════════════════════════════════
@app.route('/admin/withdrawals')
@admin_required
def admin_withdrawals():
    withdrawals = query_db("""
        SELECT w.*,u.full_name,u.email FROM withdrawals w
        JOIN users u ON u.id=w.user_id ORDER BY w.created_at DESC
    """)
    return render_template('admin/withdrawals.html', withdrawals=withdrawals)


@app.route('/admin/withdrawals/<int:wd_id>/approve', methods=['POST'])
@admin_required
def admin_approve_withdrawal(wd_id):
    wd = query_db("SELECT * FROM withdrawals WHERE id=%s", [wd_id], one=True)
    if wd and wd['status'] == 'pending':
        query_db("UPDATE withdrawals SET status='approved',updated_at=NOW() WHERE id=%s RETURNING id",
                 [wd_id], commit=True)
        query_db("UPDATE users SET total_earnings=total_earnings+%s WHERE id=%s RETURNING id",
                 [wd['amount'], wd['user_id']], commit=True)
        query_db("INSERT INTO notifications (user_id,message,type) VALUES (%s,%s,%s) RETURNING id",
                 [wd['user_id'], f'Withdrawal of ₦{float(wd["amount"]):,.2f} approved and sent!', 'success'],
                 commit=True)
        flash('Withdrawal approved.', 'success')
    return redirect(url_for('admin_withdrawals'))


@app.route('/admin/withdrawals/<int:wd_id>/reject', methods=['POST'])
@admin_required
def admin_reject_withdrawal(wd_id):
    note = request.form.get('note', 'Rejected by admin')
    wd   = query_db("SELECT * FROM withdrawals WHERE id=%s", [wd_id], one=True)
    if wd and wd['status'] == 'pending':
        query_db("UPDATE users SET balance=balance+%s WHERE id=%s RETURNING id",
                 [wd['amount'], wd['user_id']], commit=True)
        query_db("UPDATE withdrawals SET status='rejected',admin_note=%s,updated_at=NOW() WHERE id=%s RETURNING id",
                 [note, wd_id], commit=True)
        query_db("INSERT INTO notifications (user_id,message,type) VALUES (%s,%s,%s) RETURNING id",
                 [wd['user_id'], f'Withdrawal of ₦{float(wd["amount"]):,.2f} rejected. Refunded. Reason: {note}', 'warning'],
                 commit=True)
        flash('Withdrawal rejected and balance refunded.', 'warning')
    return redirect(url_for('admin_withdrawals'))


# ═════════════════════════════════════════════
# ADMIN — Investments
# ═════════════════════════════════════════════
@app.route('/admin/investments')
@admin_required
def admin_investments():
    investments = query_db("""
        SELECT i.*,u.full_name,u.email FROM investments i
        JOIN users u ON u.id=i.user_id ORDER BY i.created_at DESC
    """)
    return render_template('admin/investments.html', investments=investments)


@app.route('/admin/investments/<int:inv_id>/complete', methods=['POST'])
@admin_required
def admin_complete_investment(inv_id):
    inv = query_db("SELECT * FROM investments WHERE id=%s", [inv_id], one=True)
    if inv and inv['status'] == 'active':
        query_db("UPDATE investments SET status='completed' WHERE id=%s RETURNING id",
                 [inv_id], commit=True)
        profit = float(inv['expected_return']) - float(inv['amount'])
        query_db("UPDATE users SET balance=balance+%s, total_earnings=total_earnings+%s WHERE id=%s RETURNING id",
                 [inv['expected_return'], profit, inv['user_id']], commit=True)
        query_db("INSERT INTO notifications (user_id,message,type) VALUES (%s,%s,%s) RETURNING id",
                 [inv['user_id'],
                  f'{inv["plan_name"]} matured! ₦{float(inv["expected_return"]):,.2f} credited to your balance.',
                  'success'], commit=True)
        flash('Investment completed and balance credited.', 'success')
    return redirect(url_for('admin_investments'))


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
            value = datetime.fromisoformat(value)
        except Exception:
            return value
    try:
        return value.strftime(fmt)
    except Exception:
        return str(value)

@app.template_filter('status_badge')
def status_badge(status):
    badges = {
        'pending':   'badge-warning',
        'approved':  'badge-success',
        'rejected':  'badge-error',
        'active':    'badge-info',
        'completed': 'badge-success',
    }
    return badges.get(str(status or '').lower(), 'badge-neutral')


# ═════════════════════════════════════════════
# Initialize DB on startup (gunicorn safe)
# ═════════════════════════════════════════════
with app.app_context():
    try:
        init_db()
        print("✓ Database initialized successfully")
    except Exception as _e:
        print(f"⚠ DB init skipped (no DATABASE_URL yet?): {_e}")

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=5000)
