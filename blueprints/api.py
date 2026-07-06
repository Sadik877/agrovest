import hmac, hashlib, json
from flask import Blueprint, request, jsonify, session, current_app
from utils.supabase_client import (db_select, db_insert, db_update,
                                   db_delete, db_upsert)
from utils.decorators import api_login_required
from utils.helpers import generate_reference, calc_platform_fee

api_bp = Blueprint("api", __name__)


# ── Search Autocomplete ───────────────────────────────────────

@api_bp.route("/search/autocomplete")
def autocomplete():
    q = request.args.get("q", "").strip().lower()
    if len(q) < 2:
        return jsonify([])
    listings = db_select(
        "listings",
        "id,title,slug,price,preview_images",
        filters={"status": "active", "is_approved": True},
        limit=50
    )
    results = [
        {"id": l["id"], "title": l["title"], "slug": l["slug"],
         "price": float(l["price"]), "thumb": (l.get("preview_images") or [""])[0]}
        for l in listings
        if q in (l.get("title") or "").lower()
    ][:8]
    return jsonify(results)


# ── Cart Count ────────────────────────────────────────────────

@api_bp.route("/cart/count")
@api_login_required
def cart_count():
    count = len(db_select("cart_items", "id", filters={"user_id": session["user_id"]}))
    return jsonify({"count": count})


# ── Wishlist Toggle ───────────────────────────────────────────

@api_bp.route("/wishlist/toggle", methods=["POST"])
@api_login_required
def wishlist_toggle():
    uid        = session["user_id"]
    listing_id = request.json.get("listing_id") if request.is_json else request.form.get("listing_id")
    if not listing_id:
        return jsonify({"error": "listing_id required"}), 400

    existing = db_select("wishlist", "id",
                         filters={"user_id": uid, "listing_id": listing_id}, single=True)
    if existing:
        db_delete("wishlist", {"id": existing["id"]})
        # Decrement wishlist count
        listing = db_select("listings", "wishlist_count", filters={"id": listing_id}, single=True)
        if listing:
            db_update("listings",
                      {"wishlist_count": max(0, (listing.get("wishlist_count") or 1) - 1)},
                      {"id": listing_id})
        return jsonify({"in_wishlist": False})
    else:
        db_insert("wishlist", {"user_id": uid, "listing_id": listing_id})
        listing = db_select("listings", "wishlist_count", filters={"id": listing_id}, single=True)
        if listing:
            db_update("listings",
                      {"wishlist_count": (listing.get("wishlist_count") or 0) + 1},
                      {"id": listing_id})
        return jsonify({"in_wishlist": True})


# ── Notifications ─────────────────────────────────────────────

@api_bp.route("/notifications/mark-read", methods=["POST"])
@api_login_required
def mark_notification_read():
    nid = request.json.get("id") if request.is_json else request.form.get("id")
    if nid:
        db_update("notifications", {"is_read": True},
                  {"id": nid, "user_id": session["user_id"]})
    return jsonify({"ok": True})


@api_bp.route("/notifications/mark-all-read", methods=["POST"])
@api_login_required
def mark_all_read():
    db_update("notifications", {"is_read": True},
              {"user_id": session["user_id"], "is_read": False})
    return jsonify({"ok": True})


@api_bp.route("/notifications/unread-count")
@api_login_required
def unread_notifications():
    count = len(db_select("notifications", "id",
                          filters={"user_id": session["user_id"], "is_read": False}))
    return jsonify({"count": count})


# ── Messages ──────────────────────────────────────────────────

@api_bp.route("/messages/unread-count")
@api_login_required
def unread_messages():
    uid    = session["user_id"]
    convs1 = db_select("conversations", "unread_count_1",
                       filters={"participant_1": uid})
    convs2 = db_select("conversations", "unread_count_2",
                       filters={"participant_2": uid})
    total = (sum(c.get("unread_count_1", 0) for c in convs1) +
             sum(c.get("unread_count_2", 0) for c in convs2))
    return jsonify({"count": total})


# ── Review Helpful Vote ───────────────────────────────────────

@api_bp.route("/reviews/<review_id>/helpful", methods=["POST"])
@api_login_required
def review_helpful(review_id):
    uid      = session["user_id"]
    existing = db_select("review_votes", "id",
                         filters={"review_id": review_id, "user_id": uid}, single=True)
    if existing:
        return jsonify({"error": "Already voted"}), 400

    db_insert("review_votes", {"review_id": review_id, "user_id": uid})
    review = db_select("reviews", "helpful_votes", filters={"id": review_id}, single=True)
    if review:
        new_count = (review.get("helpful_votes") or 0) + 1
        db_update("reviews", {"helpful_votes": new_count}, {"id": review_id})
        return jsonify({"votes": new_count})
    return jsonify({"error": "Review not found"}), 404


# ── Seller Reply to Review ────────────────────────────────────

@api_bp.route("/reviews/<review_id>/reply", methods=["POST"])
@api_login_required
def seller_reply(review_id):
    from datetime import datetime, timezone
    uid    = session["user_id"]
    text   = (request.json.get("reply") if request.is_json else request.form.get("reply", "")).strip()
    review = db_select("reviews", "seller_id", filters={"id": review_id}, single=True)
    if not review or review["seller_id"] != uid:
        return jsonify({"error": "Unauthorized"}), 403
    db_update("reviews", {
        "seller_reply":      text[:1000],
        "seller_replied_at": datetime.now(timezone.utc).isoformat(),
    }, {"id": review_id})
    return jsonify({"ok": True})


