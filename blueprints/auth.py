from flask import (Blueprint, render_template, redirect, url_for,
                   request, session, flash, current_app)
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta, timezone
from utils.supabase_client import db_select, db_insert, db_update
from utils.helpers import generate_token, hash_token, generate_referral_code, log_audit
from utils.email import send_verification_email, send_password_reset_email
from utils.decorators import guest_only, login_required

auth_bp = Blueprint("auth", __name__)


def _set_session(user: dict):
    session.permanent = True
    session["user_id"]     = user["id"]
    session["username"]    = user["username"]
    session["email"]       = user["email"]
    session["role"]        = user["role"]
    session["is_verified"] = user.get("is_verified", False)
    session["is_banned"]   = user.get("is_banned", False)
    session["balance"]     = float(user.get("balance", 0))


# ── Login ─────────────────────────────────────────────────────

@auth_bp.route("/login", methods=["GET", "POST"])
@guest_only
def login():
    if request.method == "POST":
        identifier = request.form.get("identifier", "").strip().lower()
        password   = request.form.get("password", "")
        remember   = request.form.get("remember") == "on"

        if not identifier or not password:
            flash("Please fill in all fields.", "danger")
            return render_template("auth/login.html")

        # Find user by email or username
        user = (db_select("users", filters={"email": identifier}, single=True)
                or db_select("users", filters={"username": identifier}, single=True))

        if not user:
            flash("Invalid credentials.", "danger")
            return render_template("auth/login.html")

        # Check lockout
        if user.get("locked_until"):
            locked = datetime.fromisoformat(user["locked_until"].replace("Z", "+00:00"))
            if locked > datetime.now(timezone.utc):
                flash("Account temporarily locked. Try again later.", "danger")
                return render_template("auth/login.html")

        if not check_password_hash(user["password_hash"], password):
            fails = (user.get("failed_login_count") or 0) + 1
            update = {"failed_login_count": fails}
            if fails >= 5:
                update["locked_until"] = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()
            db_update("users", update, {"id": user["id"]})
            flash("Invalid credentials.", "danger")
            return render_template("auth/login.html")

        if user.get("is_banned"):
            flash("Your account has been suspended. Contact support.", "danger")
            return render_template("auth/login.html")

        if user.get("deleted_at"):
            flash("This account no longer exists.", "danger")
            return render_template("auth/login.html")

        # Successful login
        db_update("users", {
            "last_login": datetime.now(timezone.utc).isoformat(),
            "login_count": (user.get("login_count") or 0) + 1,
            "failed_login_count": 0,
            "locked_until": None,
        }, {"id": user["id"]})

        if not remember:
            session.permanent = False
        _set_session(user)
        log_audit(user["id"], "login", ip=request.remote_addr, ua=request.user_agent.string)

        flash(f"Welcome back, {user['username']}! 👋", "success")
        next_url = request.args.get("next") or url_for("dashboard.index")
        return redirect(next_url)

    return render_template("auth/login.html")


# ── Register ──────────────────────────────────────────────────

@auth_bp.route("/register", methods=["GET", "POST"])
@guest_only
def register():
    if request.method == "POST":
        username  = request.form.get("username", "").strip()
        email     = request.form.get("email", "").strip().lower()
        password  = request.form.get("password", "")
        confirm   = request.form.get("confirm_password", "")
        ref_code  = request.form.get("referral_code", "").strip().upper()
        terms     = request.form.get("terms") == "on"

        # Validation
        errors = []
        if not username or len(username) < 3:
            errors.append("Username must be at least 3 characters.")
        if not email or "@" not in email:
            errors.append("Enter a valid email address.")
        if len(password) < 8:
            errors.append("Password must be at least 8 characters.")
        if password != confirm:
            errors.append("Passwords do not match.")
        if not terms:
            errors.append("You must accept the terms of service.")

        if errors:
            for e in errors:
                flash(e, "danger")
            return render_template("auth/register.html", form=request.form)

        # Check duplicates
        if db_select("users", filters={"email": email}, single=True):
            flash("An account with this email already exists.", "danger")
            return render_template("auth/register.html", form=request.form)
        if db_select("users", filters={"username": username}, single=True):
            flash("This username is already taken.", "danger")
            return render_template("auth/register.html", form=request.form)

        # Handle referral
        referrer_id = None
        if ref_code:
            profile = db_select("user_profiles", "user_id",
                                filters={"referral_code": ref_code}, single=True)
            if profile:
                referrer_id = profile["user_id"]

        # Create verification token
        raw_token  = generate_token()
        token_hash = hash_token(raw_token)
        expires    = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()

        user = db_insert("users", {
            "email":                      email,
            "username":                   username,
            "password_hash":              generate_password_hash(password),
            "role":                       "buyer",
            "email_verification_token":   token_hash,
            "email_verification_expires": expires,
        })

        if not user:
            flash("Registration failed. Please try again.", "danger")
            return render_template("auth/register.html", form=request.form)

        # Create profile
        db_insert("user_profiles", {
            "user_id":       user["id"],
            "referral_code": generate_referral_code(username),
            "referred_by":   referrer_id,
        })

        # Give referral bonus
        if referrer_id:
            bonus = current_app.config.get("REFERRAL_BONUS", 5.0)
            ref_user = db_select("users", "id,balance", filters={"id": referrer_id}, single=True)
            if ref_user:
                bal_before = float(ref_user["balance"])
                db_update("users", {"balance": bal_before + bonus}, {"id": referrer_id})
                bal_after = bal_before + bonus
                db_insert("wallet_transactions", {
                    "user_id": referrer_id, "type": "referral",
                    "amount": bonus, "balance_before": bal_before,
                    "balance_after": bal_after, "status": "completed",
                    "description": f"Referral bonus for inviting {username}",
                })
                db_update("user_profiles", {"referral_count": 1}, {"user_id": referrer_id})
                db_insert("notifications", {
                    "user_id": referrer_id, "type": "referral",
                    "title": "Referral Bonus!",
                    "message": f"You earned ${bonus:.2f} for referring {username}!",
                    "link": "/dashboard/referrals", "icon": "gift",
                })

        # Send verification email
        verify_url = url_for("auth.verify_email", token=raw_token, _external=True)
        send_verification_email(email, username, verify_url)

        flash("Account created! Please check your email to verify your account.", "success")
        log_audit(user["id"], "register", ip=request.remote_addr)
        return redirect(url_for("auth.verify_notice"))

    ref_code = request.args.get("ref", "")
    return render_template("auth/register.html", form={}, ref_code=ref_code)


