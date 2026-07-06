from flask import (Blueprint, render_template, redirect, url_for,
                   request, session, flash, current_app, jsonify)
from datetime import datetime, timezone, timedelta
from utils.supabase_client import (db_select, db_insert, db_update,
                                   db_delete, storage_upload)
from utils.decorators import login_required, seller_required, verified_required
from utils.helpers import (make_slug, sanitize_html, allowed_image, allowed_file,
                           safe_filename, calc_platform_fee, log_audit, generate_reference)
from utils.email import send_listing_status, send_sale_notification

seller_bp = Blueprint("seller", __name__)

def _seller_id():
    return session.get("user_id")


# ── Become Seller (redirect) ──────────────────────────────────

@seller_bp.route("/become-seller", methods=["GET"])
@login_required
def become_seller():
    if session.get("role") in ("seller", "admin"):
        return redirect(url_for("seller.dashboard"))
    return render_template("seller/become_seller.html")


# ── Seller Dashboard ──────────────────────────────────────────

@seller_bp.route("/dashboard")
@seller_required
def dashboard():
    sid = _seller_id()

    # Quick stats
    listings   = db_select("listings", "id,status,sales_count,views",
                           filters={"seller_id": sid})
    active     = [l for l in listings if l["status"] == "active"]
    pending_ap = [l for l in listings if l["status"] == "pending"]

    orders_all = db_select("orders", "id,status,total,created_at",
                           filters={"seller_id": sid}, order="-created_at")
    pending_orders = [o for o in orders_all if o["status"] in ("pending", "processing")]
    completed      = [o for o in orders_all if o["status"] == "completed"]

    total_revenue = sum(float(o["total"]) for o in completed)
    total_sales   = len(completed)
    total_views   = sum(int(l.get("views") or 0) for l in listings)

    # Monthly revenue (last 6 months)
    monthly = {}
    for o in completed:
        dt = o.get("created_at", "")[:7]   # YYYY-MM
        if dt:
            monthly[dt] = monthly.get(dt, 0) + float(o["total"])
    monthly_labels = sorted(monthly)[-6:]
    monthly_values = [monthly.get(m, 0) for m in monthly_labels]

    # Recent orders
    recent_orders = orders_all[:10]

    # Pending manual deliveries
    manual_pending = []
    for o in pending_orders:
        items = db_select("order_items", "*",
                          filters={"order_id": o["id"], "delivery_status": "pending"})
        if items:
            manual_pending.append({"order": o, "items": items})

    user    = db_select("users", "id,username,balance", filters={"id": sid}, single=True)
    profile = db_select("user_profiles", "*", filters={"user_id": sid}, single=True)

    return render_template("seller/dashboard.html",
        user=user, profile=profile,
        total_listings=len(listings),
        active_listings=len(active),
        pending_approval=len(pending_ap),
        pending_orders=len(pending_orders),
        total_revenue=total_revenue,
        total_sales=total_sales,
        total_views=total_views,
        recent_orders=recent_orders,
        manual_pending=manual_pending,
        monthly_labels=monthly_labels,
        monthly_values=monthly_values,
    )


# ── Create Listing ────────────────────────────────────────────