# ── Related Listings ──────────────────────────────────────────

@api_bp.route("/listings/related/<listing_id>")
def related_listings(listing_id):
    listing = db_select("listings", "category_id", filters={"id": listing_id}, single=True)
    if not listing or not listing.get("category_id"):
        return jsonify([])
    related = db_select(
        "listings",
        "id,title,slug,price,compare_price,rating,preview_images",
        filters={"category_id": listing["category_id"],
                 "status": "active", "is_approved": True},
        order="-sales_count", limit=8
    )
    related = [r for r in related if r["id"] != listing_id][:4]
    return jsonify(related)


# ── Wallet Balance ────────────────────────────────────────────

@api_bp.route("/wallet/balance")
@api_login_required
def wallet_balance():
    user = db_select("users", "balance", filters={"id": session["user_id"]}, single=True)
    bal  = float(user.get("balance", 0)) if user else 0
    session["balance"] = bal
    return jsonify({"balance": bal})


# ── Report Listing ────────────────────────────────────────────

@api_bp.route("/listings/<listing_id>/report", methods=["POST"])
@api_login_required
def report_listing(listing_id):
    uid    = session["user_id"]
    data   = request.get_json(silent=True) or request.form
    reason = str(data.get("reason", "")).strip()[:100]
    desc   = str(data.get("description", "")).strip()[:500]
    if not reason:
        return jsonify({"error": "Reason required"}), 400
    db_insert("listing_reports", {
        "reporter_id": uid,
        "listing_id":  listing_id,
        "reason":      reason,
        "description": desc,
    })
    return jsonify({"ok": True})


# ── Payment Webhooks ──────────────────────────────────────────

@api_bp.route("/payment/stripe/webhook", methods=["POST"])
def stripe_webhook():
    import stripe
    payload    = request.get_data()
    sig_header = request.headers.get("Stripe-Signature", "")
    secret     = current_app.config.get("STRIPE_WEBHOOK_SECRET", "")
    if not secret:
        return jsonify({"error": "Not configured"}), 400
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, secret)
    except Exception:
        return jsonify({"error": "Invalid signature"}), 400

    if event["type"] == "payment_intent.succeeded":
        pi   = event["data"]["object"]
        meta = pi.get("metadata", {})
        uid  = meta.get("user_id")
        if uid:
            amount = pi["amount_received"] / 100
            user   = db_select("users", "id,balance", filters={"id": uid}, single=True)
            if user:
                bal_before = float(user["balance"])
                bal_after  = bal_before + amount
                db_update("users", {"balance": bal_after}, {"id": uid})
                db_insert("wallet_transactions", {
                    "user_id": uid, "type": "deposit", "amount": amount,
                    "balance_before": bal_before, "balance_after": bal_after,
                    "reference": pi["id"], "status": "completed",
                    "payment_method": "stripe",
                    "description": "Wallet deposit via Stripe",
                })
    return jsonify({"received": True})


@api_bp.route("/payment/paystack/webhook", methods=["POST"])
def paystack_webhook():
    secret  = current_app.config.get("PAYSTACK_SECRET_KEY", "")
    sig     = request.headers.get("x-paystack-signature", "")
    payload = request.get_data()
    expected = hmac.new(secret.encode(), payload, hashlib.sha512).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return jsonify({"error": "Invalid signature"}), 400

    data  = request.get_json(silent=True) or {}
    event = data.get("event")
    if event == "charge.success":
        meta   = data.get("data", {}).get("metadata", {})
        uid    = meta.get("user_id")
        amount = (data.get("data", {}).get("amount", 0)) / 100
        if uid and amount:
            user = db_select("users", "id,balance", filters={"id": uid}, single=True)
            if user:
                bal_before = float(user["balance"])
                bal_after  = bal_before + amount
                db_update("users", {"balance": bal_after}, {"id": uid})
                db_insert("wallet_transactions", {
                    "user_id": uid, "type": "deposit", "amount": amount,
                    "balance_before": bal_before, "balance_after": bal_after,
                    "reference": data.get("data", {}).get("reference", generate_reference("PSK")),
                    "status": "completed", "payment_method": "paystack",
                    "description": "Wallet deposit via Paystack",
                })
    return jsonify({"status": "ok"})


@api_bp.route("/payment/flutterwave/webhook", methods=["POST"])
def flutterwave_webhook():
    secret = current_app.config.get("FLUTTERWAVE_WEBHOOK_SECRET", "")
    sig    = request.headers.get("verif-hash", "")
    if secret and sig != secret:
        return jsonify({"error": "Invalid signature"}), 400

    data   = request.get_json(silent=True) or {}
    status = data.get("data", {}).get("status", "")
    if status == "successful":
        meta   = data.get("data", {}).get("meta", {})
        uid    = meta.get("user_id")
        amount = float(data.get("data", {}).get("amount", 0))
        if uid and amount:
            user = db_select("users", "id,balance", filters={"id": uid}, single=True)
            if user:
                bal_before = float(user["balance"])
                bal_after  = bal_before + amount
                db_update("users", {"balance": bal_after}, {"id": uid})
                db_insert("wallet_transactions", {
                    "user_id": uid, "type": "deposit", "amount": amount,
                    "balance_before": bal_before, "balance_after": bal_after,
                    "reference": str(data.get("data", {}).get("id", generate_reference("FLW"))),
                    "status": "completed", "payment_method": "flutterwave",
                    "description": "Wallet deposit via Flutterwave",
                })
    return jsonify({"status": "ok"})
