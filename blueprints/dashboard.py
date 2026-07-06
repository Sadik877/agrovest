from flask import (Blueprint, render_template, redirect, url_for,
                   request, session, flash, current_app, jsonify, send_file)
from datetime import datetime, timezone, timedelta
from werkzeug.security import generate_password_hash, check_password_hash
from utils.supabase_client import (db_select, db_insert, db_update,
                                   db_delete, storage_upload, storage_signed_url)
from utils.decorators import login_required, verified_required
from utils.helpers import (generate_token, hash_token, generate_reference,
                           fmt_price, allowed_image, safe_filename, log_audit)
from utils.email import send_deposit_confirmation, send_withdrawal_processed

dashboard_bp = Blueprint("dashboard", __name__)


def _current_user():
    uid = session.get("user_id")
    return db_select("users", "*", filters={"id": uid}, single=True) if uid else None


def _profile():
    return db_select("user_profiles", "*", filters={"user_id": session["user_id"]}, single=True)


# ── Overview ──────────────────────────────────────────────────

@dashboard_bp.route("/")
@login_required
def index():
    uid   = session["user_id"]
    user  = _current_user()
    prof  = _profile()

    # Refresh balance in session
    if user:
        session["balance"] = float(user.get("balance", 0))

    # Recent purchases
    purchases = db_select("orders", "*", filters={"buyer_id": uid}, order="-created_at", limit=5)
    # Recent sales (if seller)
    sales = []
    if session.get("role") in ("seller", "admin"):
        sales = db_select("orders", "*", filters={"seller_id": uid}, order="-created_at", limit=5)

    # Unread notifications count
    all_notifs = db_select("notifications", "id", filters={"user_id": uid, "is_read": False})

    # Wishlist count
    wl = db_select("wishlist", "id", filters={"user_id": uid})

    # Wallet stats
    tx = db_select("wallet_transactions", "type,amount,status",
                   filters={"user_id": uid, "status": "completed"})
    total_spent   = sum(float(t["amount"]) for t in tx if t["type"] == "purchase")
    total_earned  = sum(float(t["amount"]) for t in tx if t["type"] == "sale")
    total_deposit = sum(float(t["amount"]) for t in tx if t["type"] == "deposit")

    return render_template("dashboard/index.html",
        user=user, profile=prof,
        purchases=purchases, sales=sales,
        unread_notifications=len(all_notifs),
        wishlist_count=len(wl),
        total_spent=total_spent,
        total_earned=total_earned,
        total_deposit=total_deposit,
    )


# ── Wallet ────────────────────────────────────────────────────

@dashboard_bp.route("/wallet")
@login_required
def wallet():
    uid  = session["user_id"]
    user = _current_user()
    session["balance"] = float(user.get("balance", 0))

    transactions = db_select("wallet_transactions", "*",
                             filters={"user_id": uid}, order="-created_at", limit=50)
    pending_deposits = db_select("wallet_transactions", "*",
                                 filters={"user_id": uid, "type": "deposit", "status": "pending"})
    pending_withdrawals = db_select("wallet_transactions", "*",
                                    filters={"user_id": uid, "type": "withdrawal", "status": "pending"})

    cfg = current_app.config
    return render_template("dashboard/wallet.html",
        user=user,
        transactions=transactions,
        pending_deposits=pending_deposits,
        pending_withdrawals=pending_withdrawals,
        stripe_pk=cfg.get("STRIPE_PUBLISHABLE_KEY", ""),
        paystack_pk=cfg.get("PAYSTACK_PUBLIC_KEY", ""),
        flw_pk=cfg.get("FLUTTERWAVE_PUBLIC_KEY", ""),
        min_deposit=cfg.get("MIN_DEPOSIT", 5),
        min_withdrawal=cfg.get("MIN_WITHDRAWAL", 10),
        max_withdrawal=cfg.get("MAX_WITHDRAWAL", 10000),
    )


@dashboard_bp.route("/wallet/deposit", methods=["POST"])
@login_required
def wallet_deposit():
    uid    = session["user_id"]
    amount = request.form.get("amount", "")
    method = request.form.get("method", "")
    ref    = request.form.get("reference", "")

    try:
        amount = float(amount)
        assert amount >= current_app.config.get("MIN_DEPOSIT", 5)
    except (ValueError, AssertionError):
        flash(f"Minimum deposit is ${current_app.config.get('MIN_DEPOSIT', 5):.2f}.", "danger")
        return redirect(url_for("dashboard.wallet"))

    user = _current_user()
    bal  = float(user.get("balance", 0))
    reference = ref or generate_reference("DEP")

    db_insert("wallet_transactions", {
        "user_id":        uid,
        "type":           "deposit",
        "amount":         amount,
        "balance_before": bal,
        "balance_after":  bal,         # Updated when admin approves
        "reference":      reference,
        "status":         "pending",
        "payment_method": method,
        "description":    f"Wallet deposit via {method}",
    })
    log_audit(uid, "wallet_deposit_request", details={"amount": amount, "method": method})
    flash("Deposit request submitted. It will be processed within 24 hours.", "success")
    return redirect(url_for("dashboard.wallet"))


