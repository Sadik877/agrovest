from flask import (Blueprint, render_template, redirect, url_for,
                   request, session, flash, current_app, jsonify)
from datetime import datetime, timezone, timedelta
from utils.supabase_client import (db_select, db_insert, db_update,
                                   db_delete, db_upsert, storage_signed_url)
from utils.decorators import login_required, verified_required
from utils.helpers import (calc_platform_fee, generate_reference,
                           log_audit, fmt_price)
from utils.email import (send_order_confirmation, send_sale_notification,
                         send_deposit_confirmation)

marketplace_bp = Blueprint("marketplace", __name__)


# ── Browse ────────────────────────────────────────────────────

@marketplace_bp.route("/")
def index():
    sort     = request.args.get("sort", "popular")
    cat_slug = request.args.get("category", "")
    page     = int(request.args.get("page", 1))
    per_page = 24

    filters = {"status": "active", "is_approved": True}

    # Resolve category filter
    category = None
    if cat_slug:
        category = db_select("categories", "*", filters={"slug": cat_slug}, single=True)
        if category:
            filters["category_id"] = category["id"]

    order_map = {
        "popular":  "-sales_count",
        "newest":   "-created_at",
        "price_low": "price",
        "price_high": "-price",
        "rating":   "-rating",
    }
    order = order_map.get(sort, "-sales_count")

    all_listings = db_select(
        "listings",
        "id,title,slug,price,compare_price,rating,review_count,sales_count,preview_images,seller_id,category_id,is_featured,tags",
        filters=filters,
        order=order,
    )

    categories  = db_select("categories", "*", filters={"is_active": True}, order="sort_order")
    total       = len(all_listings)
    start       = (page - 1) * per_page
    paginated   = all_listings[start: start + per_page]
    pages       = max(1, -(-total // per_page))

    return render_template("marketplace/index.html",
        listings=paginated, categories=categories,
        current_category=category, sort=sort,
        total=total, page=page, pages=pages,
    )


@marketplace_bp.route("/category/<slug>")
def category(slug):
    return redirect(url_for("marketplace.index", category=slug))


# ── Seller Store ──────────────────────────────────────────────

@marketplace_bp.route("/store/<store_slug>")
def seller_store(store_slug):
    profile = db_select("user_profiles", "*", filters={"store_slug": store_slug}, single=True)
    if not profile:
        flash("Store not found.", "danger")
        return redirect(url_for("marketplace.index"))

    seller = db_select("users", "id,username,created_at",
                       filters={"id": profile["user_id"]}, single=True)
    if not seller:
        flash("Store not found.", "danger")
        return redirect(url_for("marketplace.index"))

    listings = db_select(
        "listings",
        "id,title,slug,price,compare_price,rating,review_count,sales_count,preview_images",
        filters={"seller_id": seller["id"], "status": "active", "is_approved": True},
        order="-sales_count",
    )
    reviews = db_select(
        "reviews", "rating,review_text,created_at,buyer_id",
        filters={"seller_id": seller["id"], "is_hidden": False},
        order="-created_at", limit=10
    )
    avg_rating = (sum(float(r["rating"]) for r in reviews) / len(reviews)) if reviews else 0

    return render_template("marketplace/seller_store.html",
        profile=profile, seller=seller, listings=listings,
        reviews=reviews, avg_rating=round(avg_rating, 1))


# ── Listing Detail ────────────────────────────────────────────

@marketplace_bp.route("/p/<slug>")
def listing_detail(slug):
    listing = db_select("listings", "*", filters={"slug": slug, "status": "active"}, single=True)
    if not listing or not listing.get("is_approved"):
        flash("Product not found.", "danger")
        return redirect(url_for("marketplace.index"))

    # Increment views
    db_update("listings", {"views": (listing.get("views") or 0) + 1}, {"id": listing["id"]})

    # Seller info
    seller  = db_select("users", "id,username", filters={"id": listing["seller_id"]}, single=True)
    profile = db_select("user_profiles", "*", filters={"user_id": listing["seller_id"]}, single=True)

    # Images
    images  = db_select("listing_images", "*", filters={"listing_id": listing["id"]},
                        order="sort_order")

    # Reviews
    reviews = db_select("reviews", "*",
                        filters={"listing_id": listing["id"], "is_hidden": False},
                        order="-created_at", limit=20)
    for r in reviews:
        buyer = db_select("users", "id,username", filters={"id": r["buyer_id"]}, single=True)
        bprof = db_select("user_profiles", "avatar_url", filters={"user_id": r["buyer_id"]}, single=True)
        r["buyer"]  = buyer
        r["avatar"] = bprof.get("avatar_url") if bprof else None

    # Rating breakdown
    rating_counts = {i: sum(1 for r in reviews if r["rating"] == i) for i in range(1, 6)}

    # Category
    cat = None
    if listing.get("category_id"):
        cat = db_select("categories", "*", filters={"id": listing["category_id"]}, single=True)

    # Related listings
    related = []
    if listing.get("category_id"):
        related = db_select(
            "listings",
            "id,title,slug,price,compare_price,rating,preview_images",
            filters={"category_id": listing["category_id"], "status": "active",
                     "is_approved": True},
            order="-sales_count", limit=6
        )
        related = [r for r in related if r["id"] != listing["id"]][:4]

    # Check if user owns it already
    uid             = session.get("user_id")
    already_bought  = False
    in_wishlist     = False
    user_review     = None
    in_cart         = False

    if uid:
        order = db_select(
            "order_items", "id",
            filters={"listing_id": listing["id"]}, single=True
        )
        if order:
            parent = db_select("orders", "buyer_id,status",
                               filters={"id": order.get("order_id")}, single=True)
            if parent and parent["buyer_id"] == uid and parent["status"] == "completed":
                already_bought = True

        wl = db_select("wishlist", "id",
                       filters={"user_id": uid, "listing_id": listing["id"]}, single=True)
        in_wishlist = bool(wl)

        ur = db_select("reviews", "*",
                       filters={"buyer_id": uid, "listing_id": listing["id"]}, single=True)
        user_review = ur

        ci = db_select("cart_items", "id",
                       filters={"user_id": uid, "listing_id": listing["id"]}, single=True)
        in_cart = bool(ci)

        # Log recently viewed
        if uid:
            db_upsert("recently_viewed",
                      {"user_id": uid, "listing_id": listing["id"],
                       "viewed_at": datetime.now(timezone.utc).isoformat()},
                      on_conflict="user_id,listing_id")

    return render_template("marketplace/listing.html",
        listing=listing, seller=seller, profile=profile,
        images=images, reviews=reviews, rating_counts=rating_counts,
        category=cat, related=related,
        already_bought=already_bought, in_wishlist=in_wishlist,
        user_review=user_review, in_cart=in_cart,
    )


@marketplace_bp.route("/p/<listing_id>/review", methods=["POST"])
@login_required
def add_review(listing_id):
    uid     = session["user_id"]
    listing = db_select("listings", "id,slug,seller_id", filters={"id": listing_id}, single=True)
    if not listing:
        flash("Product not found.", "danger")
        return redirect(url_for("marketplace.index"))

    rating  = int(request.form.get("rating", 0))
    text    = request.form.get("review_text", "").strip()[:2000]

    if rating < 1 or rating > 5:
        flash("Please select a rating.", "danger")
        return redirect(url_for("marketplace.listing_detail", slug=listing["slug"]))

    existing = db_select("reviews", "id",
                         filters={"buyer_id": uid, "listing_id": listing_id}, single=True)
    if existing:
        db_update("reviews", {"rating": rating, "review_text": text},
                  {"id": existing["id"]})
        flash("Review updated.", "success")
    else:
        db_insert("reviews", {
            "buyer_id":              uid,
            "seller_id":             listing["seller_id"],
            "listing_id":            listing_id,
            "rating":                rating,
            "review_text":           text,
            "is_verified_purchase":  True,
        })
        flash("Review submitted. Thank you! 🌟", "success")

    return redirect(url_for("marketplace.listing_detail", slug=listing["slug"]))


@marketplace_bp.route("/p/<listing_id>/report", methods=["POST"])
@login_required
def report_listing(listing_id):
    uid    = session["user_id"]
    reason = request.form.get("reason", "").strip()[:100]
    desc   = request.form.get("description", "").strip()[:500]
    db_insert("listing_reports", {
        "reporter_id": uid, "listing_id": listing_id,
        "reason": reason, "description": desc,
    })
    flash("Report submitted. Our team will review it shortly.", "success")
    listing = db_select("listings", "slug", filters={"id": listing_id}, single=True)
    return redirect(url_for("marketplace.listing_detail", slug=listing["slug"]) if listing else
                    url_for("marketplace.index"))


# ── Cart ──────────────────────────────────────────────────────

@marketplace_bp.route("/cart")
@login_required
def cart():
    uid   = session["user_id"]
    items = db_select("cart_items", "*", filters={"user_id": uid}, order="created_at")
    cart_data = []
    subtotal  = 0.0
    for item in items:
        listing = db_select(
            "listings",
            "id,title,slug,price,compare_price,preview_images,seller_id,stock,status,is_approved",
            filters={"id": item["listing_id"]}, single=True
        )
        if listing and listing["status"] == "active" and listing["is_approved"]:
            listing["cart_id"] = item["id"]
            cart_data.append(listing)
            subtotal += float(listing["price"])

    # Applied coupon
    coupon     = session.get("cart_coupon")
    discount   = 0.0
    coupon_obj = None
    if coupon:
        coupon_obj = db_select("coupons", "*", filters={"code": coupon, "is_active": True}, single=True)
        if coupon_obj:
            if coupon_obj["type"] == "percentage":
                d = subtotal * float(coupon_obj["value"]) / 100
                if coupon_obj.get("max_discount"):
                    d = min(d, float(coupon_obj["max_discount"]))
                discount = round(d, 2)
            else:
                discount = min(float(coupon_obj["value"]), subtotal)

    total = max(0, subtotal - discount)
    return render_template("marketplace/cart.html",
        cart_items=cart_data, subtotal=subtotal,
        discount=discount, total=total,
        coupon=coupon, coupon_obj=coupon_obj,
        max_items=current_app.config.get("MAX_CART_ITEMS", 20),
    )


@marketplace_bp.route("/cart/add/<listing_id>", methods=["POST"])
@login_required
def cart_add(listing_id):
    uid = session["user_id"]
    listing = db_select("listings", "id,title,status,is_approved,price",
                        filters={"id": listing_id}, single=True)
    if not listing or listing["status"] != "active" or not listing["is_approved"]:
        flash("This product is not available.", "danger")
        return redirect(request.referrer or url_for("marketplace.index"))

    # Cart limit
    cart_count = len(db_select("cart_items", "id", filters={"user_id": uid}))
    if cart_count >= current_app.config.get("MAX_CART_ITEMS", 20):
        flash("Cart is full. Remove items before adding more.", "warning")
        return redirect(request.referrer or url_for("marketplace.cart"))

    existing = db_select("cart_items", "id",
                         filters={"user_id": uid, "listing_id": listing_id}, single=True)
    if existing:
        flash(f""{listing['title']}" is already in your cart.", "info")
    else:
        db_insert("cart_items", {"user_id": uid, "listing_id": listing_id})
        flash(f""{listing['title']}" added to cart!", "success")

    next_url = request.form.get("next") or request.referrer or url_for("marketplace.cart")
    return redirect(next_url)


@marketplace_bp.route("/cart/remove/<cart_id>", methods=["POST"])
@login_required
def cart_remove(cart_id):
    db_delete("cart_items", {"id": cart_id, "user_id": session["user_id"]})
    flash("Item removed from cart.", "info")
    return redirect(url_for("marketplace.cart"))


@marketplace_bp.route("/cart/clear", methods=["POST"])
@login_required
def cart_clear():
    db_delete("cart_items", {"user_id": session["user_id"]})
    return redirect(url_for("marketplace.cart"))


# ── Coupon ────────────────────────────────────────────────────

@marketplace_bp.route("/cart/coupon", methods=["POST"])
@login_required
def apply_coupon():
    code = request.form.get("code", "").strip().upper()
    uid  = session["user_id"]

    coupon = db_select("coupons", "*", filters={"code": code, "is_active": True}, single=True)
    if not coupon:
        flash("Invalid or expired coupon code.", "danger")
        return redirect(url_for("marketplace.cart"))

    # Check expiry
    if coupon.get("expires_at"):
        exp = datetime.fromisoformat(coupon["expires_at"].replace("Z", "+00:00"))
        if exp < datetime.now(timezone.utc):
            flash("This coupon has expired.", "danger")
            return redirect(url_for("marketplace.cart"))

    # Check usage limit
    if coupon.get("max_uses") and coupon.get("used_count", 0) >= coupon["max_uses"]:
        flash("This coupon has reached its usage limit.", "danger")
        return redirect(url_for("marketplace.cart"))

    # Per user limit
    user_uses = len(db_select("coupon_uses",
                              filters={"coupon_id": coupon["id"], "user_id": uid}))
    per_limit = coupon.get("per_user_limit", 1) or 1
    if user_uses >= per_limit:
        flash("You have already used this coupon.", "danger")
        return redirect(url_for("marketplace.cart"))

    session["cart_coupon"] = code
    flash(f"Coupon "{code}" applied! 🎉", "success")
    return redirect(url_for("marketplace.cart"))


@marketplace_bp.route("/cart/coupon/remove", methods=["POST"])
def remove_coupon():
    session.pop("cart_coupon", None)
    flash("Coupon removed.", "info")
    return redirect(url_for("marketplace.cart"))


# ── Checkout ──────────────────────────────────────────────────

@marketplace_bp.route("/checkout", methods=["GET", "POST"])
@login_required
@verified_required
def checkout():
    uid   = session["user_id"]
    items = db_select("cart_items", "*", filters={"user_id": uid})
    if not items:
        flash("Your cart is empty.", "warning")
        return redirect(url_for("marketplace.cart"))

    cart_listings = []
    subtotal = 0.0
    for item in items:
        listing = db_select(
            "listings",
            "id,title,slug,price,seller_id,delivery_type,download_url",
            filters={"id": item["listing_id"], "status": "active", "is_approved": True},
            single=True
        )
        if listing:
            cart_listings.append(listing)
            subtotal += float(listing["price"])

    if not cart_listings:
        flash("No valid items in cart.", "warning")
        return redirect(url_for("marketplace.cart"))

    # Coupon
    coupon     = session.get("cart_coupon")
    discount   = 0.0
    coupon_obj = None
    if coupon:
        coupon_obj = db_select("coupons", "*", filters={"code": coupon, "is_active": True}, single=True)
        if coupon_obj:
            if coupon_obj["type"] == "percentage":
                d = subtotal * float(coupon_obj["value"]) / 100
                if coupon_obj.get("max_discount"):
                    d = min(d, float(coupon_obj["max_discount"]))
                discount = round(d, 2)
            else:
                discount = min(float(coupon_obj["value"]), subtotal)

    total = max(0, subtotal - discount)
    user  = db_select("users", "id,balance,email,username", filters={"id": uid}, single=True)
    wallet_balance = float(user.get("balance", 0))

    if request.method == "POST":
        payment_method = request.form.get("payment_method", "wallet")
        note           = request.form.get("note", "").strip()[:500]

        if payment_method == "wallet":
            if wallet_balance < total:
                flash("Insufficient wallet balance.", "danger")
                return redirect(url_for("marketplace.checkout"))

            # Group by seller
            sellers = {}
            for listing in cart_listings:
                sid = listing["seller_id"]
                sellers.setdefault(sid, []).append(listing)

            created_orders = []
            for sid, s_listings in sellers.items():
                s_total   = sum(float(l["price"]) for l in s_listings)
                s_discount = discount if len(sellers) == 1 else 0
                s_net     = max(0, s_total - s_discount)
                fee, earnings = calc_platform_fee(s_net)

                order = db_insert("orders", {
                    "buyer_id":      uid,
                    "seller_id":     sid,
                    "status":        "processing",
                    "payment_method": "wallet",
                    "subtotal":      s_total,
                    "discount_amount": s_discount,
                    "coupon_code":   coupon if len(sellers) == 1 else None,
                    "platform_fee":  fee,
                    "seller_earnings": earnings,
                    "total":         s_net,
                    "buyer_note":    note,
                })

                if not order:
                    continue

                created_orders.append(order)

                # Create order items
                expires_at = (datetime.now(timezone.utc) +
                              timedelta(days=current_app.config.get("DOWNLOAD_EXPIRY_DAYS", 7))
                              ).isoformat()
                for listing in s_listings:
                    # For instant delivery, generate signed download URL
                    dl_url = None
                    if listing.get("download_url") and listing["delivery_type"] == "instant":
                        bucket = current_app.config["SUPABASE_BUCKET"]
                        dl_url = storage_signed_url(
                            bucket, listing["download_url"],
                            expires_in=int(current_app.config.get("DOWNLOAD_EXPIRY_DAYS", 7)) * 86400
                        )
                    db_insert("order_items", {
                        "order_id":         order["id"],
                        "listing_id":       listing["id"],
                        "title":            listing["title"],
                        "quantity":         1,
                        "unit_price":       float(listing["price"]),
                        "total_price":      float(listing["price"]),
                        "license_type":     "personal",
                        "download_url":     dl_url,
                        "max_downloads":    current_app.config.get("MAX_DOWNLOADS", 5),
                        "download_expires_at": expires_at,
                        "delivery_status":  ("delivered" if listing["delivery_type"] == "instant"
                                             else "pending"),
                        "delivered_at":     (datetime.now(timezone.utc).isoformat()
                                             if listing["delivery_type"] == "instant" else None),
                    })

                    if listing["delivery_type"] == "instant":
                        db_update("listings",
                                  {"sales_count": (listing.get("sales_count") or 0) + 1},
                                  {"id": listing["id"]})

                # Mark as completed for instant
                all_instant = all(l["delivery_type"] == "instant" for l in s_listings)
                if all_instant:
                    db_update("orders", {"status": "completed"}, {"id": order["id"]})

                # Notify seller
                db_insert("notifications", {
                    "user_id": sid, "type": "new_order", "icon": "shopping-cart",
                    "title": "New Order Received!",
                    "message": f"You have a new order: {order['order_number']}",
                    "link":  "/seller/orders",
                })

            # Deduct from buyer wallet
            bal_before = wallet_balance
            bal_after  = wallet_balance - total
            db_update("users", {"balance": bal_after}, {"id": uid})
            db_insert("wallet_transactions", {
                "user_id":        uid,
                "type":           "purchase",
                "amount":         total,
                "balance_before": bal_before,
                "balance_after":  bal_after,
                "reference":      generate_reference("PUR"),
                "status":         "completed",
                "description":    f"Purchase — {len(cart_listings)} item(s)",
                "order_id":       created_orders[0]["id"] if created_orders else None,
            })

            # Track coupon use
            if coupon_obj and created_orders:
                db_insert("coupon_uses", {
                    "coupon_id":       coupon_obj["id"],
                    "user_id":         uid,
                    "order_id":        created_orders[0]["id"],
                    "discount_amount": discount,
                })
                db_update("coupons",
                          {"used_count": (coupon_obj.get("used_count") or 0) + 1},
                          {"id": coupon_obj["id"]})

            # Clear cart & coupon
            db_delete("cart_items", {"user_id": uid})
            session.pop("cart_coupon", None)
            session["balance"] = bal_after

            # Send confirmation email
            email_items = [{"title": l["title"], "price": float(l["price"]), "qty": 1}
                           for l in cart_listings]
            dashboard_url = url_for("dashboard.purchases", _external=True)
            send_order_confirmation(user["email"], user["username"],
                                    created_orders[0]["order_number"] if created_orders else "—",
                                    email_items, total, dashboard_url)

            first_order = created_orders[0]["order_number"] if created_orders else ""
            flash("Order placed successfully! Your downloads are ready. 🎉", "success")
            return redirect(url_for("marketplace.order_confirm",
                                    order_number=first_order))

        else:
            # External payment — redirect to gateway initiation
            flash("External payment processing not yet configured.", "info")
            return redirect(url_for("marketplace.checkout"))

    cfg = current_app.config
    return render_template("marketplace/checkout.html",
        cart_items=cart_listings,
        subtotal=subtotal, discount=discount, total=total,
        coupon=coupon, coupon_obj=coupon_obj,
        wallet_balance=wallet_balance,
        stripe_pk=cfg.get("STRIPE_PUBLISHABLE_KEY", ""),
        paystack_pk=cfg.get("PAYSTACK_PUBLIC_KEY", ""),
        flw_pk=cfg.get("FLUTTERWAVE_PUBLIC_KEY", ""),
    )


@marketplace_bp.route("/order/<order_number>")
@login_required
def order_confirm(order_number):
    uid   = session["user_id"]
    order = db_select("orders", "*",
                      filters={"order_number": order_number, "buyer_id": uid}, single=True)
    if not order:
        flash("Order not found.", "danger")
        return redirect(url_for("dashboard.purchases"))

    items = db_select("order_items", "*", filters={"order_id": order["id"]})
    return render_template("marketplace/order_confirm.html",
                           order=order, items=items)
