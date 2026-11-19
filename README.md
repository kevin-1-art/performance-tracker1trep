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
- **User and manager downloads (Excel `.xlsx` or `.csv`) work only on Thursday**, in the configured timezone (default `Africa/Kampala`). Admins can download any active user's report or the overall report on any day. Reports remain viewable outside the download window; the rule is enforced on the server, not just by hiding a button.
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

Production: `SECRET_KEY=<long random string> SESSION_COOKIE_SECURE=1 gunicorn "run:app" -b 0.0.0.0:8000` behind HTTPS.

## Public deployment (Render)

The repository includes a Render Blueprint configured with a persistent disk for the SQLite database and a generated `SECRET_KEY`. Persistent disks require a paid Render web-service plan and keep this app to a single service instance.

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/kevin-1-art/performance-tracker1trep)

After confirming the Blueprint in Render:

1. Wait for the service deployment to finish and copy its `onrender.com` URL from the Render dashboard.
2. Open the service Shell and run `flask --app run create-user --role admin` to create the first administrator. Do not seed demo accounts on a public deployment.
3. Share the service URL. The login page is public, but application records require authentication.

## Admin workflow

1. Log in as admin, open **Users**, create managers.
2. Create users and assign each a manager and a weekly target.
3. Set each user’s station in their user profile. Users enter their own records, or import an `.xlsx` workbook using the headers `doc_no, doc_type, entity_name, reg_no, prn, date_of_application, amount`. PRNs must be exactly 13 digits. Admins can also add or import on behalf of any user.
4. Registration totals are generated from saved application records. Users enter four weekly activity measures from the dashboard, before the Excel bulk-import form, or from the report page. Admins can enter activity on behalf of a selected user. Individual application records still require a 13-digit PRN; existing records can be edited to add document details.

## Sharing the report automatically

`flask --app run export-weekly` writes last week's company-wide `.xlsx` and `.csv` to `instance/reports/`. Schedule it with cron (for example every Thursday at 07:00) and email or sync the files however you like:

```
0 7 * * 4  cd /path/to/performance_tracker && .venv/bin/flask --app run export-weekly
```

## Configuration (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `SECRET_KEY` | dev value | **Set this in production** |
| `DATABASE` | `instance/tracker.db` | SQLite file |
| `APP_TIMEZONE` | `Africa/Kampala` | Decides what "Thursday" means |
| `DOWNLOAD_WEEKDAY` | `3` | 0=Mon ... 3=Thu ... 6=Sun |
| `CURRENCY` | `UGX` | Label only |
| `SESSION_COOKIE_SECURE` | `0` | Set `1` behind HTTPS |

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
- Passwords are hashed (Werkzeug); all POST requests are CSRF-protected; exports neutralise spreadsheet formula injection.
- SQLite is fine for a small team. For heavy concurrent use, move to PostgreSQL.
- Not included (add if needed): login rate limiting or lockout, password reset by email.

## Tests

```bash
python -m pytest -q
```
