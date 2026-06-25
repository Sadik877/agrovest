"""
AgroVest Pro - Agricultural Investment Platform
Main Flask Application
"""

import os
import sqlite3
import uuid
from datetime import datetime, timedelta
from functools import wraps

from flask import (Flask, render_template, request, redirect, url_for,
                   flash, session, g, jsonify, abort)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

# ─────────────────────────────────────────────
# App Configuration
# ─────────────────────────────────────────────
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'agrovest-super-secret-key-2024-change-in-prod')
app.config['DATABASE'] = os.path.join(app.root_path, 'database.db')
app.config['UPLOAD_FOLDER'] = os.path.join(app.root_path, 'static', 'uploads')
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024  # 5MB
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=2)

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'pdf'}

# ─────────────────────────────────────────────
# Investment Plans (static config)
# ─────────────────────────────────────────────
INVESTMENT_PLANS = [
    {
        'id': 1,
        'name': 'Starter Farm',
        'slug': 'starter',
        'min_amount': 10000,
        'max_amount': 49999,
        'roi_percent': 15,
        'duration_days': 30,
        'icon': '🌱',
        'color': 'emerald',
        'description': 'Perfect entry point for new agricultural investors.',
        'features': ['Daily ROI updates', 'Email notifications', 'Basic support']
    },
    {
        'id': 2,
        'name': 'Green Harvest',
        'slug': 'green-harvest',
        'min_amount': 50000,
        'max_amount': 199999,
        'roi_percent': 25,
        'duration_days': 45,
        'icon': '🌿',
        'color': 'teal',
        'description': 'Mid-range plan with diversified crop investments.',
        'features': ['Daily ROI updates', 'Priority support', 'Monthly reports', 'Referral bonus']
    },
    {
        'id': 3,
        'name': 'Premium Agro',
        'slug': 'premium-agro',
        'min_amount': 200000,
        'max_amount': 999999,
        'roi_percent': 40,
        'duration_days': 60,
        'icon': '🌾',
        'color': 'gold',
        'description': 'Premium returns from large-scale farming operations.',
        'features': ['Daily ROI updates', '24/7 VIP support', 'Weekly reports', 'Higher referral bonus', 'Early withdrawal option']
    },
    {
        'id': 4,
        'name': 'Elite Farm',
        'slug': 'elite',
        'min_amount': 1000000,
        'max_amount': None,
        'roi_percent': 60,
        'duration_days': 90,
        'icon': '👑',
        'color': 'purple',
        'description': 'Elite tier for serious investors seeking maximum returns.',
        'features': ['Daily ROI updates', 'Dedicated account manager', 'Daily reports', 'Maximum referral bonus', 'Flexible withdrawal', 'Farm visit opportunity']
    }
]

REFERRAL_COMMISSION = 5  # percent

# ─────────────────────────────────────────────
# Database Helpers
# ─────────────────────────────────────────────
def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(
            app.config['DATABASE'],
            detect_types=sqlite3.PARSE_DECLTYPES
        )
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(error):
    db = g.pop('db', None)
    if db is not None:
        db.close()

def query_db(query, args=(), one=False, commit=False):
    db = get_db()
    cur = db.execute(query, args)
    if commit:
        db.commit()
        return cur.lastrowid
    rv = cur.fetchall()
    return (rv[0] if rv else None) if one else rv

