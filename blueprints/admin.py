from flask import (Blueprint, render_template, redirect, url_for,
                   request, session, flash, current_app, jsonify)
from datetime import datetime, timezone
from utils.supabase_client import (db_select, db_insert, db_update, db_delete)
from utils.decorators import admin_required, super_admin_required
from utils.helpers import (make_slug, calc_platform_fee, generate_reference, log_audit)
from utils.email import (send_listing_status, send_withdrawal_processed)

admin_bp = Blueprint("admin", __name__)


# ── Dashboard ─────────────────────────────────────────────────

@admin_bp.route("/")
@admin_required
def dashboard():
    # Platform stats
    total_users     = len(db_select("users", "id"))
    total_sellers   = len(db_select("users", "id", filters={"role": "seller"}))
    total_listings  = len(db_select("listings", "id", filters={"status": "active", "is_approved": True}))
    pending_ap      = len(db_select("listings", "id", filters={"status": "pending"}))
    total_orders    = len(db_select("orders", "id"))
    completed_orders = len(db_select("orders", "id", filters={"status": "completed"}))

    # Revenue
    completed = db_select("orders", "total,platform_fee,created_at",
                          filters={"status": "completed"})
    total_revenue  = sum(float(o["total"]) for o in completed)
    platform_fees  = sum(float(o.get("platform_fee") or 0) for o in completed)

    # Monthly revenue
    monthly = {}
    for o in completed:
        m = (o.get("created_at") or "")[:7]
        if m:
            monthly[m] = monthly.get(m, 0) + float(o["total"])
    months_12      = sorted(monthly)[-12:]
    revenue_values = [monthly.get(m, 0) for m in months_12]

    # Recent activity
    recent_users   = db_select("users", "id,username,email,role,created_at",
                               order="-created_at", limit=5)
    recent_orders  = db_select("orders", "id,order_number,status,total,created_at",
                               order="-created_at", limit=5)
    pending_deposits = db_select("wallet_transactions", "*",
                                 filters={"type": "deposit", "status": "pending"})
    pending_withdrawals = db_select("wallet_transactions", "*",
                                    filters={"type": "withdrawal", "status": "pending"})

    # User registration trend
    user_monthly = {}
    for u in db_select("users", "created_at"):
        m = (u.get("created_at") or "")[:7]
        if m:
            user_monthly[m] = user_monthly.get(m, 0) + 1
    user_months   = sorted(user_monthly)[-6:]
    user_values   = [user_monthly.get(m, 0) for m in user_months]

    return render_template("admin/dashboard.html",
        total_users=total_users,
        total_sellers=total_sellers,
        total_listings=total_listings,
        pending_approval=pending_ap,
        total_orders=total_orders,
        completed_orders=completed_orders,
        total_revenue=total_revenue,
        platform_fees=platform_fees,
        monthly_labels=months_12,
        monthly_values=revenue_values,
        user_labels=user_months,
        user_values=user_values,
        recent_users=recent_users,
        recent_orders=recent_orders,
        pending_deposits=len(pending_deposits),
        pending_withdrawals=len(pending_withdrawals),
    )


# ── Users ─────────────────────────────────────────────────────

