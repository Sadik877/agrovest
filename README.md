# 🌾 AgroVest Pro — Agricultural Investment Platform

A production Flask web app for a Nigerian agricultural investment platform, backed by Supabase (PostgreSQL) and deployed on Render. Full NGN (₦) support, referral commissions, deposit/withdrawal workflows, and an admin back office.

---

## ✨ Features

| Feature | Description |
|---|---|
| 🏠 Landing Page | Hero, stats, plans, testimonials, FAQ, CTA |
| 💼 Investment Plans | Configurable tiers with min/max amount, ROI %, duration |
| 🔐 Authentication | Register, login, logout — hashed passwords, rate-limited |
| 📊 User Dashboard | Balance, investments, deposits, withdrawals, referrals, live notifications |
| 💳 Deposit System | Submit proof of payment, admin approval, optional persistent Supabase Storage |
| 💸 Withdrawal System | Request withdrawal, admin approve/reject with automatic refund |
| 🤝 Referral System | Unique referral links, 5% commission, real-time tracking |
| 🛠️ Admin Panel | Full management of users, plans, deposits, withdrawals, investments |
| 📱 Responsive UI | Mobile-first Tailwind design |
| 🔒 Security | CSRF protection, rate-limited auth, HTTPS-only cookies, atomic balance updates |

---

## 🚀 Deploying to Render (production)

