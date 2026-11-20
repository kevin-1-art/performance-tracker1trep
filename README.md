# Performance Tracker

A full-stack Python (Flask + SQLite) app that records application and registration details, tracks each person's performance, and produces individual TREP-style Excel reports. User and manager downloads are available on Thursdays; admins can download any user's report any day.

## Roles and interfaces

| Role | Interface | Can see |
|---|---|---|
| **User** | `/my` personal dashboard: add records, Excel import, weekly KPIs, target progress, daily chart | Only their own records and numbers |
| **Manager** | `/manager/` team dashboard (plus `/my` for their own records) | Their team (users assigned to them) and themselves |
| **Admin** | `/admin/` overview, user management, all records, download audit log | Everyone |

Each role gets its own navigation and landing page after login.

## Weekly report

- A reporting week runs **Friday to Thursday**. Reports default to the current reporting week; enter only a from date to select that week or both dates for a custom period.
- Metrics per person: applications, total amount, average amount, active days, weekly target, % of target achieved, previous-week total, week-on-week change, rank.
- Individual Excel reports use the TREP layout and include the TREP partner marks and URSB logo in the header, registration totals generated from saved application records, NTR totals from those records, manually entered activity measures, station, and a document-level detail table.
- Team and overall Excel/CSV summaries include each person's weekly activity measures and totals.
- The report is **scoped by role**: users get their own, managers their team, admins everyone.
- **User and manager downloads (Excel `.xlsx` or `.csv`) work only on Thursday**, in the configured timezone (default `Africa/Kampala`), unless an admin grants that account any-day download access. Admins can grant or revoke the all-weeks permission from an individual user's report page. Admins can download any active user's report or the overall report on any day. Reports remain viewable outside the download window; download permissions are enforced on the server.
- Admins can download an individual report on any day, including the current Friday–Thursday period; an in-progress download includes dates through today. User and manager downloads unlock on Thursday.
- Every download is logged and shown on the admin overview.

## Quick start

```bash
cd performance_tracker
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 1) create your first admin
flask --app run create-user --role admin

# (or load demo data: admin / manager1 / alice, password "Password123!")
# flask --app run seed-demo

# 2) run
python run.py          # http://127.0.0.1:5000
```

Production: `APP_ENV=production SECRET_KEY=<long random string> gunicorn "run:app" -b 0.0.0.0:8000` behind HTTPS. Production startup rejects the development secret and forces secure session cookies.

## Public deployment (Render)

The repository includes a Render Blueprint for a free web service with a generated `SECRET_KEY`. The free service uses an ephemeral filesystem: SQLite data is lost whenever Render spins the service down after inactivity, restarts it, or redeploys it. Free instances can also take about a minute to wake and have limited monthly usage. This is for demos only; do not enter real or important records. Free web services do not provide Shell access.

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/kevin-1-art/performance-tracker1trep)

During Blueprint setup, provide values for `INITIAL_ADMIN_USERNAME`, `INITIAL_ADMIN_FULL_NAME`, and `INITIAL_ADMIN_PASSWORD` in Render's environment settings. Use a unique password of at least 12 characters; never put credentials in the repository. The service creates that admin account when its database is empty, including after a free-tier restart clears the database.

After the deploy completes, copy the service's `onrender.com` URL from the Render dashboard and share it. The login page is public, but application records require authentication.

## Admin workflow

1. Log in as admin, open **Users**, create managers.
2. Create users and assign each a manager and a weekly target.
3. Set each user’s station in their user profile. Users enter their own records, or import an `.xlsx` workbook using the headers `doc_no, doc_type, entity_name, reg_no, prn, date_of_application, amount`. PRNs must be exactly 13 digits. Admins can also add or import on behalf of any user.
4. Registration totals are generated from saved application records. Users enter daily values for clients engaged, radio talk shows, joint field operations, and OBRS accounts on the dashboard or report page. Reports sum each day's values for the selected period. Admins can enter daily activity on behalf of a selected user. Existing weekly activity totals are migrated to the first day of their week.

## Sharing the report automatically

`flask --app run export-weekly` writes last week's company-wide `.xlsx` and `.csv` to `instance/reports/`. Schedule it with cron (for example every Thursday at 07:00) and email or sync the files however you like:

```
0 7 * * 4  cd /path/to/performance_tracker && .venv/bin/flask --app run export-weekly
```

## Configuration (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `APP_ENV` | `development` | Use `production` to require a non-default secret and secure cookies |
| `SECRET_KEY` | dev value | **Set this in production** |
| `DATABASE` | `instance/tracker.db` | SQLite file |
| `APP_TIMEZONE` | `Africa/Kampala` | Decides what "Thursday" means |
| `DOWNLOAD_WEEKDAY` | `3` | 0=Mon ... 3=Thu ... 6=Sun |
| `CURRENCY` | `UGX` | Label only |
| `SESSION_COOKIE_SECURE` | `0` | Set `1` behind HTTPS; always enabled in production |

## Project layout

```
run.py                  entry point
app/
  __init__.py           app factory, CSRF, error pages
  config.py  db.py  utils.py
  auth.py               login, roles, password change
  records.py            user interface, add / Excel import / edit / delete
  manager.py            team dashboard
  admin.py              overview, users, all records
  reports.py            report page + Thursday-gated download
  reporting.py          aggregation and xlsx/csv export
  cli.py                init-db, create-user, seed-demo, export-weekly
  templates/  static/
tests/test_app.py       tests for roles, scoping, Thursday gate, Excel import, and exports
```

## Design notes and security

- Amounts are stored as integer minor units (amount x 100) to avoid floating-point errors.
- PRNs must contain exactly 13 digits; keep them as text in Excel to preserve any leading zeroes.
- PRNs are unique across the whole system and must contain exactly 13 digits, so the same PRN cannot be counted twice by different users.
- The supplied TREP/partner artwork is used as a faint watermark across the app background.
- Passwords are hashed (Werkzeug), require at least 12 characters for new or changed accounts, and are served through secure, HTTP-only, same-site session cookies in production. All POST requests are CSRF-protected; exports neutralise spreadsheet formula injection.
- Responses set a restrictive Content Security Policy and other browser security headers, disable caching, and return a generic error page for unexpected server errors. There are no API endpoints or API documentation linked in the UI; routes that handle records require authentication and role checks.
- Use a unique production `SECRET_KEY`, HTTPS, and a persistent database. This free Render demo still uses ephemeral SQLite storage and is not appropriate for important records.
- SQLite is fine for a small team. For heavy concurrent use, move to PostgreSQL.
- Not included (add if needed): login rate limiting or lockout, password reset by email.

## Tests

```bash
python -m pytest -q
```