@dashboard_bp.route("/wallet/withdraw", methods=["POST"])
@login_required
@verified_required
def wallet_withdraw():
    uid    = session["user_id"]
    amount = request.form.get("amount", "")
    method = request.form.get("method", "bank_transfer")
    details = request.form.get("details", "").strip()

    try:
        amount = float(amount)
        cfg    = current_app.config
        assert cfg.get("MIN_WITHDRAWAL", 10) <= amount <= cfg.get("MAX_WITHDRAWAL", 10000)
    except (ValueError, AssertionError):
        flash("Invalid withdrawal amount.", "danger")
        return redirect(url_for("dashboard.wallet"))

    user = _current_user()
    bal  = float(user.get("balance", 0))

    if amount > bal:
        flash("Insufficient wallet balance.", "danger")
        return redirect(url_for("dashboard.wallet"))

    reference = generate_reference("WDR")
    db_insert("wallet_transactions", {
        "user_id":        uid,
        "type":           "withdrawal",
        "amount":         amount,
        "balance_before": bal,
        "balance_after":  bal - amount,
        "reference":      reference,
        "status":         "pending",
        "payment_method": method,
        "description":    f"Withdrawal request — {method}",
        "metadata":       {"payout_details": details},
    })
    log_audit(uid, "wallet_withdraw_request", details={"amount": amount, "method": method})
    flash("Withdrawal request submitted. Processing takes 1–2 business days.", "success")
    return redirect(url_for("dashboard.wallet"))


# ── Orders ────────────────────────────────────────────────────