### 1. Create your Supabase project
Go to [supabase.com](https://supabase.com) → New project. Once it's ready:

- **Settings → API** — copy the **Project URL** and the **`service_role` secret key**
  (NOT the `anon`/`public` key — this app needs full read/write access and does
  its own admin checks in Python, bypassing Postgres row-level security).
- **SQL Editor → New query** — paste the entire contents of `supabase_setup.sql`
  and run it once. This creates all tables, indexes, timestamp triggers, and
  the atomic balance-adjustment function the app relies on.

### 2. Deploy to Render
Push this project to a GitHub repo, then on [render.com](https://render.com):
**New → Blueprint**, point it at your repo. `render.yaml` configures the
service automatically. You'll be prompted to fill in two values it can't
generate for you:

| Variable | Where to get it |
|---|---|
| `SUPABASE_URL` | Supabase → Settings → API → Project URL |
| `SUPABASE_KEY` | Supabase → Settings → API → `service_role` key |

`SECRET_KEY` is generated for you automatically. You do **not** need to set
`RENDER=true` yourself — Render sets that automatically on every service.

No Blueprint? Create a Web Service manually: build command
`pip install -r requirements.txt`, start command
`gunicorn app:app --workers 2 --bind 0.0.0.0:$PORT --timeout 120`, and add the
same three environment variables above.

### 3. First login
Visit your Render URL. If anything is misconfigured you'll land on a
**setup page** that tells you exactly which environment variable is missing
or wrong — there's no silent failure. Once configured, log in with:

| Role | Email | Password |
|---|---|---|
| **Admin** | `admin@agrovest.ng` | `Admin@2024!` |

**Change this password immediately** — go to Admin → Users → edit your own
account.

---

## 💻 Running locally

```bash
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # then fill in SUPABASE_URL / SUPABASE_KEY
python run.py
```

Open **http://localhost:5000**. Locally `RENDER` isn't set, so cookies work
over plain HTTP and uploads go to `static/uploads/` instead of `/tmp`.

---

## 📁 Project Structure

```
agrovest/
├── app.py                    # Flask application (routes, auth, business logic)
├── run.py                    # Local dev entrypoint
├── requirements.txt          # Python dependencies
├── supabase_setup.sql        # Run once in Supabase SQL Editor
├── render.yaml                # Render Blueprint (env vars, build/start commands)
├── Procfile                   # Process command (gunicorn)
├── runtime.txt                 # Pinned Python version
├── .env.example                # Local dev env var reference
│
├── templates/
│   ├── base.html, setup.html, index.html, about.html, plans.html, contact.html
│   ├── login.html, register.html
│   ├── dashboard_base.html, dashboard.html, invest.html, deposit.html, withdraw.html
│   ├── partials/        navbar.html, footer.html
│   ├── admin/            base.html, dashboard.html, users.html, user_form.html,
│   │                      plans.html, plan_form.html, deposits.html,
│   │                      withdrawals.html, investments.html
│   └── errors/            403.html, 404.html, 500.html
│
└── static/
    └── uploads/           # Local deposit-proof storage (see note below)
```

---

## ⚙️ Environment Variables

| Variable | Required | Purpose |
|---|---|---|
| `SECRET_KEY` | Yes | Signs session cookies. `render.yaml` generates one automatically. |
| `SUPABASE_URL` | Yes | Your Supabase project's REST API URL. |
| `SUPABASE_KEY` | Yes | The Supabase **service_role** key. |
| `USE_SUPABASE_STORAGE` | No (default `false`) | Persist deposit proofs in Supabase Storage instead of local disk — see below. |
| `SUPABASE_STORAGE_BUCKET` | No (default `deposit-proofs`) | Bucket name if the above is enabled. |
| `RENDER` | Set automatically by Render | Used to detect HTTPS-only cookies and the `/tmp` upload path. Don't set this yourself. |

---

## 📦 A note on uploaded files (important)

Render's web service disk is **ephemeral** — anything written to it
(including `static/uploads/` or `/tmp`) is wiped on every deploy and every
restart. By default this app still works fine for deposits (the proof image
just won't survive a redeploy). For a platform handling real money, that's
a real limitation worth knowing about.

To fix it permanently: uncomment the bucket-creation block at the bottom of
`supabase_setup.sql`, run it once, then set `USE_SUPABASE_STORAGE=true`.
Deposit proofs will then be stored in Supabase Storage and viewed by admins
via short-lived signed URLs. If that upload ever fails for any reason, the
app automatically falls back to local disk so a deposit is never blocked.

---

## 🛡️ Security Features

- PBKDF2 password hashing (via Werkzeug) — never stored in plain text
- CSRF protection on every form (Flask-WTF)
- Rate limiting on login/register (in-memory, per-IP)
- HTTPS-only, `HttpOnly`, `SameSite=Lax` session cookies in production
- Admin routes protected by a dedicated decorator; admins can't suspend or
  demote their own account by accident
- File upload validation (extension allow-list + 5MB size cap)
- **Atomic balance updates** — deposits, withdrawals, investments and admin
  credits all go through a single Postgres function (`agrovest_adjust_balance`)
  that locks the user's row before checking/changing their balance. This is
  what stops a double-clicked submit button (or two open tabs) from spending
  money the account doesn't have, and stops an admin double-clicking
  "Approve" from crediting the same deposit twice.
- Parameterized queries throughout (Supabase client — no raw SQL string
  building, no SQL injection surface)

---

## 💰 Investment Plans

Seeded on first run, fully editable afterwards from **Admin → Plans**:

| Plan | Min | Max | ROI | Duration |
|---|---|---|---|---|
| 🌱 Starter Farm | ₦10,000 | ₦49,999 | 15% | 30 days |
| 🌿 Green Harvest | ₦50,000 | ₦199,999 | 25% | 45 days |
| 🌾 Premium Agro | ₦200,000 | ₦999,999 | 40% | 60 days |
| 👑 Elite Farm | ₦1,000,000 | Unlimited | 60% | 90 days |

---

## 📈 Workflow

```
User registers → funds wallet (deposit + proof) → admin approves →
user selects a plan + amount → investment activated → admin marks it
complete when mature → ROI credited to wallet → user requests a
withdrawal → admin approves (paid externally) or rejects (auto-refunded)
```

---

## 🔧 Production Checklist

- [x] CSRF protection on all forms
- [x] Rate limiting on login/register
- [x] HTTPS-only session cookies (automatic on Render)
- [x] Atomic, race-condition-safe balance updates
- [x] Database indexes on every filtered/sorted column
- [ ] Change the admin password from the default immediately after first login
- [ ] Decide whether to enable `USE_SUPABASE_STORAGE` for persistent proofs
- [ ] Point a real domain at the Render service and confirm HTTPS
- [ ] Set up a real payout process for approved withdrawals (this app tracks
      status — it does not move money on its own)
- [ ] Set up Supabase database backups (Settings → Database → Backups)

---

## 📞 Support

Built for Nigerian agricultural investors.

**Email:** invest@agrovest.ng
**WhatsApp:** +234 800 123 4567
