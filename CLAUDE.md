# CLAUDE.md — ClubLifter (repo: OneStepGPS)

Shared instructions for Claude Code on the owner's PC (VS Code) **and** Claude Code Cloud
(phone). Both work on the same GitHub repo with separate checkouts: **GitHub is the only
shared state.** Nothing uncommitted on one side is visible to the other.

This is a **live production app** used nightly by drivers, dispatch and guests. Prefer small,
targeted changes over rewrites. Never put secrets, passwords, API keys or personal phone
numbers in this file or in new code.

---

## 1. What the app is

ClubLifter: VIP transport, guest lists and driver dispatch for Las Vegas clubs
(e.g. Hustler Las Vegas). It covers:

- **Web dashboard** (promoters, dispatch, club owners/managers, master admins): guest list,
  today's schedule, driver and car management, live GPS tracking, analytics, QR codes, API keys.
- **Driver board** (`/driver…`, also embedded in the app as "Driver mode"): pickups,
  enroute/"I'm here" actions, clock in/out, in-app navigation.
- **Guest app** (`/app`, a PWA and a Capacitor native shell): SMS login, check-ins, loyalty
  levels, "Take me there" directions, GoBest clubs, live ride tracking.
- **Public tracking link** (`/t/<token>`, page `track.html`) that can be texted to guests.
- **AI agent / voice APIs** and **partner webhooks** (key-protected JSON APIs).

The repo is named after OneStepGPS, the vehicle-tracking provider whose API supplies live car
positions.

## 2. Repository layout (verified)

```
app.py              ENTIRE backend: Flask app, models, routes, integrations, background
                    tracker, seeds and migrations (~11k lines, ~220 routes)
requirements.txt    Unpinned: Flask, Flask-SQLAlchemy, Flask-CORS, requests, Werkzeug,
                    qrcode, Pillow, reportlab
templates/          Jinja2 pages (admin_*.html, driver_dashboard.html, index.html = guest
                    app, track.html, login.html, landing.html, privacy.html, ...)
                    _sidebar.html is the shared admin navigation
static/js/script.js Shared front-end JS
static/native.js    Capacitor native bridge (barcode, geolocation, local notifications).
                    No-op in a normal browser. Also served at /native.js
static/css/style.css
static/img/         Icons / PWA images
instance/clublifter.db  Small committed SQLite file (dev leftover; NOT production data)
```

There is **no** CI, test suite, Dockerfile, Procfile, `railway.json`/`nixpacks.toml`,
`.gitignore` or `.env.example` in the repo. The native app's source (Capacitor project) lives
outside this repo.

### Navigating app.py

It is split into banner sections. Search for `# ─── <NAME>` to jump, e.g.
`DATABASE CONFIG`, `TWILIO`, `SHOPIFY`, `MODELS`, `LOYALTY LEVEL SYSTEM`, `API KEY PROTECTION`,
`AUTH`, `PUBLIC LIVE TRACKING LINK`, `APP MANAGER`, `TEMPORARY PICKUP RESTRICTIONS`,
`REGISTER CUSTOMER`, `GOBEST APP APIs`, `APP AUTH (SMS login)`, `APP ADMIN MODE`,
`AI AGENT APIs`, `ADMIN: …` (users, API keys, clubs, packages, drivers, cars, shifts),
`DRIVER PORTAL`, `DRIVER PICKUP ACTIONS`, `LIVE DRIVER TRACKING`,
`BACKGROUND DISTANCE TRACKER`, `OPS API`, `INIT`, `REAL VENUES + CARTVIP PACKAGES`.
Use Grep with line numbers; never read the whole file at once.

## 3. Architecture