@seller_bp.route("/create", methods=["GET", "POST"])
@seller_required
@verified_required
def create_listing():
    categories = db_select("categories", filters={"is_active": True}, order="sort_order")

    if request.method == "POST":
        sid   = _seller_id()
        title = request.form.get("title", "").strip()
        cat   = request.form.get("category_id", "")
        desc  = sanitize_html(request.form.get("description", ""), strip=False)
        s_desc = request.form.get("short_description", "").strip()[:500]
        price  = request.form.get("price", "0")
        comp_price = request.form.get("compare_price", "")
        license_t  = request.form.get("license_type", "personal")
        version    = request.form.get("version", "1.0").strip()[:50]
        demo_url   = request.form.get("demo_url", "").strip()[:500]
        docs_url   = request.form.get("documentation_url", "").strip()[:500]
        support    = request.form.get("support_included") == "on"
        sup_days   = request.form.get("support_duration_days", "")
        updates    = request.form.get("updates_included") == "on"
        delivery_t = request.form.get("delivery_type", "instant")
        tags_raw   = request.form.get("tags", "")
        formats    = request.form.getlist("file_format")

        # Validation
        if not title:
            flash("Product title is required.", "danger")
            return render_template("seller/create_listing.html", categories=categories, form=request.form)
        try:
            price = float(price)
            assert price >= 0
        except (ValueError, AssertionError):
            flash("Enter a valid price.", "danger")
            return render_template("seller/create_listing.html", categories=categories, form=request.form)

        slug = make_slug(title)
        tags = [t.strip().lower() for t in tags_raw.split(",") if t.strip()]

        listing_data = {
            "seller_id":     sid,
            "category_id":   cat or None,
            "title":         title,
            "slug":          slug,
            "description":   desc,
            "short_description": s_desc,
            "price":         price,
            "compare_price": float(comp_price) if comp_price else None,
            "license_type":  license_t,
            "version":       version,
            "demo_url":      demo_url or None,
            "documentation_url": docs_url or None,
            "support_included":  support,
            "support_duration_days": int(sup_days) if sup_days else None,
            "updates_included": updates,
            "delivery_type": delivery_t,
            "tags":          tags,
            "file_format":   formats,
            "status":        "pending",
            "is_approved":   False,
        }

        listing = db_insert("listings", listing_data)
        if not listing:
            flash("Failed to create listing. Please try again.", "danger")
            return render_template("seller/create_listing.html", categories=categories, form=request.form)

        lid = listing["id"]

        # Handle product image uploads
        bucket = current_app.config["SUPABASE_BUCKET"]
        preview_urls = []
        for i, f in enumerate(request.files.getlist("images")):
            if f and f.filename and allowed_image(f.filename):
                ext  = f.filename.rsplit(".", 1)[-1].lower()
                path = f"listings/{lid}/img_{i}.{ext}"
                url  = storage_upload(bucket, path, f.read(), f"image/{ext}")
                if url:
                    preview_urls.append(url)
                    db_insert("listing_images", {
                        "listing_id": lid, "url": url,
                        "is_primary": i == 0, "sort_order": i,
                    })

        if preview_urls:
            db_update("listings", {"preview_images": preview_urls}, {"id": lid})

        # Handle digital file upload
        product_file = request.files.get("product_file")
        if product_file and product_file.filename and allowed_file(product_file.filename):
            ext  = product_file.filename.rsplit(".", 1)[-1].lower()
            fn   = safe_filename(product_file.filename)
            path = f"products/{lid}/v{version}_{fn}"
            url  = storage_upload(bucket, path, product_file.read(),
                                  "application/octet-stream")
            if url:
                file_bytes = product_file.seek(0, 2)
                db_insert("listing_files", {
                    "listing_id": lid,
                    "version":    version,
                    "filename":   fn,
                    "file_url":   path,   # Supabase Storage path for signed URLs
                })
                db_update("listings", {"download_url": path}, {"id": lid})

        log_audit(sid, "create_listing", resource_type="listing", resource_id=lid,
                  details={"title": title})
        flash("Listing submitted for review! We'll notify you once approved.", "success")
        return redirect(url_for("seller.inventory"))

    return render_template("seller/create_listing.html",
                           categories=categories, form={})


# ── Edit Listing ──────────────────────────────────────────────

