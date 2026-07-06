from functools import wraps
from flask import session, redirect, url_for, flash, abort, request, jsonify


def login_required(f):
    """Redirect to login if user is not authenticated."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in to continue.", "warning")
            return redirect(url_for("auth.login", next=request.path))
        return f(*args, **kwargs)
    return decorated


def seller_required(f):
    """Require the user to have seller role."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in to continue.", "warning")
            return redirect(url_for("auth.login", next=request.path))
        if session.get("role") not in ("seller", "admin", "moderator"):
            flash("You need a seller account to access this page.", "warning")
            return redirect(url_for("seller.become_seller"))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    """Require the user to have admin role."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("auth.login", next=request.path))
        if session.get("role") not in ("admin", "moderator"):
            abort(403)
        return f(*args, **kwargs)
    return decorated


def super_admin_required(f):
    """Require the user to have the admin (not just moderator) role."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("auth.login", next=request.path))
        if session.get("role") != "admin":
            abort(403)
        return f(*args, **kwargs)
    return decorated


def verified_required(f):
    """Require that the user's email is verified."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("is_verified"):
            flash("Please verify your email address first.", "warning")
            return redirect(url_for("auth.verify_notice"))
        return f(*args, **kwargs)
    return decorated


def guest_only(f):
    """Redirect logged-in users away from auth pages."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" in session:
            return redirect(url_for("dashboard.index"))
        return f(*args, **kwargs)
    return decorated


def api_login_required(f):
    """Return JSON 401 instead of redirect for API endpoints."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return jsonify({"error": "Authentication required."}), 401
        return f(*args, **kwargs)
    return decorated


def not_banned(f):
    """Block banned/suspended users."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get("is_banned"):
            flash("Your account has been suspended. Contact support.", "danger")
            return redirect(url_for("main.index"))
        return f(*args, **kwargs)
    return decorated