# ── Email verification ────────────────────────────────────────

@auth_bp.route("/verify-notice")
def verify_notice():
    return render_template("auth/verify_email.html")


@auth_bp.route("/verify-email/<token>")
def verify_email(token):
    token_hash = hash_token(token)
    user = db_select("users", filters={"email_verification_token": token_hash}, single=True)

    if not user:
        flash("Invalid or expired verification link.", "danger")
        return redirect(url_for("auth.login"))

    expires = user.get("email_verification_expires")
    if expires:
        exp_dt = datetime.fromisoformat(expires.replace("Z", "+00:00"))
        if exp_dt < datetime.now(timezone.utc):
            flash("Verification link has expired. Please request a new one.", "danger")
            return redirect(url_for("auth.resend_verification"))

    db_update("users", {
        "is_verified": True,
        "email_verification_token": None,
        "email_verification_expires": None,
    }, {"id": user["id"]})

    log_audit(user["id"], "verify_email")
    flash("Email verified! Your account is now active. 🎉", "success")
    return redirect(url_for("auth.login"))


@auth_bp.route("/resend-verification")
@login_required
def resend_verification():
    if session.get("is_verified"):
        return redirect(url_for("dashboard.index"))
    user_id = session["user_id"]
    user    = db_select("users", "id,email,username", filters={"id": user_id}, single=True)
    raw     = generate_token()
    expires = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
    db_update("users", {
        "email_verification_token":   hash_token(raw),
        "email_verification_expires": expires,
    }, {"id": user_id})
    verify_url = url_for("auth.verify_email", token=raw, _external=True)
    send_verification_email(user["email"], user["username"], verify_url)
    flash("Verification email resent. Please check your inbox.", "success")
    return redirect(url_for("auth.verify_notice"))


# ── Forgot Password ───────────────────────────────────────────

@auth_bp.route("/forgot-password", methods=["GET", "POST"])
@guest_only
def forgot_password():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        if not email:
            flash("Please enter your email address.", "danger")
            return render_template("auth/forgot_password.html")

        user = db_select("users", filters={"email": email}, single=True)
        # Always show success to prevent user enumeration
        if user:
            raw     = generate_token()
            expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
            db_update("users", {
                "password_reset_token":   hash_token(raw),
                "password_reset_expires": expires,
            }, {"id": user["id"]})
            reset_url = url_for("auth.reset_password", token=raw, _external=True)
            send_password_reset_email(email, user["username"], reset_url)

        flash("If that email exists, a reset link has been sent.", "success")
        return redirect(url_for("auth.forgot_password"))

    return render_template("auth/forgot_password.html")


# ── Reset Password ────────────────────────────────────────────

@auth_bp.route("/reset-password/<token>", methods=["GET", "POST"])
@guest_only
def reset_password(token):
    token_hash = hash_token(token)
    user = db_select("users", filters={"password_reset_token": token_hash}, single=True)

    if not user:
        flash("Invalid or expired reset link.", "danger")
        return redirect(url_for("auth.forgot_password"))

    expires = user.get("password_reset_expires")
    if expires:
        exp_dt = datetime.fromisoformat(expires.replace("Z", "+00:00"))
        if exp_dt < datetime.now(timezone.utc):
            flash("Reset link has expired. Request a new one.", "danger")
            return redirect(url_for("auth.forgot_password"))

    if request.method == "POST":
        password = request.form.get("password", "")
        confirm  = request.form.get("confirm_password", "")
        if len(password) < 8:
            flash("Password must be at least 8 characters.", "danger")
            return render_template("auth/reset_password.html", token=token)
        if password != confirm:
            flash("Passwords do not match.", "danger")
            return render_template("auth/reset_password.html", token=token)

        db_update("users", {
            "password_hash":          generate_password_hash(password),
            "password_reset_token":   None,
            "password_reset_expires": None,
            "failed_login_count":     0,
            "locked_until":           None,
        }, {"id": user["id"]})

        log_audit(user["id"], "password_reset")
        flash("Password reset successfully. You can now log in.", "success")
        return redirect(url_for("auth.login"))

    return render_template("auth/reset_password.html", token=token)


# ── Logout ────────────────────────────────────────────────────

@auth_bp.route("/logout")
@login_required
def logout():
    log_audit(session.get("user_id"), "logout")
    session.clear()
    flash("You've been logged out.", "info")
    return redirect(url_for("main.index"))
