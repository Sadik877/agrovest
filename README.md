# 🌾 AgroVest Pro — Agricultural Investment Platform

A premium, production-ready agricultural investment web application built with Flask (Python) and Tailwind CSS. Designed for Nigerian users with full NGN (₦) support.

---

## ✨ Features

| Feature | Description |
|---|---|
| 🏠 Landing Page | Hero, stats, plans, testimonials, FAQ, CTA |
| 📄 About Page | Mission, vision, how it works, trust signals |
| 💼 Investment Plans | 4 tiers: Starter, Green Harvest, Premium Agro, Elite |
| 🔐 Authentication | Register, Login, Logout with password hashing |
| 📊 User Dashboard | Balance, investments, deposits, withdrawals, referrals |
| 💳 Deposit System | Submit proof of payment, admin approval |
| 💸 Withdrawal System | Request withdrawal, admin approve/reject with refund |
| 🤝 Referral System | Unique links, 5% commission, real-time tracking |
| 🛠️ Admin Panel | Full CRUD: users, deposits, withdrawals, investments |
| 📱 Responsive UI | Mobile-first, works on all screen sizes |

---

## 🚀 Quick Start

### 1. Clone / Download
```bash
cd agrovest
```

### 2. Create Virtual Environment
```bash
python3 -m venv venv
source venv/bin/activate        # Linux / Mac
venv\Scripts\activate           # Windows
```

### 3. Install Dependencies
```bash
pip install -r requirements.txt
```

### 4. Run the Application
```bash
python run.py
```

Open **http://localhost:5000** in your browser.

---

## 🔑 Default Credentials

| Role | Email | Password |
|---|---|---|
| **Admin** | admin@agrovest.ng | Admin@2024! |

> ⚠️ Change the admin password immediately in production!

---

## 📁 Project Structure

```
agrovest/
├── app.py                    # Main Flask application
├── run.py                    # Startup script
├── requirements.txt          # Python dependencies
├── database.db               # SQLite database (auto-created)
│
├── templates/
│   ├── base.html             # Base layout with Tailwind config
│   ├── index.html            # Homepage
│   ├── about.html            # About page
│   ├── plans.html            # Investment plans page
│   ├── contact.html          # Contact page
│   ├── login.html            # Login page
│   ├── register.html         # Registration page
│   ├── dashboard_base.html   # Dashboard sidebar layout
│   ├── dashboard.html        # User dashboard overview
│   ├── invest.html           # Investment page
│   ├── deposit.html          # Deposit page
│   ├── withdraw.html         # Withdrawal page
│   ├── partials/
│   │   ├── navbar.html       # Public navigation
│   │   └── footer.html       # Site footer
│   ├── admin/
│   │   ├── base.html         # Admin sidebar layout
│   │   ├── dashboard.html    # Admin overview
│   │   ├── users.html        # User management
│   │   ├── deposits.html     # Deposit management
│   │   ├── withdrawals.html  # Withdrawal management
│   │   └── investments.html  # Investment management
│   └── errors/
│       ├── 403.html
│       ├── 404.html
│       └── 500.html
│
└── static/
    └── uploads/              # Payment proof uploads
```

---

## 💰 Investment Plans

| Plan | Min | Max | ROI | Duration |
|---|---|---|---|---|
| 🌱 Starter Farm | ₦10,000 | ₦49,999 | 15% | 30 days |
| 🌿 Green Harvest | ₦50,000 | ₦199,999 | 25% | 45 days |
| 🌾 Premium Agro | ₦200,000 | ₦999,999 | 40% | 60 days |
| 👑 Elite Farm | ₦1,000,000 | Unlimited | 60% | 90 days |

---

## 🔐 Admin Routes

| Route | Description |
|---|---|
| `/admin` | Admin dashboard with stats & pending actions |
| `/admin/users` | View, credit, suspend/activate users |
| `/admin/deposits` | Approve or reject deposit requests |
| `/admin/withdrawals` | Process or reject withdrawal requests |
| `/admin/investments` | Mark investments as complete (pays out) |

---

## 🎨 Design System

- **Primary Color:** Forest Green `#0A4A2F`
- **Accent:** Warm Gold `#C9A84C`
- **Background:** Light Gray `#F9FAFB`
- **Typography:** Inter (body) + Playfair Display (headings)
- **Effects:** Glassmorphism cards, grain texture overlays, smooth scroll animations
- **Icons:** Lucide Icons (CDN)
- **CSS Framework:** Tailwind CSS (CDN)

---

## ⚙️ Environment Variables

For production, set these environment variables:

```bash
SECRET_KEY=your-very-long-random-secret-key
```

---

## 🛡️ Security Features

- ✅ Bcrypt password hashing (via Werkzeug)
- ✅ Session-based authentication with 2-hour timeout
- ✅ Admin-only route protection decorator
- ✅ File upload validation (type + size)
- ✅ Input sanitization on all forms
- ✅ SQL parameterized queries (no SQL injection)
- ✅ CSRF-safe form submissions

---

## 📈 Workflow

```
User registers → funds wallet (deposit) → admin approves →
user selects plan + amount → investment created → admin marks 
complete when mature → ROI credited to wallet → user withdraws → 
admin processes → payment sent
```

---

## 🔧 Production Checklist

- [ ] Change `SECRET_KEY` to a random 64-character string
- [ ] Change admin password from default
- [ ] Set `debug=False` in `app.run()`
- [ ] Use PostgreSQL instead of SQLite for production
- [ ] Set up HTTPS with a valid SSL certificate
- [ ] Configure a proper email service for notifications
- [ ] Use Gunicorn + Nginx for deployment
- [ ] Set up automated database backups
- [ ] Add rate limiting to auth endpoints

---

## 📞 Support

Built with ❤️ for Nigerian agricultural investors.

**Email:** invest@agrovest.ng  
**WhatsApp:** +234 800 123 4567