@seller_bp.route("/edit/<listing_id>", methods=["GET", "POST"])
@seller_required
def edit_listing(listing_id):
    sid     = _seller_id()
    listing = db_select("listings", "*", filters={"id": listing_id, "seller_id": sid}, single=True)
    if not listing:
        flash("Listing not found.", "danger")
        return redirect(url_for("seller.inventory"))

    categories = db_select("categories", filters={"is_active": True}, order="sort_order")
    images     = db_select("listing_images", "*", filters={"listing_id": listing_id},
                           order="sort_order")

    if request.method == "POST":
        title   = request.form.get("title", "").strip()
        desc    = sanitize_html(request.form.get("description", ""), strip=False)
        s_desc  = request.form.get("short_description", "").strip()[:500]
        price   = request.form.get("price", "0")
        cat     = request.form.get("category_id", "")
        comp    = request.form.get("compare_price", "")
        license_t = request.form.get("license_type", "personal")
        version = request.form.get("version", "1.0").strip()
        demo_url = request.form.get("demo_url", "").strip()
        docs_url = request.form.get("documentation_url", "").strip()
        support  = request.form.get("support_included") == "on"
        sup_days = request.form.get("support_duration_days", "")
        updates  = request.form.get("updates_included") == "on"
        tags_raw = request.form.get("tags", "")
        formats  = request.form.getlist("file_format")

        if not title:
            flash("Title is required.", "danger")
            return render_template("seller/edit_listing.html",
                                   listing=listing, categories=categories, images=images)
        try:
            price = float(price)
        except ValueError:
            flash("Enter a valid price.", "danger")
            return render_template("seller/edit_listing.html",
                                   listing=listing, categories=categories, images=images)

        tags = [t.strip().lower() for t in tags_raw.split(",") if t.strip()]

        db_update("listings", {
            "title":        title,
            "category_id":  cat or None,
            "description":  desc,
            "short_description": s_desc,
            "price":        price,
            "compare_price": float(comp) if comp else None,
            "license_type": license_t,
            "version":      version,
            "demo_url":     demo_url or None,
            "documentation_url": docs_url or None,
            "support_included": support,
            "support_duration_days": int(sup_days) if sup_days else None,
            "updates_included": updates,
            "tags":         tags,
            "file_format":  formats,
            "status":       "pending",   # Re-submit for review on edits
            "is_approved":  False,
        }, {"id": listing_id, "seller_id": sid})

        # Handle new image uploads
        bucket = current_app.config["SUPABASE_BUCKET"]
        for i, f in enumerate(request.files.getlist("new_images")):
            if f and f.filename and allowed_image(f.filename):
                ext  = f.filename.rsplit(".", 1)[-1].lower()
                path = f"listings/{listing_id}/img_{i}_{int(datetime.now().timestamp())}.{ext}"
                url  = storage_upload(bucket, path, f.read(), f"image/{ext}")
                if url:
                    db_insert("listing_images", {
                        "listing_id": listing_id, "url": url, "sort_order": 99,
                    })

        # Handle new file upload
        new_file = request.files.get("product_file")
        if new_file and new_file.filename and allowed_file(new_file.filename):
            fn   = safe_filename(new_file.filename)
            path = f"products/{listing_id}/v{version}_{fn}"
            storage_upload(bucket, path, new_file.read(), "application/octet-stream")
            db_insert("listing_files", {
                "listing_id": listing_id, "version": version,
                "filename": fn, "file_url": path,
            })
            db_update("listings", {"download_url": path}, {"id": listing_id})

        log_audit(sid, "edit_listing", resource_type="listing", resource_id=listing_id)
        flash("Listing updated and resubmitted for review.", "success")
        return redirect(url_for("seller.inventory"))

    return render_template("seller/edit_listing.html",
                           listing=listing, categories=categories, images=images)


# ── Delete / Pause / Activate ─────────────────────────────────

@seller_bp.route("/delete/<listing_id>", methods=["POST"])
@seller_required
def delete_listing(listing_id):
    sid = _seller_id()
    db_update("listings", {
        "status": "deleted",
        "deleted_at": datetime.now(timezone.utc).isoformat(),
    }, {"id": listing_id, "seller_id": sid})
    log_audit(sid, "delete_listing", resource_type="listing", resource_id=listing_id)
    flash("Listing deleted.", "success")
    return redirect(url_for("seller.inventory"))