def init_db():
    """Initialize database schema and seed admin."""
    db = get_db()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            phone TEXT,
            password_hash TEXT NOT NULL,
            referral_code TEXT UNIQUE NOT NULL,
            referred_by INTEGER REFERENCES users(id),
            balance REAL DEFAULT 0.0,
            total_invested REAL DEFAULT 0.0,
            total_earnings REAL DEFAULT 0.0,
            referral_earnings REAL DEFAULT 0.0,
            is_admin INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS investments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id),
            plan_id INTEGER NOT NULL,
            plan_name TEXT NOT NULL,
            amount REAL NOT NULL,
            roi_percent REAL NOT NULL,
            expected_return REAL NOT NULL,
            duration_days INTEGER NOT NULL,
            start_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            end_date TIMESTAMP NOT NULL,
            status TEXT DEFAULT 'active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS deposits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id),
            amount REAL NOT NULL,
            payment_method TEXT NOT NULL,
            proof_filename TEXT,
            reference TEXT UNIQUE NOT NULL,
            status TEXT DEFAULT 'pending',
            admin_note TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS withdrawals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id),
            amount REAL NOT NULL,
            bank_name TEXT NOT NULL,
            account_number TEXT NOT NULL,
            account_name TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            admin_note TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS referrals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            referrer_id INTEGER NOT NULL REFERENCES users(id),
            referred_id INTEGER NOT NULL REFERENCES users(id),
            commission REAL DEFAULT 0.0,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id),
            message TEXT NOT NULL,
            type TEXT DEFAULT 'info',
            is_read INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    db.commit()

    # Seed admin if not exists
    admin = db.execute("SELECT id FROM users WHERE email='admin@agrovest.ng'").fetchone()
    if not admin:
        db.execute("""
            INSERT INTO users (full_name, email, phone, password_hash, referral_code, is_admin)
            VALUES (?, ?, ?, ?, ?, 1)
        """, ('AgroVest Admin', 'admin@agrovest.ng', '08000000000',
              generate_password_hash('Admin@2024!'), 'ADMIN001'))
        db.commit()

# ─────────────────────────────────────────────
# Auth Decorators
# ─────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            flash('Please log in to continue.', 'warning')
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        user = query_db("SELECT * FROM users WHERE id=?", [session['user_id']], one=True)
        if not user or not user['is_admin']:
            abort(403)
        return f(*args, **kwargs)
    return decorated

def get_current_user():
    if 'user_id' in session:
        return query_db("SELECT * FROM users WHERE id=?", [session['user_id']], one=True)
    return None

@app.context_processor
def inject_user():
    user = get_current_user()
    unread = 0
    if user:
        unread = query_db("SELECT COUNT(*) as c FROM notifications WHERE user_id=? AND is_read=0",
                          [user['id']], one=True)['c']
    return dict(current_user=user, unread_count=unread, plans=INVESTMENT_PLANS)

# ─────────────────────────────────────────────
# Public Pages
# ─────────────────────────────────────────────
@app.route('/')
def index():
    stats = {
        'total_users': query_db("SELECT COUNT(*) as c FROM users WHERE is_admin=0", one=True)['c'],
        'total_invested': query_db("SELECT COALESCE(SUM(amount),0) as s FROM investments", one=True)['s'],
        'active_investments': query_db("SELECT COUNT(*) as c FROM investments WHERE status='active'", one=True)['c'],
        'total_paid': query_db("SELECT COALESCE(SUM(amount),0) as s FROM withdrawals WHERE status='approved'", one=True)['s'],
    }
    return render_template('index.html', stats=stats, plans=INVESTMENT_PLANS)

@app.route('/about')
def about():
    return render_template('about.html')