| Area | Details |
|---|---|
| Framework | Flask + Flask-SQLAlchemy (SQLAlchemy 2.x), Jinja2 server-rendered pages + JSON APIs, Flask-CORS |
| Database | `DATABASE_URL` (production: Postgres; `postgres://` is rewritten to `postgresql://`). Default fallback `sqlite:////data/clublifter.db` (a Railway volume path) |
| Uploads | `/data/uploads` (car/driver photos, "I'm here" photos), the persistent volume |
| Main models | `User` (web accounts + roles), `Club`, `Package`, `Customer` (a guest **ride/booking**: statuses, dispatch, return rides, tracking token), `Driver`, `Car`, `Shift`, `DriverDay` (clock-in/nights off), `RideEvent`, `RideChatMessage`, `ChatMessage`/`ChatRead`, `DriverNotice`, `DriverStop`, `AppUser`, `OTPCode`, `CheckIn`, `AdBanner`, `QRCodeItem`, `SavedLocation`, `GeoCache`, `SmsLog`, `Setting` (key/value runtime settings via `get_setting`/`set_setting`) |
| Time | Business logic runs on **Las Vegas time** (`America/Los_Angeles`) via `vegas_today()`, `vegas_naive_now()`, `vegas_datetime()`. DB timestamps are naive UTC (`datetime.utcnow`). A "night" ends at `CLUB_CLOSE_HOUR`, when drivers are auto clocked out |
| Phones | Always normalize with `normalize_phone_e164()` before comparing or storing |
| Background work | `distance_tracker_loop` daemon thread (driver positions, 15/10/5 km guest notifications, stop detection, dispatch engine ticks). Outbound SMS and webhooks are sent from short-lived daemon threads |
| Logging | `print(..., flush=True)` to stdout (Railway log), mirrored to an in-memory ring buffer per process (readable via `/api/ops/logs`) |
| Build marker | `CODE_BUILD = "..."` in app.py, returned by `GET /api/app/ping`. **Bump it in every PR that changes behavior**, so the live build can be confirmed after a deploy |

### Auth and roles

- **Web:** username/password session login (`/login`, in-memory rate limiter), 30-day
  permanent sessions signed with `SECRET_KEY`.
  Roles: `master`, `club_owner`, `club_manager`, `dispatch_manager`, `dispatch`, `promoter`,
  `driver`. Groups: `ADMIN_LEVEL_ROLES`, `DISPATCH_ROLES`, `APP_MANAGER_ROLES`,
  `CREATABLE_ROLES` (who may create whom). Helpers: `is_master()`, `is_admin_level()`,
  `can_dispatch()`, `can_see_api()`, `is_dispatch_manager()`.
  Reuse these; don't add ad-hoc role string checks.
- **App:** SMS one-time-code login (`/api/app/auth/send-code`, `/api/app/auth/verify`), with
  per-day OTP limits for staff numbers. A QA test login is enabled only when `QA_TEST_PHONE` and
  `QA_TEST_CODE` are set. Staff "admin mode" in the app uses `app_staff_required(perm)` and
  the same role groups as the web.
- **Machine APIs:** `API_ACCESS_KEY` (or the `api_access_key` setting) for AI/voice/partner
  APIs. `OPS_API_KEY` (`X-Ops-Key` header) for `/api/ops/*`.
- `SECRET_KEY` also signs QR flyer links (`URLSafeTimedSerializer`) and partner-status HMACs.
  **Changing it logs everyone out and breaks existing signed links.**

### Integrations

| Service | Use | Config (names only) |
|---|---|---|
| OneStepGPS | Live vehicle positions (`track.onestepgps.com/v3/api/public/marker`) | `ONESTEPGPS_API_KEY` |
| Telnyx | Primary SMS/MMS + voice calls (TeXML) | `TELNYX_API_KEY`, `TELNYX_FROM_NUMBER`, `TELNYX_MESSAGING_PROFILE_ID`, `TELNYX_TEXML_APP_ID`, `TELNYX_VOICE_NUMBER` |
| Twilio | Alternate SMS/voice provider | `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`, `TWILIO_MESSAGING_SERVICE_SID`, `TWILIO_VOICE_NUMBER` |
| Shopify | Optional order creation (off by default) | `SHOPIFY_ENABLED`, `SHOPIFY_STORE`, `SHOPIFY_TOKEN` |
| Make.com | Outbound webhook | `MAKE_WEBHOOK_URL` |
| Partner status webhooks | Guest status pushes to partners | `PARTNER_STATUS_WEBHOOK_URL`, `PARTNER_STATUS_WEBHOOK_KEY`, `PARTNER_STATUS_PROMOTERS` |
| Nominatim / Photon | Geocoding (cached in `GeoCache`) | `NOMINATIM_UA` |
| OSRM | Routing / ETAs | `OSRM_URL`, `TRACK_ETA_AVG_KMH` |
| CartVIP | Package checkout URLs (seeded in `VENUES`) | none |
| Front-end CDNs | Bootstrap 5.3.0, Leaflet 1.9.4, leaflet-routing-machine (`@latest`, unpinned) | none |