@admin_bp.route("/users")
@admin_required
def users():
    search = request.args.get("q", "").strip().lower()
    role   = request.args.get("role", "")
    page   = int(request.args.get("page", 1))

    filters = {}
    if role:
        filters["role"] = role

    all_users = db_select("users", "id,username,email,role,is_verified,is_banned,balance,created_at,last_login",
                          filters=filters, order="-created_at")
    if search:
        all_users = [u for u in all_users
                     if search in (u.get("username") or "").lower()
                     or search in (u.get("email") or "").lower()]

    per_page  = 30
    total     = len(all_users)
    start     = (page - 1) * per_page
    paginated = all_users[start: start + per_page]
    pages     = max(1, -(-total // per_page))

    return render_template("admin/users.html",
        users=paginated, search=search, role=role,
        page=page, pages=pages, total=total)


@admin_bp.route("/users/<user_id>")
@admin_required
def user_detail(user_id):
    user    = db_select("users", "*", filters={"id": user_id}, single=True)
    if not user:
        flash("User not found.", "danger")
        return redirect(url_for("admin.users"))
    profile = db_select("user_profiles", "*", filters={"user_id": user_id}, single=True)
    orders  = db_select("orders", "*", filters={"buyer_id": user_id}, order="-created_at", limit=10)
    tx      = db_select("wallet_transactions", "*", filters={"user_id": user_id},
                        order="-created_at", limit=20)
    listings = db_select("listings", "id,title,status,sales_count,price,created_at",
                         filters={"seller_id": user_id}, order="-created_at")
    return render_template("admin/user_detail.html",
        user=user, profile=profile, orders=orders,
        transactions=tx, listings=listings)


@admin_bp.route("/users/<user_id>/ban", methods=["POST"])
@admin_required
def ban_user(user_id):
    reason = request.form.get("reason", "Policy violation").strip()
    db_update("users", {"is_banned": True, "suspend_reason": reason}, {"id": user_id})
    log_audit(session["user_id"], "ban_user", resource_id=user_id, details={"reason": reason})
    flash("User banned.", "success")
    return redirect(url_for("admin.user_detail", user_id=user_id))


@admin_bp.route("/users/<user_id>/unban", methods=["POST"])
@admin_required
def unban_user(user_id):
    db_update("users", {"is_banned": False, "suspend_reason": None}, {"id": user_id})
    log_audit(session["user_id"], "unban_user", resource_id=user_id)
    flash("User unbanned.", "success")
    return redirect(url_for("admin.user_detail", user_id=user_id))


@admin_bp.route("/users/<user_id>/set-role", methods=["POST"])
@super_admin_required
def set_role(user_id):
    new_role = request.form.get("role", "buyer")
    if new_role not in ("buyer", "seller", "moderator", "admin"):
        flash("Invalid role.", "danger")
        return redirect(url_for("admin.user_detail", user_id=user_id))
    db_update("users", {"role": new_role}, {"id": user_id})
    log_audit(session["user_id"], "set_role", resource_id=user_id, details={"role": new_role})
    flash(f"Role updated to {new_role}.", "success")
    return redirect(url_for("admin.user_detail", user_id=user_id))


@admin_bp.route("/users/<user_id>/verify-seller", methods=["POST"])
@admin_required
def verify_seller(user_id):
    db_update("user_profiles", {"seller_verified": True}, {"user_id": user_id})
    db_update("users", {"role": "seller"}, {"id": user_id})
    db_insert("notifications", {
        "user_id": user_id, "type": "verified", "icon": "shield",
        "title": "Seller Account Verified! ✅",
        "message": "Your seller account has been verified. You can now list products.",
        "link": "/seller/dashboard",
    })
    log_audit(session["user_id"], "verify_seller", resource_id=user_id)
    flash("Seller verified.", "success")
    return redirect(url_for("admin.user_detail", user_id=user_id))


# ── Listings ──────────────────────────────────────────────────

@admin_bp.route("/listings")
@admin_required
def listings():
    status = request.args.get("status", "pending")
    search = request.args.get("q", "").strip().lower()
    page   = int(request.args.get("page", 1))

    filters = {}
    if status:
        filters["status"] = status

    all_listings = db_select(
        "listings",
        "id,title,seller_id,status,price,sales_count,is_featured,is_approved,created_at,category_id",
        filters=filters, order="-created_at"
    )
    if search:
        all_listings = [l for l in all_listings
                        if search in (l.get("title") or "").lower()]

    # Enrich with seller info
    for l in all_listings:
        seller = db_select("users", "id,username", filters={"id": l["seller_id"]}, single=True)
        l["seller"] = seller

    per_page  = 25
    total     = len(all_listings)
    start     = (page - 1) * per_page
    paginated = all_listings[start: start + per_page]
    pages     = max(1, -(-total // per_page))

    return render_template("admin/listings.html",
        listings=paginated, status=status, search=search,
        page=page, pages=pages, total=total)


@admin_bp.route("/listings/<listing_id>/approve", methods=["POST"])
@admin_required
def approve_listing(listing_id):
    listing = db_select("listings", "title,seller_id",
                        filters={"id": listing_id}, single=True)
    if not listing:
        flash("Listing not found.", "danger")
        return redirect(url_for("admin.listings"))

    db_update("listings", {"is_approved": True, "status": "active"}, {"id": listing_id})

    seller  = db_select("users", "email,username", filters={"id": listing["seller_id"]}, single=True)
    if seller:
        send_listing_status(seller["email"], seller["username"], listing["title"], "approved")

    db_insert("notifications", {
        "user_id": listing["seller_id"], "type": "listing_approved", "icon": "check-circle",
        "title": "Listing Approved! ✅",
        "message": f'"{listing["title"]}" is now live on the marketplace.',
        "link": "/seller/inventory",
    })
    log_audit(session["user_id"], "approve_listing", resource_id=listing_id)
    flash("Listing approved and published.", "success")
    return redirect(url_for("admin.listings", status="pending"))


@admin_bp.route("/listings/<listing_id>/reject", methods=["POST"])
@admin_required
def reject_listing(listing_id):
    reason  = request.form.get("reason", "").strip()
    listing = db_select("listings", "title,seller_id",
                        filters={"id": listing_id}, single=True)
    if not listing:
        flash("Listing not found.", "danger")
        return redirect(url_for("admin.listings"))

    db_update("listings", {
        "status": "rejected", "is_approved": False, "reject_reason": reason
    }, {"id": listing_id})

    seller = db_select("users", "email,username", filters={"id": listing["seller_id"]}, single=True)
    if seller:
        send_listing_status(seller["email"], seller["username"],
                            listing["title"], "rejected", reason)

    db_insert("notifications", {
        "user_id": listing["seller_id"], "type": "listing_rejected", "icon": "x-circle",
        "title": "Listing Rejected ❌",
        "message": f'"{listing["title"]}" was rejected. {reason}',
        "link": "/seller/inventory",
    })
    log_audit(session["user_id"], "reject_listing", resource_id=listing_id,
              details={"reason": reason})
    flash("Listing rejected.", "warning")
    return redirect(url_for("admin.listings", status="pending"))


@admin_bp.route("/listings/<listing_id>/feature", methods=["POST"])
@admin_required
def feature_listing(listing_id):
    listing = db_select("listings", "is_featured", filters={"id": listing_id}, single=True)
    if not listing:
        flash("Not found.", "danger")
        return redirect(url_for("admin.listings"))
    new_val = not listing.get("is_featured", False)
    db_update("listings", {"is_featured": new_val}, {"id": listing_id})
    msg = "Listing featured." if new_val else "Listing unfeatured."
    flash(msg, "success")
    return redirect(request.referrer or url_for("admin.listings"))


@admin_bp.route("/listings/<listing_id>/delete", methods=["POST"])
@admin_required
def delete_listing(listing_id):
    db_update("listings", {
        "status": "deleted",
        "deleted_at": datetime.now(timezone.utc).isoformat(),
    }, {"id": listing_id})
    log_audit(session["user_id"], "admin_delete_listing", resource_id=listing_id)
    flash("Listing deleted.", "success")
    return redirect(url_for("admin.listings"))


# ── Orders ────────────────────────────────────────────────────

@admin_bp.route("/orders")
@admin_required
def orders():
    status = request.args.get("status", "")
    search = request.args.get("q", "").strip().lower()
    page   = int(request.args.get("page", 1))

    filters = {}
    if status:
        filters["status"] = status

    all_orders = db_select("orders", "*", filters=filters, order="-created_at")

    if search:
        all_orders = [o for o in all_orders
                      if search in (o.get("order_number") or "").lower()]

    for o in all_orders:
        buyer  = db_select("users", "id,username", filters={"id": o["buyer_id"]}, single=True)
        seller = db_select("users", "id,username", filters={"id": o["seller_id"]}, single=True)
        o["buyer"]  = buyer
        o["seller"] = seller

    per_page  = 25
    total     = len(all_orders)
    start     = (page - 1) * per_page
    paginated = all_orders[start: start + per_page]
    pages     = max(1, -(-total // per_page))

    return render_template("admin/orders.html",
        orders=paginated, status=status, search=search,
        page=page, pages=pages, total=total)


@admin_bp.route("/orders/<order_id>/refund", methods=["POST"])
@admin_required
def refund_order(order_id):
    amount = request.form.get("amount", "")
    reason = request.form.get("reason", "Admin refund").strip()

    order = db_select("orders", "*", filters={"id": order_id}, single=True)
    if not order:
        flash("Order not found.", "danger")
        return redirect(url_for("admin.orders"))

    try:
        amount = float(amount)
        assert 0 < amount <= float(order["total"])
    except (ValueError, AssertionError):
        flash("Invalid refund amount.", "danger")
        return redirect(url_for("admin.orders"))

    buyer = db_select("users", "id,balance,email,username",
                      filters={"id": order["buyer_id"]}, single=True)
    bal_before = float(buyer["balance"])
    bal_after  = bal_before + amount
    db_update("users", {"balance": bal_after}, {"id": buyer["id"]})
    db_insert("wallet_transactions", {
        "user_id":        buyer["id"],
        "type":           "refund",
        "amount":         amount,
        "balance_before": bal_before,
        "balance_after":  bal_after,
        "reference":      generate_reference("REF"),
        "status":         "completed",
        "description":    f"Refund — {order['order_number']}: {reason}",
        "order_id":       order_id,
    })
    db_update("orders", {
        "status":        "refunded",
        "refund_amount": amount,
        "refund_reason": reason,
    }, {"id": order_id})
    db_insert("notifications", {
        "user_id": buyer["id"], "type": "refund", "icon": "refresh-ccw",
        "title": f"Refund Issued: ${amount:.2f}",
        "message": f"A ${amount:.2f} refund for order {order['order_number']} has been added to your wallet.",
        "link": "/dashboard/wallet",
    })
    log_audit(session["user_id"], "refund_order", resource_id=order_id,
              details={"amount": amount, "reason": reason})
    flash(f"Refund of ${amount:.2f} issued.", "success")
    return redirect(url_for("admin.orders"))


# ── Wallet ────────────────────────────────────────────────────

@admin_bp.route("/wallet")
@admin_required
def wallet():
    type_filter = request.args.get("type", "")
    status_f    = request.args.get("status", "pending")
    page        = int(request.args.get("page", 1))

    filters = {}
    if type_filter:
        filters["type"] = type_filter
    if status_f:
        filters["status"] = status_f

    txs = db_select("wallet_transactions", "*", filters=filters, order="-created_at")
    for tx in txs:
        u = db_select("users", "id,username,email", filters={"id": tx["user_id"]}, single=True)
        tx["user"] = u

    per_page  = 25
    total     = len(txs)
    start     = (page - 1) * per_page
    paginated = txs[start: start + per_page]
    pages     = max(1, -(-total // per_page))

    return render_template("admin/wallet.html",
        transactions=paginated, type_filter=type_filter,
        status_f=status_f, page=page, pages=pages, total=total)


@admin_bp.route("/wallet/<tx_id>/approve", methods=["POST"])
@admin_required
def approve_wallet_tx(tx_id):
    tx = db_select("wallet_transactions", "*", filters={"id": tx_id}, single=True)
    if not tx or tx["status"] != "pending":
        flash("Transaction not found or already processed.", "warning")
        return redirect(url_for("admin.wallet"))

    user = db_select("users", "id,balance,email,username",
                     filters={"id": tx["user_id"]}, single=True)
    bal  = float(user["balance"])

    if tx["type"] == "deposit":
        new_bal = bal + float(tx["amount"])
        db_update("users", {"balance": new_bal}, {"id": user["id"]})
        db_update("wallet_transactions", {
            "status":        "completed",
            "balance_before": bal,
            "balance_after": new_bal,
            "processed_by":  session["user_id"],
        }, {"id": tx_id})
        db_insert("notifications", {
            "user_id": user["id"], "type": "deposit_approved", "icon": "trending-up",
            "title": f"Deposit Approved: ${float(tx['amount']):.2f}",
            "message": "Your wallet has been funded.",
            "link": "/dashboard/wallet",
        })
        from utils.email import send_deposit_confirmation
        send_deposit_confirmation(user["email"], user["username"],
                                  float(tx["amount"]), tx.get("reference", ""))

    elif tx["type"] == "withdrawal":
        if float(tx["amount"]) > bal:
            flash("Insufficient user balance to approve withdrawal.", "danger")
            return redirect(url_for("admin.wallet"))
        new_bal = bal - float(tx["amount"])
        db_update("users", {"balance": new_bal}, {"id": user["id"]})
        db_update("wallet_transactions", {
            "status":        "completed",
            "balance_before": bal,
            "balance_after": new_bal,
            "processed_by":  session["user_id"],
        }, {"id": tx_id})
        db_insert("notifications", {
            "user_id": user["id"], "type": "withdrawal_approved", "icon": "trending-down",
            "title": f"Withdrawal Approved: ${float(tx['amount']):.2f}",
            "message": "Your withdrawal is being processed.",
            "link": "/dashboard/wallet",
        })
        send_withdrawal_processed(user["email"], user["username"],
                                  float(tx["amount"]), "approved")

    log_audit(session["user_id"], f"approve_{tx['type']}", resource_id=tx_id)
    flash("Transaction approved.", "success")
    return redirect(url_for("admin.wallet"))


@admin_bp.route("/wallet/<tx_id>/reject", methods=["POST"])
@admin_required
def reject_wallet_tx(tx_id):
    note = request.form.get("note", "").strip()
    tx   = db_select("wallet_transactions", "*", filters={"id": tx_id}, single=True)
    if not tx or tx["status"] != "pending":
        flash("Transaction not found or already processed.", "warning")
        return redirect(url_for("admin.wallet"))

    db_update("wallet_transactions", {
        "status":      "cancelled",
        "admin_note":  note,
        "processed_by": session["user_id"],
    }, {"id": tx_id})

    user = db_select("users", "email,username", filters={"id": tx["user_id"]}, single=True)
    if user and tx["type"] == "withdrawal":
        send_withdrawal_processed(user["email"], user["username"],
                                  float(tx["amount"]), "rejected", note)
    db_insert("notifications", {
        "user_id": tx["user_id"], "type": "tx_rejected", "icon": "x-circle",
        "title": f"{tx['type'].title()} Rejected",
        "message": note or "Your request was not approved. Contact support.",
        "link": "/dashboard/wallet",
    })
    log_audit(session["user_id"], f"reject_{tx['type']}", resource_id=tx_id,
              details={"note": note})
    flash("Transaction rejected.", "warning")
    return redirect(url_for("admin.wallet"))


# ── Categories ────────────────────────────────────────────────

@admin_bp.route("/categories", methods=["GET", "POST"])
@admin_required
def categories():
    if request.method == "POST":
        name  = request.form.get("name", "").strip()
        desc  = request.form.get("description", "").strip()
        icon  = request.form.get("icon", "package").strip()
        color = request.form.get("color", "#7C3AED").strip()
        if name:
            slug = make_slug(name, suffix=False)
            db_insert("categories", {
                "name": name, "slug": slug,
                "description": desc, "icon": icon, "color": color,
            })
            flash(f"Category "{name}" created.", "success")
        return redirect(url_for("admin.categories"))

    cats = db_select("categories", "*", order="sort_order")
    return render_template("admin/categories.html", categories=cats)


@admin_bp.route("/categories/<cat_id>/delete", methods=["POST"])
@admin_required
def delete_category(cat_id):
    db_delete("categories", {"id": cat_id})
    flash("Category deleted.", "success")
    return redirect(url_for("admin.categories"))


# ── Coupons ───────────────────────────────────────────────────

@admin_bp.route("/coupons", methods=["GET", "POST"])
@admin_required
def coupons():
    if request.method == "POST":
        code      = request.form.get("code", "").strip().upper()
        type_     = request.form.get("type", "percentage")
        value     = request.form.get("value", "10")
        min_order = request.form.get("min_order", "0")
        max_disc  = request.form.get("max_discount", "")
        max_uses  = request.form.get("max_uses", "")
        expires   = request.form.get("expires_at", "")
        desc      = request.form.get("description", "").strip()

        if not code:
            flash("Coupon code is required.", "danger")
        else:
            db_insert("coupons", {
                "code":        code,
                "type":        type_,
                "value":       float(value),
                "min_order":   float(min_order) if min_order else 0,
                "max_discount": float(max_disc) if max_disc else None,
                "max_uses":    int(max_uses) if max_uses else None,
                "expires_at":  expires or None,
                "description": desc,
                "is_active":   True,
            })
            flash(f"Coupon "{code}" created.", "success")
        return redirect(url_for("admin.coupons"))

    all_coupons = db_select("coupons", "*", order="-created_at")
    return render_template("admin/coupons.html", coupons=all_coupons)


@admin_bp.route("/coupons/<coupon_id>/toggle", methods=["POST"])
@admin_required
def toggle_coupon(coupon_id):
    coupon = db_select("coupons", "is_active", filters={"id": coupon_id}, single=True)
    if coupon:
        db_update("coupons", {"is_active": not coupon["is_active"]}, {"id": coupon_id})
    return redirect(url_for("admin.coupons"))


# ── Reports ───────────────────────────────────────────────────

@admin_bp.route("/reports")
@admin_required
def reports():
    status = request.args.get("status", "pending")
    rpts   = db_select("listing_reports", "*",
                       filters={"status": status} if status else {},
                       order="-created_at")
    for r in rpts:
        reporter = db_select("users", "id,username", filters={"id": r["reporter_id"]}, single=True)
        listing  = db_select("listings", "id,title,slug", filters={"id": r["listing_id"]}, single=True)
        r["reporter"] = reporter
        r["listing"]  = listing
    return render_template("admin/reports.html", reports=rpts, status=status)


@admin_bp.route("/reports/<report_id>/action", methods=["POST"])
@admin_required
def action_report(report_id):
    action = request.form.get("action", "dismissed")
    db_update("listing_reports", {
        "status":      action,
        "reviewed_by": session["user_id"],
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
    }, {"id": report_id})
    flash(f"Report {action}.", "success")
    return redirect(url_for("admin.reports"))


# ── Site Settings ─────────────────────────────────────────────

@admin_bp.route("/settings", methods=["GET", "POST"])
@super_admin_required
def settings():
    if request.method == "POST":
        for key, value in request.form.items():
            if key.startswith("_"):
                continue
            db_update("site_settings", {
                "value":      value,
                "updated_by": session["user_id"],
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }, {"key": key})
        flash("Settings saved.", "success")
        return redirect(url_for("admin.settings"))

    raw      = db_select("site_settings", "*", order="key")
    site_cfg = {s["key"]: s for s in raw}
    return render_template("admin/settings.html", settings=site_cfg)


# ── Analytics ─────────────────────────────────────────────────

@admin_bp.route("/analytics")
@admin_required
def analytics():
    orders = db_select("orders", "total,platform_fee,status,created_at")
    completed = [o for o in orders if o["status"] == "completed"]

    # Revenue by month (last 12)
    monthly_rev = {}
    monthly_fee = {}
    for o in completed:
        m = (o.get("created_at") or "")[:7]
        if m:
            monthly_rev[m] = monthly_rev.get(m, 0) + float(o["total"])
            monthly_fee[m] = monthly_fee.get(m, 0) + float(o.get("platform_fee") or 0)
    months = sorted(monthly_rev)[-12:]

    # Category breakdown
    cats = db_select("categories", "id,name,listing_count,slug", filters={"is_active": True})
    cat_names  = [c["name"] for c in cats]
    cat_counts = [c.get("listing_count", 0) for c in cats]

    users = db_select("users", "role,created_at")
    buyers   = sum(1 for u in users if u["role"] == "buyer")
    sellers  = sum(1 for u in users if u["role"] == "seller")

    return render_template("admin/analytics.html",
        months=months,
        rev_values=[monthly_rev.get(m, 0) for m in months],
        fee_values=[monthly_fee.get(m, 0) for m in months],
        cat_names=cat_names,
        cat_counts=cat_counts,
        total_buyers=buyers,
        total_sellers=sellers,
        total_revenue=sum(monthly_rev.values()),
        total_fees=sum(monthly_fee.values()),
    )


# ── Audit Logs ────────────────────────────────────────────────

@admin_bp.route("/logs")
@admin_required
def logs():
    page     = int(request.args.get("page", 1))
    action_f = request.args.get("action", "")
    all_logs = db_select("audit_logs", "*", order="-created_at")
    if action_f:
        all_logs = [l for l in all_logs if action_f in (l.get("action") or "")]

    for l in all_logs:
        if l.get("user_id"):
            u = db_select("users", "id,username", filters={"id": l["user_id"]}, single=True)
            l["user"] = u

    per_page  = 50
    total     = len(all_logs)
    start     = (page - 1) * per_page
    paginated = all_logs[start: start + per_page]
    pages     = max(1, -(-total // per_page))

    return render_template("admin/logs.html",
        logs=paginated, action_f=action_f,
        page=page, pages=pages, total=total)


# ── Support Tickets ───────────────────────────────────────────

@admin_bp.route("/support")
@admin_required
def support():
    status = request.args.get("status", "open")
    tickets = db_select("support_tickets", "*",
                        filters={"status": status} if status else {},
                        order="-created_at")
    for t in tickets:
        u = db_select("users", "id,username,email", filters={"id": t["user_id"]}, single=True)
        t["user"] = u
    return render_template("admin/support.html", tickets=tickets, status=status)