@app.route('/plans')
def plans():
    return render_template('plans.html', plans=INVESTMENT_PLANS)

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
        email = request.form.get('email', '').strip().lower()
        phone = request.form.get('phone', '').strip()
        password = request.form.get('password', '')
        confirm = request.form.get('confirm_password', '')
        referral_input = request.form.get('referral_code', '').strip().upper()

        # Validation
        errors = []
        if not full_name or len(full_name) < 3:
            errors.append('Full name must be at least 3 characters.')
        if not email or '@' not in email:
            errors.append('Enter a valid email address.')
        if len(password) < 8:
            errors.append('Password must be at least 8 characters.')
        if password != confirm:
            errors.append('Passwords do not match.')
        if query_db("SELECT id FROM users WHERE email=?", [email], one=True):
            errors.append('Email already registered.')

        if errors:
            for e in errors:
                flash(e, 'error')
            return render_template('register.html', ref_code=referral_input)

        # Generate unique referral code
        new_ref_code = full_name.upper().replace(' ', '')[:4] + str(uuid.uuid4())[:6].upper()

        # Find referrer
        referred_by = None
        if referral_input:
            referrer = query_db("SELECT id FROM users WHERE referral_code=?", [referral_input], one=True)
            if referrer:
                referred_by = referrer['id']

        new_id = query_db("""
            INSERT INTO users (full_name, email, phone, password_hash, referral_code, referred_by)
            VALUES (?, ?, ?, ?, ?, ?)
        """, [full_name, email, phone, generate_password_hash(password), new_ref_code, referred_by],
        commit=True)

        # Log referral relationship
        if referred_by:
            query_db("INSERT INTO referrals (referrer_id, referred_id) VALUES (?, ?)",
                     [referred_by, new_id], commit=True)

        # Welcome notification
        query_db("INSERT INTO notifications (user_id, message, type) VALUES (?, ?, ?)",
                 [new_id, f'Welcome to AgroVest, {full_name}! Your account is ready.', 'success'],
                 commit=True)

        flash('Account created successfully! Please log in.', 'success')
        return redirect(url_for('login'))

    return render_template('register.html', ref_code=ref_code)


@app.route('/login', methods=['GET', 'POST'])
def login():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')

        user = query_db("SELECT * FROM users WHERE email=?", [email], one=True)

        if user and check_password_hash(user['password_hash'], password):
            if not user['is_active']:
                flash('Your account has been suspended. Contact support.', 'error')
                return render_template('login.html')

            session.permanent = True
            session['user_id'] = user['id']
            session['is_admin'] = bool(user['is_admin'])

            flash(f'Welcome back, {user["full_name"].split()[0]}!', 'success')

            if user['is_admin']:
                return redirect(url_for('admin_dashboard'))
            return redirect(url_for('dashboard'))
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
        "SELECT * FROM investments WHERE user_id=? AND status='active' ORDER BY created_at DESC",
        [user['id']]
    )
    recent_deposits = query_db(
        "SELECT * FROM deposits WHERE user_id=? ORDER BY created_at DESC LIMIT 5",
        [user['id']]
    )
    recent_withdrawals = query_db(
        "SELECT * FROM withdrawals WHERE user_id=? ORDER BY created_at DESC LIMIT 5",
        [user['id']]
    )
    referrals = query_db(
        """SELECT u.full_name, u.created_at, r.commission, r.status
           FROM referrals r JOIN users u ON u.id=r.referred_id
           WHERE r.referrer_id=? ORDER BY r.created_at DESC""",
        [user['id']]
    )
    notifications = query_db(
        "SELECT * FROM notifications WHERE user_id=? ORDER BY created_at DESC LIMIT 10",
        [user['id']]
    )
    return render_template('dashboard.html',
                           user=user,
                           active_investments=active_investments,
                           recent_deposits=recent_deposits,
                           recent_withdrawals=recent_withdrawals,
                           referrals=referrals,
                           notifications=notifications,
                           plans=INVESTMENT_PLANS)