### Feature flags and tunables (env)

`SMS_ENABLED` (**default ON**), `CALLS_ENABLED` (**default ON**), `SHOPIFY_ENABLED` (default
off), `TRACKING_LINK_ENABLED` (default off), `DISABLE_TRACKER`, `FLASK_DEBUG`, `PORT`,
`PUBLIC_BASE_URL`, `CLUB_CLOSE_HOUR`, `DRIVER_BUSY_WINDOW_MIN`, `TRACK_LINK_EXPIRE_HOURS`,
`OPS_ALERT_NUMBERS`, `DEMO_*` (demo ride/login seeds), `QA_TEST_PHONE`/`QA_TEST_CODE`.
Several dispatch tunables near the `LIVE DRIVER TRACKING` section can be overridden in Railway.

## 4. Startup, schema changes and seeds (critical)

Runs at **import time** in `with app.app_context():` near the end of app.py:

1. `db.create_all()` creates missing tables.
2. `safe_migrate(table, column, ddl)` adds missing columns with `ALTER TABLE … ADD COLUMN`.
   There is **no Alembic**. Each migration runs in its own transaction.
3. Seeds run on **every boot**: `seed_data`, `seed_gobest_clubs`, `retire_clubs`,
   `seed_admin_phones`, `seed_staff_accounts`, `seed_booking_links`, `seed_test_driver`,
   `fix_known_bad_venue_coords`, `seed_demo_ride`.
4. Then the tracker thread starts (unless `DISABLE_TRACKER=1`, or in the debug reloader parent).

Rules:
- **New column:** add it to the model **and** add a `safe_migrate(...)` line. Make it nullable
  or give it a DEFAULT. The SQL must work on **both Postgres and SQLite**.
- **Never** drop or rename columns or tables, change column types, or delete or rewrite user
  data without explicit approval. Old columns are kept for backward compatibility (e.g. the
  deprecated `Driver.car_*` fields).
- **Seeds must be idempotent** (check before insert) and must not overwrite data edited in
  the UI.
- Production may run several worker processes (`WEB_CONCURRENCY`/gunicorn hints in
  `/api/ops/config`). In-memory state (rate limiter, log ring, caches) is **per process**.
  Shared state belongs in the DB (see `SmsLog`, `Setting`).

## 5. Critical areas: change carefully and say so in the PR

- Startup migrations and seeds (above).
- `distance_tracker_loop` / dispatch engine / driver stop detection. These are timing-sensitive
  and affect live rides. Note `BOOT_GRACE_SEC` after deploys.
- SMS and call sending (`send_sms`, `send_sms_many`, Telnyx/Twilio). Mistakes text real
  guests and drivers.
- Login, sessions, OTP and role checks.
- Driver clock in/out ("clock-out is final"; only clocked-in drivers get pickups tonight).
- Guest/ride status values and labels. Partners and the app depend on the exact status strings.
- The apex→www redirect (`_redirect_to_www`, clublifter.com → www.clublifter.com).
- `/api/ops/*`: must stay **read-only**.
- **Temporary features:** the F1 pickup restriction (`TEMPORARY PICKUP RESTRICTIONS`, through
  Nov 23 2026) can be extended or ended at runtime via `POST /admin/pickup-restriction`, with no
  deploy. Remove the code only when asked.

## 6. Running and testing locally / in the cloud

There are no automated tests yet. Minimum validation for any change: boot the app against a
**throwaway SQLite DB** with all outbound side effects off, then hit the affected routes.

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

```bash
DISABLE_TRACKER=1 SMS_ENABLED=false CALLS_ENABLED=false \
DATABASE_URL=sqlite:////tmp/clublifter-test.db \
.venv/bin/python -c "
import app as m
c = m.app.test_client()
for p in ['/', '/login', '/api/app/ping']:
    print(p, c.get(p).status_code)
"
```

- **Always** set `SMS_ENABLED=false` and `CALLS_ENABLED=false`, and point `DATABASE_URL` at a
  temp file. Never at `instance/clublifter.db`, and never at production.
