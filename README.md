# amex-submission — Cox Research Amex employee verification page

Render service **amex-submission** → default address `https://amex-submission.onrender.com`,
custom domain **`https://amex.coxresearch.com`** (GoDaddy CNAME `amex` → `amex-submission.onrender.com`).

## What it does
* Each employee gets a private link per statement: `https://amex.coxresearch.com/v/<token>`.
  The token is an HMAC of (statement, employee) with `TOKEN_SECRET` (144-bit, unguessable).
* Page: full-width blue **American Express Charges** banner, employee name left-aligned under it, charges sorted by date. Credits are green with a minus sign and have no flag and no category edit. Footer rows are Total charges, Credits, and Net Total.
* **Flag**: the red button opens a **Note/Reason** popup with five presets (Wrong category, Wrong price, Duplicate charge, Not my charge, Other) plus a typed note. Save stores the note, flags the row, closes the popup, and moves to the next charge. Clicking a flagged row again clears it. No page reload. Logged. A **Flagged charges** box takes one overall comment.
* **Amex Category**: click a charge's category and pick one of Travel, Fuel, Meals, Supplies, Parts & Materials, Software, Entertainment, Other. That is the same list as the summary grid. A statement category from the old Amex list is shown as its mapped row: Restaurant as Meals, Merchandise & Supplies and Office Supplies as Supplies, and Transportation, Business Services, Communications, and anything else as Other. A change shows in blue with the original struck through.
* **Submit** and **Print**: Submit approves every unflagged row, locks the page, appends to the log on the persistent disk,
  and automatically emails a record (who, statement, flagged rows, approved rows, timestamp) to
  `RECORD_TO` = **littledude@mail.grokbot.com** (a leftover `littledude@coxresearch.com` value is rewritten to this address; optional `RECORD_CC`).
  Each flagged line is charge date, merchant, amount, flagged date, reference number (or "ref pending"), then the note. The overall comment follows those lines as `Flagged charges: <text>`. Print hides the buttons and popups. Nothing here posts to QuickBooks or American Express.
* **Alice E Cox**: her review link and verification email always go to **chris@coxresearch.com**, never alice@. That is hardcoded and is not changed by an uploaded email address.
* Log: `/var/data/log.jsonl` (append-only; opens, flags, unflags, submits, email results, and `qb_submit`).
* Each employee link shows only that employee's charges, notes, and submission. Print uses that same page. The token is an unguessable HMAC, and there is no list of employees or tokens.
* Chris and Meghan admin (HTTP login, username `Chris` or `Meghan` + `ADMIN_PASSWORD`):
  `/admin` (who has / hasn't submitted + each employee's link), `/admin/log.csv`, `/admin/links.json`,
  `POST /admin/upload` (add a month), `POST /admin/reset/<stmt>/<emp-id>` (reopen a page).
  An employee token cannot open any of these.
* **Monthly summary** for Chris and Meghan only: `/admin/summary` and `/admin/summary/<stmt>`
  (for example `/admin/summary/2026-09-22`). Same username and `ADMIN_PASSWORD`. The username is what a QuickBooks press records.
  One row per employee: name, total charges on the statement, and submitted or not submitted.
  Under that, the Amex statement total, then a fixed category grid: Travel, Fuel, Meals, Supplies, Parts & Materials, Software, Entertainment, and Other. Each amount starts as the sum of submitted charges in that category (an employee edit wins over the statement category, and retired Amex names roll into that row) and can be typed over. The running sum updates as you type. A red warning shows when that sum does not match the Amex statement total. **Save** (`POST /admin/summary/<stmt>/categories`) keeps the edited amounts for that statement only.
  Download links: `/admin/summary/<stmt>/summary.pdf`, `summary.csv`, and `summary.xlsx`. Those files use the grid amounts.
  The month folder name is `American Express charges <Month> <YYYY>` (for example `American Express charges October 2026`).
  **Submit to QuickBooks for Pay** is on this page only. It stays disabled until every cardholder has submitted and the grid sum equals the Amex statement total; otherwise the page shows a red warning with the difference.
  Pressing it sends the grid amounts (not a fresh sum of the charges), locks the month, appends a `qb_submit` line, and saves the PDF, spreadsheet, and each submission record under `/var/data/summaries/American Express charges <Month> <YYYY>/`.
  The summary files are named `American Express charges <Month> <YYYY> - Summary.pdf` and `.xlsx` (and `.csv`).
  The email to `littledude@mail.grokbot.com` (dry-run writes `/var/data/outbox`) has subject `QB SUBMIT: American Express charges <Month> <YYYY>`.
  Each employee's own SUBMITTED record uses that same folder name in its subject so it can be filed with the month. That record is still only an email. Employee Submit does not build this summary or send the QB email. Employee pages have no QuickBooks button and do not call Dropbox.
  Littledude creates the bill and files the attachments. This server stores no QuickBooks or Dropbox credentials and does not call either service.
  After the month is locked, flag, category, comment, submit, grid edits, and admin reset return 409.