@app.route('/dashboard/invest', methods=['GET', 'POST'])
@login_required
def invest():
    user = get_current_user()

    if request.method == 'POST':
        plan_id = int(request.form.get('plan_id', 0))
        amount = float(request.form.get('amount', 0))

        plan = next((p for p in INVESTMENT_PLANS if p['id'] == plan_id), None)
        if not plan:
            flash('Invalid plan selected.', 'error')
            return redirect(url_for('invest'))

        if amount < plan['min_amount']:
            flash(f'Minimum investment for {plan["name"]} is ₦{plan["min_amount"]:,.0f}', 'error')
            return redirect(url_for('invest'))

        if plan['max_amount'] and amount > plan['max_amount']:
            flash(f'Maximum investment for {plan["name"]} is ₦{plan["max_amount"]:,.0f}', 'error')
            return redirect(url_for('invest'))

        if user['balance'] < amount:
            flash('Insufficient balance. Please deposit funds first.', 'error')
            return redirect(url_for('deposit'))

        expected_return = amount + (amount * plan['roi_percent'] / 100)
        end_date = datetime.now() + timedelta(days=plan['duration_days'])

        # Deduct from balance
        query_db("UPDATE users SET balance = balance - ?, total_invested = total_invested + ? WHERE id=?",
                 [amount, amount, user['id']], commit=True)

        # Create investment
        inv_id = query_db("""
            INSERT INTO investments (user_id, plan_id, plan_name, amount, roi_percent, expected_return, duration_days, end_date)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, [user['id'], plan['id'], plan['name'], amount, plan['roi_percent'],
              expected_return, plan['duration_days'], end_date], commit=True)

        # Referral commission
        if user['referred_by']:
            commission = amount * REFERRAL_COMMISSION / 100
            query_db("UPDATE users SET balance = balance + ?, referral_earnings = referral_earnings + ? WHERE id=?",
                     [commission, commission, user['referred_by']], commit=True)
            query_db("UPDATE referrals SET commission=commission+?, status='active' WHERE referrer_id=? AND referred_id=?",
                     [commission, user['referred_by'], user['id']], commit=True)
            query_db("INSERT INTO notifications (user_id, message, type) VALUES (?, ?, ?)",
                     [user['referred_by'],
                      f'You earned ₦{commission:,.2f} referral commission!', 'success'],
                     commit=True)

        query_db("INSERT INTO notifications (user_id, message, type) VALUES (?, ?, ?)",
                 [user['id'], f'Investment of ₦{amount:,.2f} in {plan["name"]} activated successfully!', 'success'],
                 commit=True)

        flash(f'Investment of ₦{amount:,.2f} activated! Expected return: ₦{expected_return:,.2f}', 'success')
        return redirect(url_for('dashboard'))

    return render_template('invest.html', user=user, plans=INVESTMENT_PLANS)


@app.route('/dashboard/deposit', methods=['GET', 'POST'])
@login_required
def deposit():
    user = get_current_user()

    if request.method == 'POST':
        amount = float(request.form.get('amount', 0))
        payment_method = request.form.get('payment_method', '')
        proof = request.files.get('proof')

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
            INSERT INTO deposits (user_id, amount, payment_method, proof_filename, reference)
            VALUES (?, ?, ?, ?, ?)
        """, [user['id'], amount, payment_method, proof_filename, reference], commit=True)

        query_db("INSERT INTO notifications (user_id, message, type) VALUES (?, ?, ?)",
                 [user['id'], f'Deposit request of ₦{amount:,.2f} submitted. Awaiting confirmation.', 'info'],
                 commit=True)

        flash('Deposit request submitted! It will be confirmed within 30 minutes.', 'success')
        return redirect(url_for('dashboard'))

    deposits = query_db("SELECT * FROM deposits WHERE user_id=? ORDER BY created_at DESC", [user['id']])
    return render_template('deposit.html', user=user, deposits=deposits)