@seller_bp.route("/pause/<listing_id>", methods=["POST"])
@seller_required
def pause_listing(listing_id):
    sid = _seller_id()
    db_update("listings", {"status": "paused"}, {"id": listing_id, "seller_id": sid})
    flash("Listing paused.", "info")
    return redirect(url_for("seller.inventory"))


@seller_bp.route("/activate/<listing_id>", methods=["POST"])
@seller_required
def activate_listing(listing_id):
    sid     = _seller_id()
    listing = db_select("listings", "is_approved",
                        filters={"id": listing_id, "seller_id": sid}, single=True)
    if not listing or not listing.get("is_approved"):
        flash("Listing must be approved before activation.", "warning")
    else:
        db_update("listings", {"status": "active"}, {"id": listing_id, "seller_id": sid})
        flash("Listing is now active.", "success")
    return redirect(url_for("seller.inventory"))


# ── Inventory ─────────────────────────────────────────────────

@seller_bp.route("/inventory")
@seller_required
def inventory():
    sid    = _seller_id()
    status = request.args.get("status", "")
    search = request.args.get("q", "").strip().lower()
    page   = int(request.args.get("page", 1))

    filters = {"seller_id": sid}
    if status:
        filters["status"] = status

    listings = db_select("listings", "*", filters=filters, order="-created_at")

    if search:
        listings = [l for l in listings if search in (l.get("title") or "").lower()]

    per_page  = 20
    total     = len(listings)
    start     = (page - 1) * per_page
    paginated = listings[start: start + per_page]
    pages     = max(1, -(-total // per_page))

    return render_template("seller/inventory.html",
        listings=paginated, status=status,
        search=search, page=page, pages=pages, total=total)


# ── Orders (Incoming) ─────────────────────────────────────────

@seller_bp.route("/orders")
@seller_required
def orders():
    sid    = _seller_id()
    status = request.args.get("status", "")
    page   = int(request.args.get("page", 1))

    filters = {"seller_id": sid}
    if status:
        filters["status"] = status

    all_orders = db_select("orders", "*", filters=filters, order="-created_at")
    for o in all_orders:
        buyer = db_select("users", "id,username,email",
                          filters={"id": o["buyer_id"]}, single=True)
        o["buyer"] = buyer
        o["items"] = db_select("order_items", "*", filters={"order_id": o["id"]})

    per_page  = 20
    total     = len(all_orders)
    start     = (page - 1) * per_page
    paginated = all_orders[start: start + per_page]
    pages     = max(1, -(-total // per_page))

    return render_template("seller/orders.html",
        orders=paginated, status=status, page=page, pages=pages, total=total)


@seller_bp.route("/orders/<order_id>/deliver", methods=["POST"])
@seller_required
def deliver_order(order_id):
    sid    = _seller_id()
    order  = db_select("orders", "*", filters={"id": order_id, "seller_id": sid}, single=True)
    if not order:
        flash("Order not found.", "danger")
        return redirect(url_for("seller.orders"))

    item_id = request.form.get("item_id", "")
    content = request.form.get("delivery_content", "").strip()

    if not content:
        flash("Delivery content cannot be empty.", "danger")
        return redirect(url_for("seller.orders"))

    db_update("order_items", {
        "delivered_content": content,
        "delivery_status":   "delivered",
        "delivered_at":      datetime.now(timezone.utc).isoformat(),
    }, {"id": item_id, "order_id": order_id})

    # Check if all items delivered
    remaining = db_select("order_items", "id",
                          filters={"order_id": order_id, "delivery_status": "pending"})
    if not remaining:
        db_update("orders", {"status": "completed"}, {"id": order_id})
        # Credit seller
        buyer    = db_select("users", "email,username,balance",
                             filters={"id": order["buyer_id"]}, single=True)
        s_user   = db_select("users", "id,balance", filters={"id": sid}, single=True)
        fee, earnings = calc_platform_fee(float(order["total"]))
        bal_before = float(s_user["balance"])
        bal_after  = bal_before + earnings
        db_update("users", {"balance": bal_after}, {"id": sid})
        db_insert("wallet_transactions", {
            "user_id": sid, "type": "sale", "amount": earnings,
            "balance_before": bal_before, "balance_after": bal_after,
            "reference": generate_reference("SL"), "status": "completed",
            "description": f"Sale — Order {order['order_number']}",
            "order_id": order_id,
        })
        db_insert("notifications", {
            "user_id": order["buyer_id"], "type": "order_delivered",
            "title": "Your order is ready!", "icon": "package",
            "message": f"Order {order['order_number']} has been delivered.",
            "link": "/dashboard/purchases",
        })

    log_audit(sid, "deliver_order", resource_type="order", resource_id=order_id)
    flash("Delivery sent successfully.", "success")
    return redirect(url_for("seller.orders"))


# ── Analytics ─────────────────────────────────────────────────

@seller_bp.route("/analytics")
@seller_required
def analytics():
    sid = _seller_id()

    orders_all = db_select("orders", "id,status,total,created_at",
                           filters={"seller_id": sid})
    completed  = [o for o in orders_all if o["status"] == "completed"]

    # Revenue by month (last 12 months)
    monthly = {}
    for o in completed:
        m = (o.get("created_at") or "")[:7]
        if m:
            monthly[m] = monthly.get(m, 0) + float(o["total"])

    months_12     = sorted(monthly)[-12:]
    revenue_data  = [monthly.get(m, 0) for m in months_12]

    # Top listings by sales
    top_listings = db_select(
        "listings", "id,title,sales_count,views,rating",
        filters={"seller_id": sid}, order="-sales_count", limit=10
    )

    # Conversion: views vs sales
    total_views = sum(int(l.get("views") or 0) for l in
                      db_select("listings", "views", filters={"seller_id": sid}))
    total_sales = len(completed)
    conversion  = round((total_sales / total_views * 100), 2) if total_views > 0 else 0

    # Wallet
    user = db_select("users", "balance", filters={"id": sid}, single=True)

    return render_template("seller/analytics.html",
        monthly_labels=months_12,
        monthly_values=revenue_data,
        top_listings=top_listings,
        total_orders=len(orders_all),
        completed_orders=total_sales,
        total_views=total_views,
        conversion_rate=conversion,
        total_revenue=sum(revenue_data),
        balance=float((user or {}).get("balance", 0)),
    )


# ── Store Settings ────────────────────────────────────────────

@seller_bp.route("/settings", methods=["GET", "POST"])
@seller_required
def store_settings():
    sid  = _seller_id()
    prof = db_select("user_profiles", "*", filters={"user_id": sid}, single=True)

    if request.method == "POST":
        store_name = request.form.get("store_name", "").strip()[:255]
        store_desc = sanitize_html(request.form.get("store_description", ""), strip=False)
        website    = request.form.get("website", "").strip()[:255]
        twitter    = request.form.get("twitter", "").strip()[:100]
        github     = request.form.get("github", "").strip()[:100]

        from python_slugify import slugify as _slugify
        slug = _slugify(store_name)

        db_update("user_profiles", {
            "store_name":        store_name,
            "store_slug":        slug,
            "store_description": store_desc,
            "website":           website,
            "twitter":           twitter,
            "github":            github,
        }, {"user_id": sid})

        # Banner upload
        banner = request.files.get("banner")
        if banner and banner.filename and allowed_image(banner.filename):
            bucket = current_app.config["SUPABASE_BUCKET"]
            ext    = banner.filename.rsplit(".", 1)[-1].lower()
            path   = f"banners/{sid}.{ext}"
            url    = storage_upload(bucket, path, banner.read(), f"image/{ext}")
            if url:
                db_update("user_profiles", {"store_banner_url": url}, {"user_id": sid})

        flash("Store settings updated.", "success")
        return redirect(url_for("seller.store_settings"))

    return render_template("seller/store_settings.html", profile=prof)