@dashboard_bp.route("/orders")
@login_required
def orders():
    uid    = session["user_id"]
    status = request.args.get("status", "")
    page   = int(request.args.get("page", 1))

    filters = {"buyer_id": uid}
    if status:
        filters["status"] = status

    all_orders = db_select("orders", "*", filters=filters, order="-created_at")
    per_page   = 20
    total      = len(all_orders)
    start      = (page - 1) * per_page
    paginated  = all_orders[start: start + per_page]
    pages      = max(1, -(-total // per_page))

    return render_template("dashboard/orders.html",
        orders=paginated, status=status, page=page, pages=pages, total=total)


# ── Purchases (with download links) ──────────────────────────

@dashboard_bp.route("/purchases")
@login_required
def purchases():
    uid  = session["user_id"]
    page = int(request.args.get("page", 1))

    completed = db_select("orders", "*",
                          filters={"buyer_id": uid, "status": "completed"},
                          order="-created_at")
    per_page  = 20
    total     = len(completed)
    start     = (page - 1) * per_page
    paginated = completed[start: start + per_page]
    pages     = max(1, -(-total // per_page))

    # Enrich with order items
    for order in paginated:
        order["items"] = db_select("order_items", "*", filters={"order_id": order["id"]})

    return render_template("dashboard/purchases.html",
        orders=paginated, page=page, pages=pages, total=total)


@dashboard_bp.route("/purchases/<order_item_id>/download")
@login_required
def download_item(order_item_id):
    uid  = session["user_id"]
    item = db_select("order_items", "*", filters={"id": order_item_id}, single=True)
    if not item:
        flash("Item not found.", "danger")
        return redirect(url_for("dashboard.purchases"))

    order = db_select("orders", "buyer_id,status", filters={"id": item["order_id"]}, single=True)
    if not order or order["buyer_id"] != uid or order["status"] != "completed":
        flash("Access denied.", "danger")
        return redirect(url_for("dashboard.purchases"))

    # Check expiry and download limit
    expires = item.get("download_expires_at")
    if expires:
        exp_dt = datetime.fromisoformat(expires.replace("Z", "+00:00"))
        if exp_dt < datetime.now(timezone.utc):
            flash("Download link has expired. Contact support.", "warning")
            return redirect(url_for("dashboard.purchases"))

    max_dl = item.get("max_downloads", 5)
    if item.get("download_count", 0) >= max_dl:
        flash("Maximum downloads reached. Contact support for more.", "warning")
        return redirect(url_for("dashboard.purchases"))

    db_update("order_items",
              {"download_count": (item.get("download_count") or 0) + 1},
              {"id": order_item_id})
    log_audit(uid, "download", resource_type="order_item", resource_id=order_item_id)
    return redirect(item.get("download_url", url_for("dashboard.purchases")))


# ── Messages ──────────────────────────────────────────────────

@dashboard_bp.route("/messages")
@login_required
def messages():
    uid    = session["user_id"]
    convos = db_select("conversations", "*",
                       filters={"participant_1": uid}, order="-last_message_at")
    convos += db_select("conversations", "*",
                        filters={"participant_2": uid}, order="-last_message_at")
    convos.sort(key=lambda x: x.get("last_message_at") or "", reverse=True)

    # Enrich with other participant info
    for c in convos:
        other_id = c["participant_2"] if c["participant_1"] == uid else c["participant_1"]
        other    = db_select("users", "id,username", filters={"id": other_id}, single=True)
        op       = db_select("user_profiles", "avatar_url", filters={"user_id": other_id}, single=True)
        c["other_user"]   = other
        c["other_avatar"] = op.get("avatar_url") if op else None
        c["unread"]       = (c["unread_count_1"] if c["participant_1"] == uid
                             else c["unread_count_2"])

    active_id = request.args.get("conversation")
    chat      = []
    active    = None
    if active_id:
        active = next((c for c in convos if c["id"] == active_id), None)
        if active:
            chat = db_select("messages", "*",
                             filters={"conversation_id": active_id}, order="created_at")
            # Mark as read
            unread_field = ("unread_count_1" if active["participant_1"] == uid
                            else "unread_count_2")
            db_update("conversations", {unread_field: 0}, {"id": active_id})
            db_update("messages", {"is_read": True}, {"conversation_id": active_id,
                                                       "receiver_id": uid, "is_read": False})

    return render_template("dashboard/messages.html",
        conversations=convos, active=active,
        chat_messages=chat, active_id=active_id)


@dashboard_bp.route("/messages/send", methods=["POST"])
@login_required
def send_message():
    uid      = session["user_id"]
    recv_id  = request.form.get("receiver_id", "").strip()
    content  = request.form.get("content", "").strip()
    conv_id  = request.form.get("conversation_id", "").strip()

    if not recv_id or not content:
        flash("Message cannot be empty.", "danger")
        return redirect(url_for("dashboard.messages"))

    # Find or create conversation
    if not conv_id:
        conv = (db_select("conversations", "*",
                          filters={"participant_1": uid, "participant_2": recv_id}, single=True)
                or db_select("conversations", "*",
                             filters={"participant_1": recv_id, "participant_2": uid}, single=True))
        if not conv:
            conv = db_insert("conversations", {
                "participant_1": uid, "participant_2": recv_id,
                "last_message_at": datetime.now(timezone.utc).isoformat(),
            })
        conv_id = conv["id"]

    db_insert("messages", {
        "conversation_id": conv_id,
        "sender_id":       uid,
        "receiver_id":     recv_id,
        "content":         content[:2000],
    })
    db_update("conversations", {
        "last_message_at": datetime.now(timezone.utc).isoformat(),
        "unread_count_2":  1,
    }, {"id": conv_id})

    db_insert("notifications", {
        "user_id": recv_id,
        "type":    "message",
        "title":   f"New message from {session['username']}",
        "message": content[:100] + ("…" if len(content) > 100 else ""),
        "link":    f"/dashboard/messages?conversation={conv_id}",
        "icon":    "message-circle",
    })
    return redirect(url_for("dashboard.messages", conversation=conv_id))


# ── Notifications ─────────────────────────────────────────────

@dashboard_bp.route("/notifications")
@login_required
def notifications():
    uid   = session["user_id"]
    page  = int(request.args.get("page", 1))
    notifs = db_select("notifications", "*", filters={"user_id": uid}, order="-created_at")
    per_page = 30
    total    = len(notifs)
    start    = (page - 1) * per_page
    paginated = notifs[start: start + per_page]
    pages     = max(1, -(-total // per_page))
    unread    = sum(1 for n in notifs if not n["is_read"])
    return render_template("dashboard/notifications.html",
        notifications=paginated, unread=unread, page=page, pages=pages, total=total)


@dashboard_bp.route("/notifications/mark-all-read", methods=["POST"])
@login_required
def mark_all_notifications_read():
    db_update("notifications", {"is_read": True},
              {"user_id": session["user_id"], "is_read": False})
    return redirect(url_for("dashboard.notifications"))


# ── Wishlist ──────────────────────────────────────────────────

@dashboard_bp.route("/wishlist")
@login_required
def wishlist():
    uid   = session["user_id"]
    items = db_select("wishlist", "*", filters={"user_id": uid}, order="-created_at")
    listings = []
    for item in items:
        listing = db_select("listings",
                            "id,title,slug,price,compare_price,rating,preview_images",
                            filters={"id": item["listing_id"]}, single=True)
        if listing:
            listings.append(listing)
    return render_template("dashboard/wishlist.html", listings=listings)


# ── Settings ──────────────────────────────────────────────────

@dashboard_bp.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    uid  = session["user_id"]
    user = _current_user()
    prof = _profile()

    if request.method == "POST":
        action = request.form.get("action", "profile")

        if action == "profile":
            full_name = request.form.get("full_name", "").strip()[:255]
            bio       = request.form.get("bio", "").strip()[:500]
            phone     = request.form.get("phone", "").strip()[:50]
            country   = request.form.get("country", "").strip()[:100]
            website   = request.form.get("website", "").strip()[:255]
            twitter   = request.form.get("twitter", "").strip()[:100]
            github    = request.form.get("github", "").strip()[:100]
            linkedin  = request.form.get("linkedin", "").strip()[:255]

            db_update("user_profiles", {
                "full_name": full_name, "bio": bio, "phone": phone,
                "country": country, "website": website,
                "twitter": twitter, "github": github, "linkedin": linkedin,
            }, {"user_id": uid})
            flash("Profile updated successfully.", "success")

        elif action == "password":
            current  = request.form.get("current_password", "")
            new_pw   = request.form.get("new_password", "")
            confirm  = request.form.get("confirm_password", "")
            if not check_password_hash(user["password_hash"], current):
                flash("Current password is incorrect.", "danger")
            elif len(new_pw) < 8:
                flash("New password must be at least 8 characters.", "danger")
            elif new_pw != confirm:
                flash("Passwords do not match.", "danger")
            else:
                db_update("users", {"password_hash": generate_password_hash(new_pw)}, {"id": uid})
                log_audit(uid, "password_change")
                flash("Password changed successfully.", "success")

        elif action == "avatar":
            f = request.files.get("avatar")
            if f and f.filename and allowed_image(f.filename):
                bucket  = current_app.config["SUPABASE_BUCKET"]
                ext     = f.filename.rsplit(".", 1)[-1].lower()
                path    = f"avatars/{uid}.{ext}"
                url     = storage_upload(bucket, path, f.read(), f"image/{ext}")
                if url:
                    db_update("user_profiles", {"avatar_url": url}, {"user_id": uid})
                    flash("Avatar updated.", "success")
                else:
                    flash("Upload failed. Try again.", "danger")
            else:
                flash("Invalid image file.", "danger")

        elif action == "notifications":
            db_update("user_profiles", {
                "notifications_email": request.form.get("notif_email") == "on",
                "notifications_inapp": request.form.get("notif_inapp") == "on",
            }, {"user_id": uid})
            flash("Notification preferences saved.", "success")

        return redirect(url_for("dashboard.settings"))

    return render_template("dashboard/settings.html", user=user, profile=prof)


@dashboard_bp.route("/become-seller", methods=["POST"])
@login_required
@verified_required
def become_seller():
    uid        = session["user_id"]
    store_name = request.form.get("store_name", "").strip()[:255]
    store_desc = request.form.get("store_description", "").strip()[:1000]

    if not store_name:
        flash("Store name is required.", "danger")
        return redirect(url_for("dashboard.settings"))

    from python_slugify import slugify as _slugify
    slug = _slugify(store_name)

    db_update("users", {"role": "seller"}, {"id": uid})
    db_update("user_profiles", {
        "store_name": store_name,
        "store_slug": slug,
        "store_description": store_desc,
    }, {"user_id": uid})

    session["role"] = "seller"
    log_audit(uid, "became_seller", details={"store_name": store_name})
    flash(f"Welcome to MercX Sellers! Your store "{store_name}" is live. 🎉", "success")
    return redirect(url_for("seller.dashboard"))


# ── Referrals ─────────────────────────────────────────────────

@dashboard_bp.route("/referrals")
@login_required
def referrals():
    uid  = session["user_id"]
    prof = _profile()
    ref_code = prof.get("referral_code") if prof else ""

    # Users who used this referral code
    referred_users = []
    if ref_code:
        profiles = db_select("user_profiles", "user_id,created_at",
                             filters={"referred_by": uid})
        for p in profiles:
            u = db_select("users", "id,username,created_at",
                          filters={"id": p["user_id"]}, single=True)
            if u:
                referred_users.append(u)

    ref_tx = db_select("wallet_transactions", "amount,created_at",
                       filters={"user_id": uid, "type": "referral", "status": "completed"})
    total_earned = sum(float(t["amount"]) for t in ref_tx)
    ref_url      = url_for("auth.register", ref=ref_code, _external=True)

    return render_template("dashboard/referrals.html",
        profile=prof, ref_url=ref_url,
        referred_users=referred_users,
        total_earned=total_earned,
        bonus=current_app.config.get("REFERRAL_BONUS", 5.0),
    )