@app.route('/dashboard/withdraw', methods=['GET', 'POST'])
@login_required
def withdraw():
    user = get_current_user()

    if request.method == 'POST':
        amount = float(request.form.get('amount', 0))
        bank_name = request.form.get('bank_name', '').strip()
        account_number = request.form.get('account_number', '').strip()
        account_name = request.form.get('account_name', '').strip()

        if amount < 2000:
            flash('Minimum withdrawal is ₦2,000.', 'error')
            return redirect(url_for('withdraw'))

        if user['balance'] < amount:
            flash('Insufficient balance.', 'error')
            return redirect(url_for('withdraw'))

        # Hold the amount
        query_db("UPDATE users SET balance = balance - ? WHERE id=?", [amount, user['id']], commit=True)

        query_db("""
            INSERT INTO withdrawals (user_id, amount, bank_name, account_number, account_name)
            VALUES (?, ?, ?, ?, ?)
        """, [user['id'], amount, bank_name, account_number, account_name], commit=True)

        query_db("INSERT INTO notifications (user_id, message, type) VALUES (?, ?, ?)",
                 [user['id'], f'Withdrawal request of ₦{amount:,.2f} submitted.', 'info'],
                 commit=True)

        flash('Withdrawal request submitted! Processing within 24 hours.', 'success')
        return redirect(url_for('dashboard'))

    withdrawals = query_db("SELECT * FROM withdrawals WHERE user_id=? ORDER BY created_at DESC", [user['id']])
    return render_template('withdraw.html', user=user, withdrawals=withdrawals)


@app.route('/dashboard/notifications/read', methods=['POST'])
@login_required
def mark_notifications_read():
    user = get_current_user()
    query_db("UPDATE notifications SET is_read=1 WHERE user_id=?", [user['id']], commit=True)
    return jsonify({'status': 'ok'})


# ─────────────────────────────────────────────
# Admin Dashboard
# ─────────────────────────────────────────────
@app.route('/admin')
@admin_required
def admin_dashboard():
    stats = {
        'total_users': query_db("SELECT COUNT(*) as c FROM users WHERE is_admin=0", one=True)['c'],
        'total_invested': query_db("SELECT COALESCE(SUM(amount),0) as s FROM investments", one=True)['s'],
        'pending_deposits': query_db("SELECT COUNT(*) as c FROM deposits WHERE status='pending'", one=True)['c'],
        'pending_withdrawals': query_db("SELECT COUNT(*) as c FROM withdrawals WHERE status='pending'", one=True)['c'],
        'total_paid_out': query_db("SELECT COALESCE(SUM(amount),0) as s FROM withdrawals WHERE status='approved'", one=True)['s'],
        'active_investments': query_db("SELECT COUNT(*) as c FROM investments WHERE status='active'", one=True)['c'],
    }
    recent_users = query_db("SELECT * FROM users WHERE is_admin=0 ORDER BY created_at DESC LIMIT 10")
    pending_deps = query_db("""
        SELECT d.*, u.full_name, u.email FROM deposits d JOIN users u ON u.id=d.user_id
        WHERE d.status='pending' ORDER BY d.created_at DESC
    """)
    pending_wds = query_db("""
        SELECT w.*, u.full_name, u.email FROM withdrawals w JOIN users u ON u.id=w.user_id
        WHERE w.status='pending' ORDER BY w.created_at DESC
    """)
    return render_template('admin/dashboard.html',
                           stats=stats,
                           recent_users=recent_users,
                           pending_deps=pending_deps,
                           pending_wds=pending_wds)


@app.route('/admin/users')
@admin_required
def admin_users():
    users = query_db("""
        SELECT u.*, 
               (SELECT COUNT(*) FROM investments WHERE user_id=u.id) as inv_count,
               (SELECT COUNT(*) FROM referrals WHERE referrer_id=u.id) as ref_count
        FROM users u WHERE u.is_admin=0 ORDER BY u.created_at DESC
    """)
    return render_template('admin/users.html', users=users)


@app.route('/admin/users/<int:user_id>/toggle', methods=['POST'])
@admin_required
def admin_toggle_user(user_id):
    user = query_db("SELECT * FROM users WHERE id=?", [user_id], one=True)
    if user:
        new_status = 0 if user['is_active'] else 1
        query_db("UPDATE users SET is_active=? WHERE id=?", [new_status, user_id], commit=True)
        flash(f'User {"activated" if new_status else "suspended"} successfully.', 'success')
    return redirect(url_for('admin_users'))