* The email builder (`tools/make_email.py`) puts only "Open my charges" links in the email, never flag/submit links,
  because Outlook Safe Links and scanners auto-open links and would create fake flags.

## Files
| Path | Purpose |
|---|---|
| `app.py` | Flask app (page, flag, submit, email, log, admin, monthly summary) |
| `summary.py` | Category grid, PDF, and spreadsheet. No outside service calls |
| `requirements.txt` | Flask, gunicorn, tzdata |
| `render.yaml` | Blueprint: Starter web service + 1 GB disk at /var/data + env vars |
| `tools/seed_sep22.py` | Builds `statements/2026-09-22.json` from the approved v5 chart (73 charges, $17,024.09) |
| `tools/load_csv.py` | Generic monthly loader: any employee, any month (CSV date,category,company,amount) |
| `tools/make_email.py` | v7 email builder → `out/<stmt>/<emp>.html` with links to amex.coxresearch.com (builds only, sends nothing) |
| `tools/render_deploy.sh` | The Render API calls (owner, create, env, deploy, domain, verify, logs, upload) |
| `tests/local_test.sh`, `tests/browser_click_test.py`, `tests/test_flag_note.py`, `tests/test_summary.py` | Local end-to-end tests (dry-run email) |

`statements/*.json` is git-ignored. The Sep 22, 2026 test statement is the exception, shipped at `statements/bundled/2026-09-22.json` so this deploy serves Chris Cox (72 charges, $17,048.40, AutoZone credit -$24.31, net $17,024.09) and Alice E Cox (9 charges, $480.62). A disk copy wins only when its `bundle_rev` is higher. `/admin/upload` sets a higher `bundle_rev`, so a later upload still replaces it.

## Environment variables
| Key | Value | Who sets it |
|---|---|---|
| `TOKEN_SECRET` | auto-generated by Render. **Never change after links go out.** | Render |
| `ADMIN_PASSWORD` | auto-generated by Render | Render |
| `PUBLIC_BASE` | `https://amex.coxresearch.com` | preset |
| `EMAIL_MODE` | `dryrun` → `smtp` once the mail password is in | Littledude |
| `RECORD_TO` | `littledude@mail.grokbot.com` | preset. The old `littledude@coxresearch.com` value is rewritten to this address. |
| `RECORD_CC` | empty, or `chris@coxresearch.com` | Chris decides |
| `MAIL_FROM` | `Chris AI <littledude@coxresearch.com>` | preset |
| `SMTP_HOST/PORT/USER` | `smtp.office365.com` / `587` / `littledude@coxresearch.com` | preset |
| `SMTP_PASSWORD` | littledude mailbox password | **Chris, typed into the Render dashboard only** |
| `RESEND_API_KEY` | only if the Resend fallback is used | Chris |

