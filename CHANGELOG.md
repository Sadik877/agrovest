# Changelog — Production Audit & Rebuild

This pass focused on real bugs found by reading every file, not a cosmetic
pass. Each item below was an actual defect in the uploaded project, verified
against the code and (where money is involved) against a scripted end-to-end
test before and after the fix.

## 🔴 Critical — would break deployment entirely

- **DNS resolution errors during cold start weren't being retried.** Right
  after a fresh deploy or restart, Render's container network can take a
  second or two to come fully up, so the first few calls to Supabase fail
  with `[Errno -2] Name or service not known`. The retry logic didn't
  recognize this as a transient error, so it gave up instantly. Worse,
  `init_db()` printed "✓ Default plans seeded" even when every single insert
  had just failed for this reason — and because seeding only ran when the
  plans table was *completely* empty, a partial failure (e.g. 2 of 4 plans
  inserted before a hiccup) would permanently skip seeding on every future
  restart. Fixed by: (1) recognizing DNS failures as retryable, (2) adding a
  short retry-with-backoff specifically around startup connectivity, and
  (3) seeding each plan independently by slug, so a restart always fills in
  whatever's still missing instead of an all-or-nothing check.
- **`render.yaml` and `.env.example` configured the wrong environment
  variables.** They told you to set `DATABASE_URL` (a Postgres connection
  string) — but `app.py` only ever reads `SUPABASE_URL` and `SUPABASE_KEY`
  (the Supabase REST API URL + service-role key). Following the original
  instructions literally meant the app would *never* connect to Supabase,
  matching exactly the reported symptoms: "Invalid API key", "Admin account
  not created", "Supabase connection issues". Fixed both files to set the
  variables the app actually uses.
- **`/admin/debug` crashed with a `NameError` every time it was opened.** It
  referenced `SUPABASE_URL` / `SUPABASE_KEY` as bare names that were never
  defined anywhere at module scope (only inside the connection function as
  local variables). Fixed to read from `os.environ` directly, and expanded
  the route to report `RENDER`/storage config too.
- **Silent registration failures.** If the Supabase insert for a new user
  failed for any reason (network blip, duplicate race), the app told the
  user "Account created!" and sent them to log in — with no account. Now
  checks the insert result and shows a real error instead of lying.

## 🟠 Money-handling race conditions (the kind that don't show up until you have real traffic)

- **Double-spend on invest/withdraw.** Balance changes were done as
  "read balance in Python → subtract → write it back" — three separate round
  trips. Two near-simultaneous requests (a double-clicked submit, two open
  tabs) could both read the same starting balance and both succeed, letting
  someone spend more than they have. Added `agrovest_adjust_balance(...)`, a
  Postgres function that takes a row lock and does the check-and-write
  atomically, and pointed every balance change (invest, withdraw, deposit
  approval, withdrawal approval/rejection, investment payout, admin credit)
  through it.
- **Double-processing on admin approve/reject.** The same race existed on
  the admin side: two clicks on "Approve" for the same deposit could credit
  the user twice. Fixed by including `status = 'pending'` in the actual
  database `WHERE` clause of the status update (not just a Python pre-check),
  so only one of two concurrent requests can ever win.
- **Money could vanish if a step failed partway through.** E.g. a user's
  balance was debited for an investment, but if saving the investment record
  itself then failed, the money was just gone with nothing to show for it.
  Added rollback (and a loud server-side log) for the invest and withdraw
  flows so a failed second step puts the balance back.
- All money math is now consistently rounded to 2 decimal places (`r2()`
  helper) instead of accumulating floating-point drift over many
  transactions.

## 🟡 Security

- **No CSRF protection anywhere.** Added Flask-WTF and a hidden token to
  every one of the 19 forms across the public site, the dashboard, and the
  admin panel, plus the `X-CSRFToken` header on the one AJAX call.
- **No rate limiting on login/register.** Added a lightweight per-IP limiter
  (10 attempts / 10 minutes) so the auth endpoints can't be brute-forced
  trivially.
- **Session cookies had no security flags set.** Added `HttpOnly`,
  `SameSite=Lax`, and `Secure` (HTTPS-only) in production — detected
  automatically via Render's own `RENDER` environment variable.
- **`SECRET_KEY` silently fell back to a hardcoded string** if unset, which
  would let anyone forge session cookies. Now generates a random key per
  process (with a clear warning) instead of using a fixed fallback.
- **An admin could accidentally remove their own admin access or suspend
  their own account**, locking themselves out with no recovery path other
  than the database. Both are now blocked.
- Added a friendly error page for CSRF failures and for files over the
  5MB upload limit (both previously surfaced as raw 400/413 errors).

## 🟢 Bugs that produced wrong or crashing pages

- **The notification bell never actually loaded anything** — the dropdown
  permanently said "Loading..." because nothing ever fetched the
  notification list. Added a real `/dashboard/notifications` JSON endpoint
  and wired up the dropdown to use it.
- **Plan creation/editing crashed (HTTP 500) if "Maximum amount" was left
  as anything non-numeric**, and didn't validate that max > min — you could
  silently create a plan where the maximum investment was *less* than the
  minimum. Both are now validated with a clear error message instead of a
  stack trace.
- Registration accepted any string containing `@` as an email and any phone
  number at all; added real format validation for both.
- Removed a leftover Alpine.js attribute (`x-data`) on the navbar — Alpine
  was never actually loaded, so it did nothing.
- Removed a stray, malformed top-level folder (`{templates,static...`) left
  over from a shell command that didn't expand correctly, and an unused
  duplicate `uploads/` directory.

## 🔵 Production-readiness / infrastructure

- **Deposit-proof uploads live on Render's ephemeral disk by default**,
  which is wiped on every deploy/restart — a real risk for financial proof
  documents. Added optional Supabase Storage support
  (`USE_SUPABASE_STORAGE=true`) with signed-URL viewing for admins, and a
  safe automatic fallback to local disk if storage isn't configured or a
  particular upload fails, so this is never a hard requirement.
- **No indexes on any foreign-key or status column** — every dashboard and
  admin list was doing a full table scan. Added indexes on every column the
  app actually filters or sorts by.
- **`updated_at` on deposits/withdrawals was permanently stuck at its
  original `created_at` value** — the app never set it, and there was no
  database trigger either. Added a trigger so it updates automatically on
  every change.
- Added light retry-with-backoff for transient network errors on read
  queries (timeouts, dropped connections), with automatic client
  reconnection — without retrying writes blindly, which could otherwise
  risk double-charging on a request that actually succeeded but lost its
  response.
- Bumped all dependencies to current stable releases (Flask 3.1.3, Werkzeug
  3.1.8, supabase-py 2.31.0, gunicorn 23, Python 3.11.15) and added
  Flask-WTF for CSRF.
- `render.yaml`'s `PYTHON_VERSION` now matches `runtime.txt` (previously
  `3.11.0` vs `3.11.9` — inconsistent, and `3.11.0` is years out of date on
  security patches).

## Testing

Before packaging, every flow below was run end-to-end against a scripted
test harness (register → login → deposit → admin approve → invest →
withdraw → admin approve/reject → investment payout → referral commission
→ admin user/plan management), including deliberately trying to
double-process the same deposit/withdrawal/investment to confirm the new
atomic-update protection actually holds.