@app.route('/admin/users/<int:user_id>/credit', methods=['POST'])
@admin_required
def admin_credit_user(user_id):
    amount = float(request.form.get('amount', 0))
    note = request.form.get('note', 'Admin credit')
    if amount > 0:
        query_db("UPDATE users SET balance = balance + ? WHERE id=?", [amount, user_id], commit=True)
        query_db("INSERT INTO notifications (user_id, message, type) VALUES (?, ?, ?)",
                 [user_id, f'Your account has been credited with ₦{amount:,.2f}. {note}', 'success'],
                 commit=True)
        flash(f'User credited ₦{amount:,.2f} successfully.', 'success')
    return redirect(url_for('admin_users'))


@app.route('/admin/deposits')
@admin_required
def admin_deposits():
    deposits = query_db("""
        SELECT d.*, u.full_name, u.email FROM deposits d
        JOIN users u ON u.id=d.user_id ORDER BY d.created_at DESC
    """)
    return render_template('admin/deposits.html', deposits=deposits)


@app.route('/admin/deposits/<int:dep_id>/approve', methods=['POST'])
@admin_required
def admin_approve_deposit(dep_id):
    dep = query_db("SELECT * FROM deposits WHERE id=?", [dep_id], one=True)
    if dep and dep['status'] == 'pending':
        query_db("UPDATE deposits SET status='approved', updated_at=CURRENT_TIMESTAMP WHERE id=?",
                 [dep_id], commit=True)
        query_db("UPDATE users SET balance = balance + ? WHERE id=?",
                 [dep['amount'], dep['user_id']], commit=True)
        query_db("INSERT INTO notifications (user_id, message, type) VALUES (?, ?, ?)",
                 [dep['user_id'], f'Your deposit of ₦{dep["amount"]:,.2f} has been approved!', 'success'],
                 commit=True)
        flash('Deposit approved and balance updated.', 'success')
    return redirect(url_for('admin_deposits'))