- Importing app.py creates `__pycache__/` (there's no .gitignore). Don't commit it, or the
  `.venv/` folder.
- Real API keys (OneStepGPS, Telnyx, Twilio, Shopify) are **not** available in cloud
  sessions, so those paths can't be tested end to end. Say what was and wasn't tested in every PR.
- For template/JS changes, render the page via the test client (log in as a seeded test user
  in the temp DB if needed), or use Playwright for a screenshot.
- `python3 -m py_compile app.py` is a quick syntax check.

## 7. Deployment (Railway): what is and isn't known

Verified from code: hosted on Railway (`RAILWAY_GIT_COMMIT_SHA`, `RAILWAY_SERVICE_NAME`,
`*.up.railway.app`), with a persistent volume at `/data`, Postgres via `DATABASE_URL`, and likely
gunicorn (`WEB_CONCURRENCY`, `GUNICORN_CMD_ARGS`).

**Not in the repo:** the build/start command, auto-deploy settings, which branch deploys, and
environment variables. gunicorn isn't in `requirements.txt`, so the start command is configured
in Railway. **Do not assume pushing or merging to `main` is safe.** Check in the Railway dashboard
(service → Settings → Source / Deploy): the connected repo and branch, whether "auto deploy"
is on, whether PR environments are enabled, the start command, and the volume mount.

To confirm what's live after a deploy: `GET /api/app/ping` → `build` (matches `CODE_BUILD`), or
`/api/ops/ping` → `commit`.

Do not add or modify deployment config (Procfile, railway.json, nixpacks, Dockerfile) without
explicit approval.

## 8. GitHub workflow (PC ⇄ Cloud)

Hard rules:
- **Never commit or push directly to `main`.** One dedicated branch per task (cloud sessions
  use the branch name they're assigned, e.g. `claude/...`; locally use `feature/...` or `fix/...`).
- **Never merge PRs, force-push, rewrite shared history, or trigger a production deploy**
  without the owner's explicit approval.
- Never discard, reset or overwrite uncommitted work without permission.

Every task:
1. `git status`, then `git fetch origin`. Compare your branch with `origin/main`, and look for
   new branches or open PRs that touch the same code.
2. Branch from the latest `origin/main`.
3. Inspect the relevant code and its callers before editing. Make the smallest reliable change.
4. Validate (section 6). Self-review for regressions and security issues.
5. Bump `CODE_BUILD` if behavior changed.
6. Commit with a descriptive message, `git push -u origin <branch>`, and open a PR for review.
   The PR states the files changed, what was tested, what couldn't be tested, and any risk
   to critical areas.
7. If the remote moved meanwhile: fetch, **merge** `origin/main` in (don't rebase pushed
   branches), and re-test. Explain conflicts instead of resolving them blindly.

On the PC: `git pull` before starting, and push before switching to the phone. The cloud
can't see local, unpushed work.

## 9. Conventions

- Keep the single-file structure; put new code in the matching `# ─── SECTION ───` block (or
  a new banner section). Don't split app.py without approval.
- Reuse existing helpers (`get_setting`, `normalize_phone_e164`, `vegas_*` time helpers, role
  helpers, `send_sms*`) instead of duplicating logic.
- Runtime-adjustable behavior goes in `Setting` or env vars, not hardcoded.
- JSON APIs return `{"success": true/false, ...}` (or `{"error": ...}` with a status code).
- Comments explain *why* (often with the date/request context). Follow that style.
- UI text is English (US). Guest status labels are business-defined; don't rename them on your own.
- Pin new CDN assets to exact versions.
- **Never hardcode credentials, API keys or personal phone numbers.** Read them from env vars.

## 10. Known issues and warnings

- `requirements.txt` is unpinned, so fresh installs may differ from production.
- No automated tests, no CI, no `.gitignore`.
- `instance/clublifter.db` is committed (dev data).
- Some seed code contains hardcoded account credentials and staff phone numbers. Treat
  them as sensitive: don't copy, log or expand them; moving them to env vars is a planned fix.
- The `/api/ops/sql` read-only guard is a keyword blocklist (SQLite also gets
  `PRAGMA query_only`; Postgres doesn't). Don't loosen it.
- Per-process in-memory state under multiple workers (see section 4).