## Sending the record email
**Choice: SMTP through the littledude@coxresearch.com Microsoft 365 mailbox (GoDaddy).**
littledude sends the record to itself, so there's no new vendor, no DNS change, and every record lands where Littledude can read it.
* Needs: **SMTP Authentication turned ON for littledude@coxresearch.com.** In GoDaddy go to Email & Office Dashboard → littledude → Manage → Advanced Settings → SMTP Authentication (GoDaddy M365 often ships with it off; GoDaddy help article 40981). **Warning from GoDaddy:** if "security defaults" are on for the organization, turning this on switches security defaults off for the whole org. MFA can still be kept for littledude by using an app password. Then the **mailbox password goes into `SMTP_PASSWORD`**. If that mailbox uses MFA, use an app password instead.
* Who provides it: **Chris**, as the GoDaddy admin. He types it straight into Render (Environment tab). It never goes in chat.
* If GoDaddy/Microsoft blocks SMTP AUTH: fallback is `EMAIL_MODE=resend`. That needs a Resend account, Resend's DKIM/SPF records added on coxresearch.com in GoDaddy, and a `RESEND_API_KEY`. Code for it is already in place.
* Until then, `EMAIL_MODE=dryrun` writes each record email to `/var/data/outbox/*.eml`. Submits are still logged, and `/admin` still shows who submitted.

## Deploy steps (Render API, run by Littledude after Chris's part)
Prereqs: Render account + card (Chris), GitHub repo `amex-submission` containing this folder (the Render GitHub app must be allowed to access it, which is one click by Chris), `RENDER_API_KEY` delivered by secure form.
```bash
export RENDER_API_KEY=...  REPO=https://github.com/<owner>/amex-submission
tools/render_deploy.sh owner                      # GET  /v1/owners            -> OWNER_ID
OWNER_ID=tea-... tools/render_deploy.sh create    # POST /v1/services          -> SERVICE_ID, first deploy (plan starter, disk 1 GB)
SERVICE_ID=srv-... tools/render_deploy.sh status  # GET  /v1/services/{id}/deploys  until "live"
curl https://amex-submission.onrender.com/healthz # -> ok
SERVICE_ID=srv-... tools/render_deploy.sh env     # GET  env-vars -> read ADMIN_PASSWORD, TOKEN_SECRET (kept private)
ADMIN_PASSWORD=... STMT=2026-09-22 tools/render_deploy.sh upload   # POST /admin/upload (Sep 22 data onto the disk)
SERVICE_ID=srv-... tools/render_deploy.sh domain  # POST custom-domains amex.coxresearch.com
#   GoDaddy DNS (Chris signs in): CNAME  amex -> amex-submission.onrender.com   (nothing else touched; email/MX unaffected)
SERVICE_ID=srv-... tools/render_deploy.sh verify  # POST custom-domains/.../verify; Render issues the TLS cert
# after Chris enters SMTP_PASSWORD in the dashboard:
SERVICE_ID=srv-... KEY=EMAIL_MODE VALUE=smtp tools/render_deploy.sh setenv
SERVICE_ID=srv-... tools/render_deploy.sh deploy
TOKEN_SECRET=... python tools/make_email.py 2026-09-22 chris-cox   # TEST email HTML -> send to chris@coxresearch.com only
```
Alternative to `create`: Render dashboard → New → Blueprint → pick the repo (uses `render.yaml`; prompts for `SMTP_PASSWORD`).

## Each month
1. Build a CSV per employee (date,category,company,amount), then run `tools/load_csv.py --stmt YYYY-MM-DD ...`. Repeat for every employee.
2. `tools/render_deploy.sh upload` with `STMT=YYYY-MM-DD`.
3. `tools/make_email.py YYYY-MM-DD` builds one email per employee. Sending needs Chris's OK.
4. Watch `/admin` and littledude's inbox for SUBMITTED records. Download `/admin/log.csv` for the archive.

## Local test
`python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && tests/local_test.sh`
(runs gunicorn on 127.0.0.1:10077 with `DATA_DIR=./local-data`, `EMAIL_MODE=dryrun`).

## Cost
Starter web service about $7/mo, plus a 1 GB disk at about $0.25/mo. Custom domain and TLS are free. Note that a service with a disk runs as a single instance, and each deploy has a few seconds of downtime.