@app.route('/admin/deposits/<int:dep_id>/reject', methods=['POST'])
@admin_required
def admin_reject_deposit(dep_id):
    note = request.form.get('note', 'Rejected by admin')
    dep = query_db("SELECT * FROM deposits WHERE id=?", [dep_id], one=True)
    if dep and dep['status'] == 'pending':
        query_db("UPDATE deposits SET status='rejected', admin_note=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                 [note, dep_id], commit=True)
        query_db("INSERT INTO notifications (user_id, message, type) VALUES (?, ?, ?)",
                 [dep['user_id'], f'Your deposit of ₦{dep["amount"]:,.2f} was rejected. Reason: {note}', 'error'],
                 commit=True)
        flash('Deposit rejected.', 'warning')
    return redirect(url_for('admin_deposits'))


@app.route('/admin/withdrawals')
@admin_required
def admin_withdrawals():
    withdrawals = query_db("""
        SELECT w.*, u.full_name, u.email FROM withdrawals w
        JOIN users u ON u.id=w.user_id ORDER BY w.created_at DESC
    """)
    return render_template('admin/withdrawals.html', withdrawals=withdrawals)


@app.route('/admin/withdrawals/<int:wd_id>/approve', methods=['POST'])
@admin_required
def admin_approve_withdrawal(wd_id):
    wd = query_db("SELECT * FROM withdrawals WHERE id=?", [wd_id], one=True)
    if wd and wd['status'] == 'pending':
        query_db("UPDATE withdrawals SET status='approved', updated_at=CURRENT_TIMESTAMP WHERE id=?",
                 [wd_id], commit=True)
        # Update total earnings stat
        query_db("UPDATE users SET total_earnings = total_earnings + ? WHERE id=?",
                 [wd['amount'], wd['user_id']], commit=True)
        query_db("INSERT INTO notifications (user_id, message, type) VALUES (?, ?, ?)",
                 [wd['user_id'], f'Your withdrawal of ₦{wd["amount"]:,.2f} has been approved and sent!', 'success'],
                 commit=True)
        flash('Withdrawal approved.', 'success')
    return redirect(url_for('admin_withdrawals'))


@app.route('/admin/withdrawals/<int:wd_id>/reject', methods=['POST'])
@admin_required
def admin_reject_withdrawal(wd_id):
    note = request.form.get('note', 'Rejected by admin')
    wd = query_db("SELECT * FROM withdrawals WHERE id=?", [wd_id], one=True)
    if wd and wd['status'] == 'pending':
        # Refund balance
        query_db("UPDATE users SET balance = balance + ? WHERE id=?",
                 [wd['amount'], wd['user_id']], commit=True)
        query_db("UPDATE withdrawals SET status='rejected', admin_note=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                 [note, wd_id], commit=True)
        query_db("INSERT INTO notifications (user_id, message, type) VALUES (?, ?, ?)",
                 [wd['user_id'], f'Withdrawal of ₦{wd["amount"]:,.2f} rejected. ₦{wd["amount"]:,.2f} refunded to balance. Reason: {note}', 'warning'],
                 commit=True)
        flash('Withdrawal rejected and balance refunded.', 'warning')
    return redirect(url_for('admin_withdrawals'))


@app.route('/admin/investments')
@admin_required
def admin_investments():
    investments = query_db("""
        SELECT i.*, u.full_name, u.email FROM investments i
        JOIN users u ON u.id=i.user_id ORDER BY i.created_at DESC
    """)
    return render_template('admin/investments.html', investments=investments)


@app.route('/admin/investments/<int:inv_id>/complete', methods=['POST'])
@admin_required
def admin_complete_investment(inv_id):
    inv = query_db("SELECT * FROM investments WHERE id=?", [inv_id], one=True)
    if inv and inv['status'] == 'active':
        query_db("UPDATE investments SET status='completed' WHERE id=?", [inv_id], commit=True)
        query_db("UPDATE users SET balance = balance + ?, total_earnings = total_earnings + ? WHERE id=?",
                 [inv['expected_return'], inv['expected_return'] - inv['amount'], inv['user_id']],
                 commit=True)
        query_db("INSERT INTO notifications (user_id, message, type) VALUES (?, ?, ?)",
                 [inv['user_id'],
                  f'Your {inv["plan_name"]} investment matured! ₦{inv["expected_return"]:,.2f} credited to your balance.',
                  'success'],
                 commit=True)
        flash('Investment marked complete and balance updated.', 'success')
    return redirect(url_for('admin_investments'))


# ─────────────────────────────────────────────
# Error Handlers
# ─────────────────────────────────────────────
@app.errorhandler(403)
def forbidden(e):
    return render_template('errors/403.html'), 403

@app.errorhandler(404)
def not_found(e):
    return render_template('errors/404.html'), 404

@app.errorhandler(500)
def server_error(e):
    return render_template('errors/500.html'), 500

# ─────────────────────────────────────────────
# Template Filters
# ─────────────────────────────────────────────
@app.template_filter('naira')
def naira_filter(value):
    try:
        return f'₦{float(value):,.2f}'
    except (TypeError, ValueError):
        return '₦0.00'

@app.template_filter('date_fmt')
def date_fmt(value, fmt='%d %b %Y'):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except Exception:
            return value
    return value.strftime(fmt) if value else ''

@app.template_filter('status_badge')
def status_badge(status):
    badges = {
        'pending': 'badge-warning',
        'approved': 'badge-success',
        'rejected': 'badge-error',
        'active': 'badge-info',
        'completed': 'badge-success',
    }
    return badges.get(status, 'badge-neutral')

# ─────────────────────────────────────────────
# Run
# ─────────────────────────────────────────────
if __name__ == '__main__':
    with app.app_context():
        init_db()
    app.run(debug=True, port=5000)
