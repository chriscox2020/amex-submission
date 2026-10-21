"""Cox Research - American Express employee verification page.
One page per employee per statement, reached by an unguessable token link.
  Flag   = the red button opens a Note/Reason popup (five presets plus a typed
           note, 500 characters). Save stores the note, flags the row, closes
           the popup, and moves to the next charge. Clicking a flagged row
           again clears it. Credits cannot be flagged or recategorized.
  Submit = one click, approves all unflagged rows, appends to the log on the
           persistent disk, and emails a record to RECORD_TO automatically.
           Each flagged line is charge date, merchant, amount, flagged date,
           reference (or "ref pending"), then the note.
Charge review stays in this app. An employee's Submit writes the log and the record email only.
Chris and Meghan have a separate summary page. Sending that summary emails Littledude.
This server does not call QuickBooks or Dropbox.
"""
import csv, hashlib, hmac, html, io, json, os, smtplib, ssl, threading, base64
from datetime import datetime
from email.message import EmailMessage
from zoneinfo import ZoneInfo
from flask import Flask, request, abort, Response, jsonify, redirect
import summary

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get('DATA_DIR', '/var/data')
LOG = os.path.join(DATA_DIR, 'log.jsonl')
OUTBOX = os.path.join(DATA_DIR, 'outbox')
TOKEN_SECRET = os.environ.get('TOKEN_SECRET', '')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', '')
PUBLIC_BASE = os.environ.get('PUBLIC_BASE', 'https://amex.coxresearch.com').rstrip('/')
EMAIL_MODE = os.environ.get('EMAIL_MODE', 'dryrun')          # dryrun | smtp | resend
# Submission records. A stale littledude@coxresearch.com env value is rewritten
# so the live service still delivers to this address.
RECORD_ADDRESS = 'littledude@mail.grokbot.com'
def normalize_record_to(raw):
    raw = (raw or '').strip()
    if raw in ('', 'littledude@coxresearch.com'):
        return RECORD_ADDRESS
    return raw
RECORD_TO = normalize_record_to(os.environ.get('RECORD_TO', ''))
RECORD_CC = os.environ.get('RECORD_CC', '')                   # optional, e.g. chris@coxresearch.com
# PERMANENT (Chris, Oct 21 2026): Alice E Cox's page link and verification email
# ALWAYS go to this address, never alice@. Do not change on go-live or later.
ALICE_SEND_TO = 'chris@coxresearch.com'
# Notices to the owners. The per-employee record still goes only to RECORD_TO.
CHRIS_EMAIL = 'chris@coxresearch.com'
MEGHAN_EMAIL = (os.environ.get('MEGHAN_EMAIL') or 'meghan@coxresearch.com').strip() or 'meghan@coxresearch.com'
APPROVAL_NOTE = "Approved by Chris on employee's behalf"
MAIL_FROM = os.environ.get('MAIL_FROM', 'Chris AI <littledude@coxresearch.com>')
TZ = ZoneInfo('America/Chicago')
LOCK = threading.Lock()
SUBMIT_LOCK = threading.Lock()   # run gunicorn with 1 worker so these locks are global
app = Flask(__name__)
os.makedirs(OUTBOX, exist_ok=True)

# ---------- statements & tokens ----------
def bundle_rev(doc):
    try:
        return int((doc or {}).get('bundle_rev') or 0)
    except (TypeError, ValueError):
        return 0

def _norm_name(value):
    return ' '.join(str(value or '').upper().split())

def is_alice(emp):
    """Alice E Cox, matched by id or name. Not a substring of any other person."""
    if not isinstance(emp, dict):
        return False
    if str(emp.get('id') or '').lower() == 'alice-cox':
        return True
    return _norm_name(emp.get('name')) in ('ALICE E COX', 'ALICE COX')

def lock_routing(doc):
    """Force Alice's send-to. An uploaded email of alice@ cannot override this."""
    if not isinstance(doc, dict):
        return doc
    for e in doc.get('employees') or []:
        if is_alice(e):
            e['email'] = ALICE_SEND_TO
            e['send_to'] = ALICE_SEND_TO
    return doc

def _load_stmt_dir(d):
    """JSON files directly in d. Subdirectories (statements/bundled) are separate."""
    out = {}
    if not os.path.isdir(d):
        return out
    for f in sorted(os.listdir(d)):
        path = os.path.join(d, f)
        if f.endswith('.json') and os.path.isfile(path):
            with open(path) as fh:
                doc = json.load(fh)
            if isinstance(doc, dict) and doc.get('stmt'):
                out[doc['stmt']] = doc
    return out

def load_statements():
    """Bundled, repo, and disk statements. The highest bundle_rev wins.

    A disk file with no bundle_rev (0) loses to the bundled Sep 22 statement,
    and that bundled copy is written onto the disk so the next boot agrees.
    /admin/upload sets a higher bundle_rev, so a later upload still wins.
    """
    bundled = _load_stmt_dir(os.path.join(APP_DIR, 'statements', 'bundled'))
    repo = _load_stmt_dir(os.path.join(APP_DIR, 'statements'))
    disk_dir = os.path.join(DATA_DIR, 'statements')
    disk = _load_stmt_dir(disk_dir)
    out = {}
    for key in set(bundled) | set(repo) | set(disk):
        cands = []
        for i, src in enumerate((bundled.get(key), repo.get(key), disk.get(key))):
            if src:
                cands.append((bundle_rev(src), i, src))
        chosen = cands[-1][2]
        b = bundled.get(key)
        if b and bundle_rev(b) > bundle_rev(disk.get(key)):
            chosen = b
            lock_routing(chosen)
            os.makedirs(disk_dir, exist_ok=True)
            with open(os.path.join(disk_dir, key + '.json'), 'w') as fh:
                json.dump(chosen, fh, indent=1)
                fh.write('\n')
        else:
            lock_routing(chosen)
        out[key] = chosen
    return out

def make_token(stmt, emp_id):
    if not TOKEN_SECRET: raise RuntimeError('TOKEN_SECRET not set')
    mac = hmac.new(TOKEN_SECRET.encode(), f'{stmt}|{emp_id}'.encode(), hashlib.sha256).digest()[:18]
    return base64.urlsafe_b64encode(mac).decode().rstrip('=')   # 144-bit, 24 chars

def resolve(token):
    for stmt, doc in load_statements().items():
        for e in doc['employees']:
            if hmac.compare_digest(make_token(stmt, e['id']), token):
                return doc, e
    return None, None

# ---------- log (append-only JSONL on the disk) ----------
def now(): return datetime.now(TZ).isoformat(timespec='seconds')

def append(rec):
    with LOCK, open(LOG, 'a') as f:
        f.write(json.dumps(rec) + '\n'); f.flush(); os.fsync(f.fileno())

def read_log():
    if not os.path.exists(LOG): return []
    out = []
    for line in open(LOG):
        try: out.append(json.loads(line))
        except Exception: pass
    return out

def state(stmt, emp_id):
    """Flags (row id -> {note, ts}), submit snapshot, overall comment, category choices.

    A dict still supports the `in` and `len` checks callers already use.
    Log lines written before notes existed have no note key; those stay valid.
    A category choice is recorded only after the employee uses the dropdown.
    """
    flags, submitted, comment, categories = {}, None, '', {}
    for r in read_log():
        if r.get('stmt') != stmt or r.get('emp_id') != emp_id: continue
        if r['type'] == 'flag':
            flags[r['row']] = {'note': r.get('note') or '', 'ts': r.get('ts') or ''}
        elif r['type'] == 'unflag': flags.pop(r.get('row'), None)
        elif r['type'] == 'comment': comment = r.get('comment') or ''
        elif r['type'] == 'category': categories[r['row']] = r.get('category') or ''
        elif r['type'] == 'submit': submitted = r
        elif r['type'] == 'admin_approve':
            flags, comment, submitted = {}, '', r
        elif r['type'] == 'reset': flags, submitted, comment, categories = {}, None, '', {}
    return flags, submitted, comment, categories

def qb_record(stmt):
    """The month lock written when Chris or Meghan send the summary for posting."""
    found = None
    for r in read_log():
        if r.get('type') == 'qb_submit' and r.get('stmt') == stmt:
            found = r
    return found

def month_locked_response(stmt):
    if not qb_record(stmt):
        return None
    if 'application/json' in request.headers.get('Accept', ''):
        return jsonify(error='month locked'), 409
    return Response('This month was sent for posting', 409)

# ---------- email ----------
def money(a): return f'${float(a):,.2f}'

def money_signed(a):
    a = float(a)
    if a < 0:
        return '&minus;' + money(abs(a))
    return money(a)

def sorted_rows(rows):
    def key(r):
        try:
            dt = datetime.strptime(str(r.get('date') or ''), '%m/%d/%Y')
        except Exception:
            dt = datetime.min
        try:
            rid = int(r.get('id') or 0)
        except (TypeError, ValueError):
            rid = 0
        return (dt, rid)
    return sorted(rows, key=key)

NOTE_MAX = 500
REF_KEYS = ('reference_number', 'reference', 'ref', 'ref_number', 'ref_no', 'referenceNumber')
AMEX_CATEGORIES = ('Merchandise & Supplies', 'Transportation', 'Restaurant', 'Travel',
                   'Business Services', 'Other', 'Entertainment', 'Communications')

def clean_note(raw):
    """Optional flag reason: one line, trimmed, capped at NOTE_MAX characters."""
    if raw is None: return ''
    s = str(raw).replace('\r\n', '\n').replace('\r', '\n').replace('\n', ' ').replace('\t', ' ')
    s = ''.join(ch for ch in s if ord(ch) >= 32).strip()
    if len(s) > NOTE_MAX: s = s[:NOTE_MAX].rstrip()
    return s

def charge_ref(row):
    """Reference number on the charge, or 'ref pending' when the row has none."""
    if isinstance(row, dict):
        for k in REF_KEYS:
            v = row.get(k)
            if v is not None and str(v).strip():
                return str(v).strip()
    return 'ref pending'

def flag_day(ts):
    if not ts: return ''
    try:
        dt = datetime.fromisoformat(str(ts))
        if dt.tzinfo is None: dt = dt.replace(tzinfo=TZ)
        return dt.astimezone(TZ).strftime('%m/%d/%Y')
    except Exception:
        return str(ts)[:10]

def flag_line(row, note, flagged_ts):
    """charge date | merchant | amount | flagged date | reference | Note: text

    The note is omitted when blank. Callers HTML-escape the whole line.
    """
    parts = [str(row.get('date') or ''), str(row.get('company') or ''), money(row.get('amount') or 0),
             flag_day(flagged_ts), charge_ref(row)]
    line = ' | '.join(parts)
    note = clean_note(note)
    if note: line += ' | Note: ' + note
    return line

def flag_meta(flags, row_id):
    if isinstance(flags, dict):
        info = flags.get(row_id)
        if isinstance(info, dict): return info
    return {}

def attach_flag_meta(rows, flags):
    out = []
    for r in rows:
        item = dict(r)
        info = flag_meta(flags, r['id'])
        item['note'] = info.get('note') or ''
        item['flagged_ts'] = info.get('ts') or ''
        out.append(item)
    return out

def flag_items_html(rows, flags):
    items = []
    for r in rows:
        if r['id'] not in flags: continue
        info = flag_meta(flags, r['id'])
        line = flag_line(r, info.get('note', ''), info.get('ts', ''))
        items.append(f"<li data-row='{int(r['id'])}'>{html.escape(line)}</li>")
    return ''.join(items)

def posted_field(name):
    """Form or JSON field. None when the field was not sent. Never from the query string."""
    data = request.get_json(silent=True)
    if isinstance(data, dict) and name in data:
        return data.get(name)
    if name in request.form:
        return request.form.get(name)
    return None

FLAG_REASONS = ('Wrong category', 'Wrong price', 'Duplicate charge', 'Not my charge', 'Other')

def is_credit_row(row):
    try:
        return float(row.get('amount') or 0) < 0
    except (TypeError, ValueError):
        return False

def posted_note():
    """Optional flag note. A preset reason and a typed note are joined as 'Reason: text'."""
    raw = posted_field('note')
    reason_raw = posted_field('reason')
    note = '' if raw is None else clean_note(raw)
    if reason_raw is None:
        return note
    reason = str(reason_raw).strip()
    if reason not in FLAG_REASONS:
        reason = ''
    if reason and note:
        return clean_note(reason + ': ' + note)
    return reason or note

def canonical_category(value):
    key = str(value or '').strip().casefold()
    if not key: return ''
    for c in AMEX_CATEGORIES:
        if c.casefold() == key: return c
    return ''

def chosen_category(row, categories):
    """Dropdown choice when the employee set one, otherwise a matching original category."""
    if isinstance(categories, dict) and row.get('id') in categories:
        return categories[row['id']] or ''
    return canonical_category(row.get('category'))

def flag_comment_line(comment):
    comment = clean_note(comment)
    if not comment: return ''
    return 'Flagged charges: ' + comment

def record_email(doc, e, flagged, approved, ts, comment=''):
    # The month folder name lets Littledude file this record. This email does not
    # upload anywhere and does not call QuickBooks or Dropbox.
    folder = summary.statement_period(doc)['folder_label']
    subj = f"Amex verification SUBMITTED: {e['name']} - {folder} - {len(flagged)} flagged"
    def tbl(rows):
        if not rows: return '<p><i>None</i></p>'
        t = ''.join(f"<tr><td style='padding:4px 8px'>{r['date']}</td><td style='padding:4px 8px'>{html.escape(r['category'])}</td>"
                    f"<td style='padding:4px 8px'>{html.escape(r['company'])}</td><td style='padding:4px 8px;text-align:right'>{money(r['amount'])}</td>"
                    f"<td style='padding:4px 8px'>{html.escape(r.get('amex_category') or '')}</td></tr>" for r in rows)
        return ("<table style='border-collapse:collapse;font-size:13px' border='1' cellspacing='0'><tr><th>Date</th><th>Description</th>"
                f"<th>Company</th><th>Amount</th><th>Category</th></tr>{t}</table>")
    def flag_html(rows):
        if not rows: return '<p><i>None</i></p>'
        return ''.join("<p style='margin:4px 0;font-size:13px'>" + html.escape(flag_line(r, r.get('note', ''), r.get('flagged_ts', ''))) + "</p>" for r in rows)
    ft = sum(r['amount'] for r in flagged); at = sum(r['amount'] for r in approved)
    comment_line = flag_comment_line(comment)
    comment_html = f"<p style='margin:8px 0;font-size:13px'>{html.escape(comment_line)}</p>" if comment_line else ''
    comment_text = comment_line + '\n' if comment_line else ''
    body = (f"<div style='font-family:Arial,Helvetica,sans-serif'><p><b>{html.escape(e['name'])}</b> submitted the {html.escape(doc['label'])} "
            f"(statement {doc['stmt']}) on {nice(ts)}.</p>"
            f"<p><b>Flagged - needs review before the bill is paid ({len(flagged)}, {money(ft)}):</b></p>{flag_html(flagged)}{comment_html}"
            f"<p><b>Approved ({len(approved)}, {money(at)}):</b></p>{tbl(approved)}"
            f"<p style='color:#777;font-size:12px'>Automatic record from {PUBLIC_BASE}. Saved to the log on the server.</p></div>")
    text = (f"{e['name']} submitted {doc['label']} ({doc['stmt']}) at {ts}.\nFlagged ({len(flagged)}, {money(ft)}):\n" +
            ''.join(f"  {flag_line(r, r.get('note', ''), r.get('flagged_ts', ''))}\n" for r in flagged) +
            comment_text +
            f"Approved ({len(approved)}, {money(at)}).\n")
    return subj, body, text

def send_prepared(msg):
    """Deliver an already built message. Dry-run writes it to the outbox and sends nothing."""
    if EMAIL_MODE == 'smtp':
        host = os.environ.get('SMTP_HOST', 'smtp.office365.com'); port = int(os.environ.get('SMTP_PORT', '587'))
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls(context=ssl.create_default_context())
            s.login(os.environ['SMTP_USER'], os.environ['SMTP_PASSWORD'])
            s.send_message(msg)
        return 'sent-smtp'
    if EMAIL_MODE == 'resend':
        import urllib.request
        text = html_body = None
        attachments = []
        for part in msg.walk():
            if part.get_content_maintype() == 'multipart':
                continue
            filename = part.get_filename()
            payload = part.get_payload(decode=True) or b''
            if filename:
                attachments.append({'filename': filename, 'content': base64.b64encode(payload).decode()})
            elif part.get_content_type() == 'text/plain' and text is None:
                text = payload.decode('utf-8', 'replace')
            elif part.get_content_type() == 'text/html' and html_body is None:
                html_body = payload.decode('utf-8', 'replace')
        to = [x.strip() for x in str(msg['To'] or '').split(',') if x.strip()]
        cc = [x.strip() for x in str(msg['Cc'] or '').split(',') if x.strip()]
        data = {'from': MAIL_FROM, 'to': to, 'cc': cc, 'subject': str(msg['Subject'] or ''),
                'html': html_body or '', 'text': text or ''}
        if attachments:
            data['attachments'] = attachments
        req = urllib.request.Request('https://api.resend.com/emails', method='POST',
            data=json.dumps(data).encode(),
            headers={'Authorization': 'Bearer ' + os.environ['RESEND_API_KEY'], 'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=30) as r: r.read()
        return 'sent-resend'
    raw = bytes(msg)
    fn = os.path.join(OUTBOX, datetime.now(TZ).strftime('%Y%m%d-%H%M%S-') + hashlib.sha1(raw).hexdigest()[:8] + '.eml')
    with open(fn, 'wb') as fh:
        fh.write(raw)
    return 'dryrun:' + fn

def send_email(subj, body, text):
    """The per-employee record. Only RECORD_TO, never a copy to Chris or anyone else."""
    to = [x.strip() for x in RECORD_TO.split(',') if x.strip()]
    msg = EmailMessage(); msg['Subject'] = subj; msg['From'] = MAIL_FROM; msg['To'] = ', '.join(to)
    msg.set_content(text); msg.add_alternative(body, subtype='html')
    return send_prepared(msg)

def all_submitted_notice(stmt):
    found = None
    for r in read_log():
        if r.get('type') == 'all_submitted_notice' and r.get('stmt') == stmt:
            found = r
    return found

def everyone_submitted(doc):
    people = doc.get('employees') or []
    if not people:
        return False
    for emp in people:
        _flags, submitted, _comment, _categories = state(doc['stmt'], emp['id'])
        if not submitted:
            return False
    return True

def owner_summary_bodies(built):
    """Category totals for Chris and Meghan. No per-charge or per-person detail."""
    lines = [f"All cardholders have submitted {built['period']['folder_label']}.",
             f"Statement close date: {built['period']['close_date']}.", '']
    for name, cents in built['categories']:
        lines.append(f"{name}: {summary.format_money(cents)}")
    lines += ['',
              f"Grand total: {summary.format_money(built['summary_cents'])}",
              f"Statement total: {summary.format_money(built['statement_cents'])}"]
    if built['difference_cents'] != 0:
        lines.append(
            f"Summary total {summary.format_money(built['summary_cents'])} does not equal the statement total "
            f"{summary.format_money(built['statement_cents'])}. "
            f"Difference: {summary.format_money(built['difference_cents'])}.")
    link = f"{PUBLIC_BASE}/admin/summary/{built['stmt']}"
    lines += ['', link]
    text = '\n'.join(lines) + '\n'
    html_rows = ''.join(
        f"<tr><td>{html.escape(name)}</td><td style='text-align:right'>{summary.format_money(cents)}</td></tr>"
        for name, cents in built['categories'])
    warning = ''
    if built['difference_cents'] != 0:
        warning = ("<p style='color:#c62828'><b>"
                   f"Summary total {summary.format_money(built['summary_cents'])} does not equal the statement total "
                   f"{summary.format_money(built['statement_cents'])}. "
                   f"Difference: {summary.format_money(built['difference_cents'])}.</b></p>")
    body = ("<div style='font-family:Arial,Helvetica,sans-serif'>"
            f"<p>All cardholders have submitted {html.escape(built['period']['folder_label'])}.</p>"
            f"<p>Statement close date: {html.escape(built['period']['close_date'])}.</p>"
            f"<table border='1' cellspacing='0' cellpadding='6'><tr><th>Category</th><th>Amount</th></tr>{html_rows}"
            f"<tr><td><b>Grand total</b></td><td style='text-align:right'><b>{summary.format_money(built['summary_cents'])}</b></td></tr>"
            f"<tr><td>Statement total</td><td style='text-align:right'>{summary.format_money(built['statement_cents'])}</td></tr></table>"
            f"{warning}<p><a href='{html.escape(link)}'>{html.escape(link)}</a></p></div>")
    return text, body

def owner_summary_email(built, addr):
    text, html_body = owner_summary_bodies(built)
    msg = EmailMessage()
    msg['Subject'] = f"All submitted: {built['period']['folder_label']}"
    msg['From'] = MAIL_FROM
    msg['To'] = addr
    msg.set_content(text)
    msg.add_alternative(html_body, subtype='html')
    return msg

def maybe_send_all_submitted(doc):
    """One email to Chris and one to Meghan, once per statement, after the last cardholder is in."""
    if not everyone_submitted(doc) or all_submitted_notice(doc['stmt']):
        return
    built = summary.build_summary(doc, state)
    try:
        statuses = [send_prepared(owner_summary_email(built, addr)) for addr in (CHRIS_EMAIL, MEGHAN_EMAIL)]
    except Exception as ex:
        append({'type': 'email', 'stmt': doc['stmt'], 'ts': now(), 'to': CHRIS_EMAIL,
                'status': f'error: {type(ex).__name__}: {ex}'})
        return
    append({'type': 'all_submitted_notice', 'stmt': doc['stmt'], 'ts': now(),
            'to': [CHRIS_EMAIL, MEGHAN_EMAIL], 'email_status': statuses})

def pending_people(doc):
    missing = []
    submitted = 0
    for emp in doc.get('employees') or []:
        _flags, sub, _comment, _categories = state(doc['stmt'], emp['id'])
        if sub:
            submitted += 1
        else:
            missing.append(emp)
    return missing, submitted

def pending_reminder_email(doc, missing):
    period = summary.statement_period(doc)
    month = period['month_label']
    text_lines = [f"These cardholders have not submitted {month}.", '']
    html_parts = [f"<p>These cardholders have not submitted {html.escape(month)}.</p>"]
    if not missing:
        text_lines.append('Everyone has submitted.')
        html_parts.append('<p>Everyone has submitted.</p>')
    for emp in missing:
        name = emp.get('name') or emp['id']
        url = f"{PUBLIC_BASE}/admin/summary/{doc['stmt']}/approve/{emp['id']}"
        text_lines.append(name)
        text_lines.append(f"Approve {name}'s charges: {url}")
        text_lines.append(f"Or reply APPROVE {name} (or APPROVE ALL).")
        text_lines.append('')
        html_parts.append(
            f"<p>{html.escape(name)}<br>"
            f"<a href='{html.escape(url, quote=True)}'>Approve {html.escape(name)}'s charges</a><br>"
            f"Or reply APPROVE {html.escape(name)} (or APPROVE ALL).</p>")
    msg = EmailMessage()
    msg['Subject'] = f"Amex {month}: not yet submitted"
    msg['From'] = MAIL_FROM
    msg['To'] = CHRIS_EMAIL
    msg['Reply-To'] = RECORD_ADDRESS
    msg.set_content('\n'.join(text_lines).rstrip() + '\n')
    msg.add_alternative(''.join(html_parts), subtype='html')
    return msg

# ---------- page ----------
CSS = """body{margin:0;background:#fff;color:#222;font-family:Arial,Helvetica,sans-serif;font-size:20px}
.banner{background:#006fcf;color:#fff;font-size:44px;font-weight:bold;text-align:center;padding:22px 12px;margin:0 0 18px;width:100%;box-sizing:border-box;-webkit-print-color-adjust:exact;print-color-adjust:exact}
h1{font-size:36px;margin:0 18px 16px;text-align:left;font-weight:bold}
.sheet{margin:0 18px 40px}
table{border-collapse:collapse;width:100%}td,th{border:1px solid #999;padding:8px 10px;vertical-align:top}th{background:#e8e8e8;text-align:left}
.n{text-align:right;white-space:nowrap}.c{text-align:center}.fc{position:relative;width:70px}
tr.f td{background:#ffecec}tr.ed td{background:#e3f0ff}tr.cr td{background:#f3fbf3}tr.nx td{background:#fff7cc}
.lbl{color:#c00;font-weight:bold;font-size:15px}
.fb{font-size:22px;padding:2px 10px;background:#fff;border:2px solid #d00;color:#d00;border-radius:4px;cursor:pointer}
.fb.on{background:#d00;color:#fff}button:disabled{opacity:.6;cursor:default}
.pop{position:fixed;left:50%;top:12%;transform:translateX(-50%);width:min(380px,calc(100vw - 24px));z-index:20;text-align:left;border:2px solid #333;background:#fff;padding:8px;box-shadow:2px 2px 8px #888}
.pop[hidden]{display:none!important}
.chips{margin:8px 0;display:flex;flex-wrap:wrap;gap:8px}.pop .chip{display:inline-block;margin:0;font-weight:normal;font-size:17px;border:1px solid #888;border-radius:16px;padding:4px 10px;cursor:pointer}
.sv{font-size:18px;padding:4px 16px;cursor:pointer}.pop textarea{width:95%;height:50px;font-size:18px;display:block;margin:6px 0}
label.boxlabel{display:block;font-weight:bold;margin:24px 0 6px}
#fcb{width:100%;box-sizing:border-box;height:110px;font-size:20px;border:2px solid #333;padding:8px}
#flaglist{margin:16px 0 0;padding-left:18px}#flaglist li,.done li{margin:6px 0;font-size:15px;line-height:1.4}
td.cat{position:relative;cursor:pointer}td.cat:hover{outline:2px solid #69c}
.edited{font-weight:bold}.ed-cat{color:#06c}.ed-note{color:#d00}.rsn{font-size:16px;margin-top:2px}
s.orig{display:block;font-size:14px;color:#888;font-weight:normal}
.dd{position:absolute;left:0;top:100%;z-index:6;width:min(300px,80vw);background:#fff;border:2px solid #333;box-shadow:2px 2px 8px #888;text-align:left}
.ddh{font-weight:bold;padding:6px 10px;background:#eee;font-size:17px}.ddo{padding:6px 10px;font-size:18px;border-top:1px solid #ddd;cursor:pointer}
.ddo:hover{background:#e3f0ff}.ddo.cur{font-weight:bold}.ddo.cx{color:#a00}
.crn{color:#080;font-weight:bold}.crl{font-size:14px;color:#080;font-weight:bold;border:1px solid #080;border-radius:4px;padding:1px 6px;margin-left:6px}
tr.ft td{background:#f6f6f6;font-size:22px}tr.net td{font-weight:bold;font-size:24px;background:#eaeaea}
.done{background:#e8f5e9;border:1px solid #2e7d32;border-radius:8px;padding:14px 16px;font-size:16px;margin:0 0 16px}
.sub{margin-top:24px;font-size:28px;padding:14px 60px;background:#0a5;color:#fff;border:0;border-radius:6px;cursor:pointer}
.prt{margin-top:24px;margin-left:12px;font-size:28px;padding:14px 50px;background:#fff;color:#333;border:2px solid #333;border-radius:6px;cursor:pointer}
@media(max-width:700px){body{font-size:15px}.banner{font-size:28px;padding:16px 8px}h1{font-size:26px;margin:0 8px 12px}.sheet{margin:0 8px 24px}
td,th{padding:6px}.sub,.prt{font-size:20px;padding:12px 22px}#fcb{height:80px;font-size:16px}tr.ft td,tr.net td{font-size:16px}}
@media print{button,.pop,.dd{display:none!important}body{margin:10mm;font-size:12pt}.banner{font-size:22pt}
tr{page-break-inside:avoid}td,th{padding:4px 6px}#fcb{height:60px}tr.ft td{font-size:12pt}tr.net td{font-size:13pt}
*{-webkit-print-color-adjust:exact;print-color-adjust:exact}}
"""

JS = r"""var CATS=__CATS__;
function closeDD(){document.querySelectorAll('.dd').forEach(function(d){d.remove()})}
function markEdited(el,v,orig,cls){
  if(v===orig){el.textContent=orig;return false}
  el.textContent='';
  var n=document.createElement('span');n.className='edited '+cls;n.textContent=v;el.appendChild(n);
  if(orig){var s=document.createElement('s');s.className='orig';s.textContent=orig;el.appendChild(s)}
  return true}
function openCat(td,ev){
  if(ev.target.closest('.dd'))return;
  closeDD();
  var d=document.createElement('div');d.className='dd';
  var h=document.createElement('div');h.className='ddh';h.textContent='Pick category';d.appendChild(h);
  CATS.forEach(function(o){
    var item=document.createElement('div');
    item.className='ddo'+(o===(td.getAttribute('data-cur')||'')?' cur':'');
    item.textContent=o;d.appendChild(item)});
  var cx=document.createElement('div');cx.className='ddo cx';cx.textContent='Cancel';d.appendChild(cx);
  d.addEventListener('click',function(e){
    e.stopPropagation();
    var o=e.target.closest('.ddo');if(!o)return;
    if(!o.classList.contains('cx'))setCat(td,o.textContent);
    closeDD()});
  td.appendChild(d)}
function setCat(td,v){
  var orig=td.getAttribute('data-orig')||'';
  fetch(td.getAttribute('data-action'),{method:'POST',headers:{'Accept':'application/json','Content-Type':'application/x-www-form-urlencoded'},body:'category='+encodeURIComponent(v)}).then(function(r){
    if(!r.ok)return;
    var changed=markEdited(td,v,orig,'ed-cat');
    td.setAttribute('data-cur',v);
    td.parentNode.classList.toggle('ed',!!changed);
  })}
document.addEventListener('click',function(e){if(!e.target.closest('td.cat'))closeDD()});
var pop=document.getElementById('pop');
function closePop(){pop.hidden=true;if(pop.parentNode!==document.body)document.body.appendChild(pop)}
function paintMerchant(tr,flagged,note){
  var m=tr.cells[1];var name=tr.getAttribute('data-merchant')||'';
  m.textContent='';
  if(flagged){var lbl=document.createElement('span');lbl.className='lbl';lbl.textContent='Flag charge';m.appendChild(lbl);m.appendChild(document.createTextNode(' '))}
  m.appendChild(document.createTextNode(name));
  if(tr.classList.contains('cr')){m.appendChild(document.createTextNode(' '));var b=document.createElement('span');b.className='crl';b.textContent='Credit';m.appendChild(b)}
  if(flagged&&note){var rd=document.createElement('div');rd.className='rsn';var n=document.createElement('span');n.className='edited ed-note';n.textContent=note;rd.appendChild(n);m.appendChild(rd)}}
function placeLi(list,li,row){
  var kids=list.children;
  for(var i=0;i<kids.length;i++){if(parseInt(kids[i].getAttribute('data-row'),10)>row){list.insertBefore(li,kids[i]);return}}
  list.appendChild(li)}
function applyFlag(btn,tr,j){
  btn.classList.toggle('on',!!j.flagged);
  btn.textContent=j.flagged?'\u2691':'\u2690';
  btn.title=j.flagged?'Flagged':'Flag';
  tr.classList.toggle('f',!!j.flagged);
  paintMerchant(tr,!!j.flagged,j.note||'');
  var list=document.getElementById('flaglist');
  if(list){
    var li=list.querySelector('li[data-row="'+j.row+'"]');
    if(j.flagged){if(!li){li=document.createElement('li');li.setAttribute('data-row',String(j.row));placeLi(list,li,j.row)}li.textContent=j.line||''}
    else if(li)li.remove()}}
function postFlag(btn,reason,note,saving){
  var tr=btn.closest('tr');btn.disabled=true;
  var opts={method:'POST',headers:{'Accept':'application/json'}};
  if(saving){opts.headers['Content-Type']='application/x-www-form-urlencoded';opts.body='reason='+encodeURIComponent(reason||'')+'&note='+encodeURIComponent(note||'')}
  fetch(btn.getAttribute('data-action'),opts).then(function(r){
    return r.json().then(function(j){return {ok:r.ok,j:j}},function(){throw new Error('bad')});
  }).then(function(x){
    btn.disabled=false;if(!x.ok)return;
    applyFlag(btn,tr,x.j);
    if(saving){closePop();
      document.querySelectorAll('tr.nx').forEach(function(n){n.classList.remove('nx')});
      var n=tr.nextElementSibling;while(n&&!n.getAttribute('data-id'))n=n.nextElementSibling;
      if(n){n.classList.add('nx');n.scrollIntoView({block:'center',behavior:'smooth'})}}
  }).catch(function(){btn.disabled=false})}
function openPop(btn){
  if(btn.disabled)return;
  if(btn.classList.contains('on')){postFlag(btn,null,null,false);return}
  var td=btn.parentNode;
  if(pop.parentNode===td&&!pop.hidden){closePop();return}
  pop.querySelectorAll('input[name=r]').forEach(function(x){x.checked=false});
  var preset=pop.querySelector('input[name=r]');if(preset)preset.checked=true;
  pop.querySelector('textarea').value='';
  td.appendChild(pop);pop.hidden=false;pop.querySelector('textarea').focus()}
function savePop(){
  if(pop.hidden||!pop.parentNode||!pop.parentNode.classList.contains('fc'))return;
  var btn=pop.parentNode.querySelector('.fb');if(!btn)return;
  var reason='';pop.querySelectorAll('input[name=r]').forEach(function(x){if(x.checked)reason=x.value});
  postFlag(btn,reason,pop.querySelector('textarea').value.slice(0,500),true)}
document.querySelectorAll('button.fb').forEach(function(b){b.addEventListener('click',function(ev){ev.preventDefault();ev.stopPropagation();openPop(b)})});
document.getElementById('popsave').addEventListener('click',function(ev){ev.preventDefault();ev.stopPropagation();savePop()});
document.addEventListener('keydown',function(ev){if(ev.key==='Escape'){if(!pop.hidden){ev.preventDefault();closePop()}closeDD()}});
var commentBox=document.getElementById('fcb');
if(commentBox&&commentBox.getAttribute('data-action')){
  var commentTimer=null;
  commentBox.addEventListener('input',function(){
    clearTimeout(commentTimer);
    commentTimer=setTimeout(function(){
      fetch(commentBox.getAttribute('data-action'),{method:'POST',headers:{'Accept':'application/json','Content-Type':'application/x-www-form-urlencoded'},body:'comment='+encodeURIComponent(commentBox.value.slice(0,500))}).catch(function(){});
    },400)})}
"""

def category_view(row, categories):
    """Current Amex category, the statement original, and whether the employee changed it."""
    original = str(row.get('category') or '')
    explicit = isinstance(categories, dict) and row.get('id') in categories
    if explicit:
        current = categories.get(row['id']) or ''
    else:
        current = canonical_category(original) or original
    baseline = canonical_category(original) or original
    edited = bool(explicit and (current or '') != baseline)
    return current, original, edited

def popup_html():
    chips = []
    for i, reason in enumerate(FLAG_REASONS):
        checked = ' checked' if i == 0 else ''
        chips.append(f"<label class='chip'><input type='radio' name='r' value='{html.escape(reason)}'{checked}> {html.escape(reason)}</label>")
    return ("<div class='pop' id='pop' hidden role='dialog' aria-modal='true' aria-labelledby='poptitle'>"
            "<b id='poptitle'>Note/Reason</b><div class='chips'>" + ''.join(chips) + "</div>"
            "<textarea id='popnote' maxlength='500' placeholder='Type your own note'></textarea>"
            "<button type='button' class='sv' id='popsave'>Save</button></div>")

def render(doc, e, token, flags, submitted, comment='', categories=None):
    """One cardholder only. Print uses this same HTML, so it cannot include anyone else."""
    esc = html.escape
    others = [p for p in (doc.get('employees') or []) if p.get('id') != e.get('id')]
    if others:
        raise RuntimeError('employee page was given another cardholder')
    rows = sorted_rows(e.get('rows') or [])
    locked = submitted is not None
    categories = categories or {}
    if submitted is not None and 'comment' in submitted:
        comment = submitted.get('comment') or ''
    lis = flag_items_html(rows, flags)
    charges = [r for r in rows if not is_credit_row(r)]
    credits = [r for r in rows if is_credit_row(r)]
    tot_ch = sum(float(r['amount']) for r in charges)
    tot_cr = sum(float(r['amount']) for r in credits)
    credit_cell = f"<span class='crn'>{money_signed(tot_cr)}</span>" if credits else 'None'
    parts = [f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
             f"<meta name='robots' content='noindex'><title>American Express Charges - {esc(e['name'])}</title><style>{CSS}</style></head>"
             f"<body data-employee='{esc(e['id'])}'>"
             f"<div class='banner'>American Express Charges</div><h1>{esc(e['name'])}</h1><div class='sheet'>"]
    if locked:
        listed = f"<ul id='flaglist'>{lis}</ul>" if lis else ''
        comment_html = f"<p style='margin:8px 0 0'>{esc(flag_comment_line(comment))}</p>" if flag_comment_line(comment) else ''
        parts.append(f"<div class='done'><b>Submitted {esc(nice(submitted['ts']))}.</b> All unflagged charges approved. "
                     f"{len(submitted['flagged'])} flagged for review. Thank you.{listed}{comment_html}</div>")
    parts.append("<table><tr><th>Date</th><th>Merchant</th><th>Amount</th><th>Amex Category</th><th>Flag</th></tr>")
    for r in rows:
        rid = int(r['id'])
        credit = is_credit_row(r)
        on = (not credit) and (rid in flags)
        current, original, edited = category_view(r, categories)
        shown = current or original
        if edited and not credit:
            strike = f"<s class='orig'>{esc(original)}</s>" if original and original != current else ''
            inner = f"<span class='edited ed-cat'>{esc(current)}</span>{strike}"
        else:
            inner = esc(shown)
        cat_cls = '' if credit or locked else " class='cat'"
        cat_click = '' if credit or locked else " onclick='openCat(this,event)'"
        cat_action = '' if credit or locked else f" data-action='/v/{token}/category/{rid}'"
        tr_cls = []
        if credit: tr_cls.append('cr')
        if on: tr_cls.append('f')
        if edited and not credit: tr_cls.append('ed')
        cls_attr = f" class='{' '.join(tr_cls)}'" if tr_cls else ''
        merchant = []
        if on:
            merchant.append("<span class='lbl'>Flag charge</span> ")
        merchant.append(esc(r['company']))
        if credit:
            merchant.append(" <span class='crl'>Credit</span>")
        if on:
            note = flag_meta(flags, rid).get('note') or ''
            if note:
                merchant.append(f"<div class='rsn'><span class='edited ed-note'>{esc(note)}</span></div>")
        if credit:
            amount = f"<span class='crn'>{money_signed(r['amount'])}</span>"
            flag = ''
        else:
            amount = f"<b>{money(r['amount'])}</b>"
            dis = ' disabled' if locked else ''
            flag = (f"<button type='button' class='fb{' on' if on else ''}' data-action='/v/{token}/flag/{rid}' "
                    f"title='{'Flagged' if on else 'Flag'}'{dis}>{'&#9873;' if on else '&#9872;'}</button>")
        parts.append(f"<tr{cls_attr} data-id='{rid}' data-merchant='{esc(r['company'])}'><td>{esc(r['date'])}</td>"
                     f"<td>{''.join(merchant)}</td><td class='n'>{amount}</td>"
                     f"<td{cat_cls} data-orig='{esc(original)}' data-cur='{esc(shown)}'{cat_action}{cat_click}>{inner}</td>"
                     f"<td class='c fc'>{flag}</td></tr>")
    parts.append(f"<tr class='ft'><td colspan='2'>Total charges</td><td class='n'>{money(tot_ch)}</td><td colspan='2'></td></tr>"
                 f"<tr class='ft'><td colspan='2'>Credits</td><td class='n'>{credit_cell}</td><td colspan='2'></td></tr>"
                 f"<tr class='ft net'><td colspan='2'>Net Total</td><td class='n'>{money_signed(tot_ch + tot_cr)}</td><td colspan='2'></td></tr></table>")
    if not locked:
        parts.append(f"<ul id='flaglist'>{lis}</ul>"
                     f"<form method='post' action='/v/{token}/submit'>"
                     f"<label class='boxlabel' for='fcb'>Flagged charges</label>"
                     f"<textarea id='fcb' name='comment' maxlength='500' data-action='/v/{token}/comment' "
                     f"placeholder='e.g. wrong category'>{esc(comment)}</textarea>"
                     f"<button class='sub' type='submit'>Submit</button></form>"
                     f"<button class='prt' type='button' onclick='window.print()'>Print</button>")
    else:
        parts.append("<button class='prt' type='button' onclick='window.print()'>Print</button>")
    script = JS.replace('__CATS__', json.dumps(list(AMEX_CATEGORIES)))
    parts.append(popup_html() + f"<script>{script}</script></div></body></html>")
    return ''.join(parts)
def nice(ts):
    return datetime.fromisoformat(ts).strftime('%b %-d, %Y %-I:%M %p') + ' CT'

def meta():
    return {'ts': now(), 'ip': request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[0].strip(),
            'ua': request.headers.get('User-Agent', '')[:300]}

@app.after_request
def stamp(resp):
    resp.headers['X-Amex-Page'] = 'review-banner'
    return resp

@app.get('/healthz')
def healthz(): return 'ok'

@app.get('/')
def root(): return Response('Cox Research', mimetype='text/plain')

@app.get('/v/<token>')
def view(token):
    doc, e = resolve(token)
    if not doc: abort(404)
    flags, sub, comment, categories = state(doc['stmt'], e['id'])
    append({'type': 'open', 'stmt': doc['stmt'], 'emp_id': e['id'], 'emp': e['name'], **meta()})
    # The token selects one employee. Drop every other cardholder before rendering or printing.
    solo = {k: v for k, v in doc.items() if k != 'employees'}
    solo['employees'] = [e]
    page = render(solo, e, token, flags, sub, comment, categories)
    act = request.args.get('do', ''); row = request.args.get('row', '')
    if not sub and act == 'flag' and row.isdigit() and int(row) not in flags:
        js = ("<script>window.addEventListener('load',function(){if(document.visibilityState!=='visible')return;"
              f"fetch('/v/{token}/flag/{int(row)}',{{method:'POST',headers:{{'Accept':'application/json'}}}}).then(function(){{location.replace('/v/{token}')}})}});</script>")
        page = page.replace('</body>', js + '</body>')
    elif not sub and act == 'submit':
        js = ("<script>window.addEventListener('load',function(){if(document.visibilityState!=='visible')return;"
              f"var f=document.createElement('form');f.method='post';f.action='/v/{token}/submit';document.body.appendChild(f);f.submit()}});</script>")
        page = page.replace('</body>', js + '</body>')
    return Response(page, headers={'Cache-Control': 'no-store', 'X-Robots-Tag': 'noindex'})

@app.post('/v/<token>/flag/<int:row>')
def flag(token, row):
    doc, e = resolve(token)
    if not doc: abort(404)
    r = next((x for x in e['rows'] if x['id'] == row), None)
    if not r: abort(404)
    if is_credit_row(r):
        return jsonify(error='credit'), 400
    locked = month_locked_response(doc['stmt'])
    if locked: return locked
    flags, sub, _comment, _categories = state(doc['stmt'], e['id'])
    if sub: return jsonify(error='already submitted', flagged=row in flags, count=len(flags)), 409
    on = row not in flags
    rec = {'type': 'flag' if on else 'unflag', 'stmt': doc['stmt'], 'emp_id': e['id'], 'emp': e['name'], 'row': row,
           'date': r['date'], 'category': r['category'], 'company': r['company'], 'amount': r['amount'], **meta()}
    if on: rec['note'] = posted_note()
    append(rec)
    count = len(flags) + (1 if on else -1)
    if 'application/json' in request.headers.get('Accept', ''):
        payload = {'flagged': on, 'count': count, 'row': row}
        if on:
            payload['note'] = rec.get('note') or ''
            payload['line'] = flag_line(r, rec.get('note', ''), rec['ts'])
        return jsonify(payload)
    return redirect(f'/v/{token}', 303)

@app.post('/v/<token>/submit')
def submit(token):
    doc, e = resolve(token)
    if not doc: abort(404)
    with SUBMIT_LOCK:
      flags, sub, stored_comment, categories = state(doc['stmt'], e['id'])
      # Employee Submit only approves rows and emails the verification record.
      # It does not build the monthly summary or send anything for posting.
      if qb_record(doc['stmt']) and not sub:
          return Response('This month was sent for posting', 409)
      if not sub:
          rows = sorted_rows(e['rows'])
          flagged = attach_flag_meta([r for r in rows if r['id'] in flags], flags)
          approved = [dict(r) for r in rows if r['id'] not in flags]
          for r in flagged + approved:
              r['amex_category'] = chosen_category(r, categories)
          incoming = posted_field('comment')
          comment = stored_comment if incoming is None else clean_note(incoming)
          rec = {'type': 'submit', 'stmt': doc['stmt'], 'emp_id': e['id'], 'emp': e['name'], **meta(),
                 'flagged': [r['id'] for r in flagged], 'approved': [r['id'] for r in approved],
                 'flagged_total': round(sum(r['amount'] for r in flagged), 2),
                 'approved_total': round(sum(r['amount'] for r in approved), 2), 'comment': comment}
          append(rec)
          try:
              status = send_email(*record_email(doc, e, flagged, approved, rec['ts'], comment))
          except Exception as ex:
              status = f'error: {type(ex).__name__}: {ex}'
          append({'type': 'email', 'stmt': doc['stmt'], 'emp_id': e['id'], 'emp': e['name'], 'ts': now(),
                  'to': RECORD_TO, 'cc': '', 'status': status})
          maybe_send_all_submitted(doc)
    return redirect(f'/v/{token}', 303)

@app.post('/v/<token>/comment')
def set_comment(token):
    doc, e = resolve(token)
    if not doc: abort(404)
    locked = month_locked_response(doc['stmt'])
    if locked: return locked
    _flags, sub, stored, _categories = state(doc['stmt'], e['id'])
    if sub: return jsonify(error='already submitted'), 409
    incoming = posted_field('comment')
    if incoming is None: return jsonify(error='missing comment'), 400
    text = clean_note(incoming)
    if text != stored:
        append({'type': 'comment', 'stmt': doc['stmt'], 'emp_id': e['id'], 'emp': e['name'], 'comment': text, **meta()})
    if 'application/json' in request.headers.get('Accept', ''):
        return jsonify(ok=True, comment=text)
    return redirect(f'/v/{token}', 303)

@app.post('/v/<token>/category/<int:row>')
def set_category(token, row):
    doc, e = resolve(token)
    if not doc: abort(404)
    r = next((x for x in e['rows'] if x['id'] == row), None)
    if not r: abort(404)
    if is_credit_row(r):
        return jsonify(error='credit'), 400
    locked = month_locked_response(doc['stmt'])
    if locked: return locked
    _flags, sub, _comment, _categories = state(doc['stmt'], e['id'])
    if sub: return jsonify(error='already submitted'), 409
    raw = posted_field('category')
    if raw is None: return jsonify(error='missing category'), 400
    choice = canonical_category(raw)
    if str(raw).strip() and not choice: return jsonify(error='invalid category'), 400
    append({'type': 'category', 'stmt': doc['stmt'], 'emp_id': e['id'], 'emp': e['name'], 'row': row,
            'date': r['date'], 'category': choice, 'company': r['company'], 'amount': r['amount'], **meta()})
    if 'application/json' in request.headers.get('Accept', ''):
        return jsonify(ok=True, row=row, category=choice)
    return redirect(f'/v/{token}', 303)

# ---------- Chris and Meghan admin ----------
def need_admin():
    """Every /admin route. Username must be Chris or Meghan, and the password is ADMIN_PASSWORD.

    An employee token is not a password and cannot open these routes.
    """
    a = request.authorization
    if not ADMIN_PASSWORD or not a or not hmac.compare_digest(a.password or '', ADMIN_PASSWORD):
        abort(Response('Login required', 401, {'WWW-Authenticate': 'Basic realm="Cox Amex admin"'}))
    who = SUMMARY_ACTORS.get((a.username or '').strip().casefold())
    if not who:
        abort(Response('Chris or Meghan only', 403))
    return who

@app.before_request
def guard_admin():
    path = request.path or ''
    if path == '/admin' or path.startswith('/admin/'):
        need_admin()

@app.get('/admin')
def admin():
    need_admin(); esc = html.escape
    rows = []
    for stmt, doc in sorted(load_statements().items(), reverse=True):
        for e in doc['employees']:
            flags, sub, _comment, _categories = state(stmt, e['id'])
            link = f"{PUBLIC_BASE}/v/{make_token(stmt, e['id'])}"
            st = f"<b style='color:#2e7d32'>Submitted {esc(nice(sub['ts']))}</b>" if sub else "<b style='color:#c62828'>Not submitted</b>"
            rows.append(f"<tr><td>{stmt}</td><td>{esc(e['name'])}</td><td>{st}</td><td>{len(sub['flagged']) if sub else len(flags)}</td>"
                        f"<td>{len(e['rows'])}</td><td>{money(sum(r['amount'] for r in e['rows']))}</td><td style='font-size:12px'>{esc(link)}</td></tr>")
    return (f"<!doctype html><meta charset='utf-8'><title>Amex admin</title><body style='font-family:Arial;padding:16px'>"
            f"<h2>Amex verification - status</h2><p>Record email: {esc(EMAIL_MODE)} to {esc(RECORD_TO)}. "
            f"<a href='/admin/log.csv'>Download full log (CSV)</a> · <a href='/admin/summary'>Monthly summary</a></p>"
            f"<table border='1' cellpadding='6' cellspacing='0'><tr><th>Statement</th><th>Employee</th><th>Status</th><th>Flagged</th><th>Charges</th><th>Total</th><th>Link</th></tr>{''.join(rows)}</table>")

@app.get('/admin/log.csv')
def admin_csv():
    need_admin()
    cols = ['ts', 'type', 'stmt', 'emp_id', 'emp', 'row', 'date', 'category', 'company', 'amount',
            'flagged', 'approved', 'flagged_total', 'approved_total', 'to', 'cc', 'status', 'ip', 'ua', 'note', 'comment', 'by']
    buf = io.StringIO(); w = csv.DictWriter(buf, cols, extrasaction='ignore'); w.writeheader()
    for r in read_log():
        row = {}
        for k, v in r.items():
            if isinstance(v, list): v = ';'.join(map(str, v))
            elif k in ('note', 'comment') and isinstance(v, str) and v[:1] in '=+-@\t\r': v = "'" + v
            row[k] = v
        w.writerow(row)
    return Response(buf.getvalue(), mimetype='text/csv', headers={'Content-Disposition': 'attachment; filename=amex-verification-log.csv'})

@app.get('/admin/links.json')
def admin_links():
    need_admin()
    return jsonify({stmt: {e['id']: {'name': e['name'], 'email': e.get('email', ''), 'url': f"{PUBLIC_BASE}/v/{make_token(stmt, e['id'])}"}
                           for e in doc['employees']} for stmt, doc in load_statements().items()})

@app.post('/admin/upload')
def admin_upload():
    """Add/replace a statement without a redeploy: POST the statements/<stmt>.json body."""
    need_admin(); doc = request.get_json(force=True)
    assert doc.get('stmt') and isinstance(doc.get('employees'), list)
    bundled_path = os.path.join(APP_DIR, 'statements', 'bundled', doc['stmt'] + '.json')
    bundled = {}
    if os.path.isfile(bundled_path):
        with open(bundled_path) as fh:
            bundled = json.load(fh)
    disk_path = os.path.join(DATA_DIR, 'statements', doc['stmt'] + '.json')
    current = {}
    if os.path.isfile(disk_path):
        with open(disk_path) as fh:
            current = json.load(fh)
    # An upload always outranks the copy shipped in the image.
    doc['bundle_rev'] = max(bundle_rev(doc), bundle_rev(bundled), bundle_rev(current)) + 1
    lock_routing(doc)
    d = os.path.join(DATA_DIR, 'statements'); os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, doc['stmt'] + '.json'), 'w') as fh:
        json.dump(doc, fh, indent=1)
        fh.write('\n')
    return jsonify(ok=True, stmt=doc['stmt'], employees=len(doc['employees']))

@app.post('/admin/reset/<stmt>/<emp_id>')
def admin_reset(stmt, emp_id):
    """Reopen one employee's page (clears flags + submission; history stays in the log)."""
    need_admin()
    if qb_record(stmt):
        return jsonify(error='month locked'), 409
    append({'type': 'reset', 'stmt': stmt, 'emp_id': emp_id, 'ts': now()})
    return jsonify(ok=True)

# ---------- monthly summary (Chris and Meghan only) ----------
SUMMARY_ACTORS = {'chris': 'Chris', 'meghan': 'Meghan'}

def summary_actor():
    """Same Chris/Meghan password as /admin. The username is what a QB press records."""
    return need_admin()

def summary_folder(label):
    """One path segment under /var/data/summaries. Slashes cannot escape that directory."""
    segment = str(label or 'unknown').replace('/', '-').replace('\\', '-').strip() or 'unknown'
    return os.path.join(DATA_DIR, 'summaries', segment)

def render_summary_page(built, actor, qb):
    esc = html.escape
    period = built['period']
    stmt = built['stmt']
    parts = ["<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>",
             f"<title>{esc(period['month_label'])} American Express summary</title>",
             "<style>body{font-family:Arial,Helvetica,sans-serif;margin:0;color:#222}h1{font-size:28px;margin:16px 18px 8px}",
             ".sheet{margin:0 18px 40px}table{border-collapse:collapse;width:100%;max-width:760px;margin:12px 0}",
             "td,th{border:1px solid #999;padding:8px 10px;text-align:left}th{background:#e8e8e8}.n{text-align:right}",
             ".warn{color:#c62828;font-weight:bold}.sent{color:#2e7d32;font-weight:bold;font-size:20px}",
             ".qb{margin-top:16px;font-size:18px;padding:12px 18px;background:#0a5;color:#fff;border:0;border-radius:6px;cursor:pointer}",
             ".qb:disabled{background:#9aa;cursor:default}a.dl{display:inline-block;margin:12px 16px 0 0}</style></head><body>",
             f"<h1>{esc(period['month_label'])} American Express</h1><div class='sheet'>",
             f"<p>Signed in as {esc(actor)}. Statement close date: {esc(period['close_date'])}.</p>",
             f"<p>Month folder: {esc(period['folder_label'])}.</p>",
             "<h2>Cardholders</h2><table><tr><th>Employee</th><th>Status</th><th>Total</th></tr>"]
    for person in built['people']:
        if person['submitted'] and person.get('record_type') == 'admin_approve':
            status = 'Approved by Chris ' + esc(nice(person['ts'])) if person['ts'] else 'Approved by Chris'
        elif person['submitted']:
            status = 'Submitted ' + esc(nice(person['ts'])) if person['ts'] else 'Submitted'
        else:
            status = "<b style='color:#c62828'>Not submitted</b>"
        parts.append(f"<tr><td>{esc(person['name'])}</td><td>{status}</td><td class='n'>{summary.format_money(person['total_cents'])}</td></tr>")
    parts.append("</table><h2>Categories</h2><table><tr><th>Category</th><th>Amount</th></tr>")
    for name, cents in built['categories']:
        parts.append(f"<tr><td>{esc(name)}</td><td class='n'>{summary.format_money(cents)}</td></tr>")
    parts.append(f"<tr><td><b>Grand total</b></td><td class='n'><b>{summary.format_money(built['summary_cents'])}</b></td></tr></table>")
    parts.append(f"<p>Statement total: {summary.format_money(built['statement_cents'])}.</p>")
    parts.append(f"<p><a class='dl' href='/admin/summary/{esc(stmt)}/summary.pdf'>Download PDF</a>"
                 f"<a class='dl' href='/admin/summary/{esc(stmt)}/summary.csv'>Download CSV</a>"
                 f"<a class='dl' href='/admin/summary/{esc(stmt)}/summary.xlsx'>Download XLSX</a></p>")
    if qb:
        when = esc(nice(qb['ts'])) if qb.get('ts') else ''
        parts.append(f"<p class='sent' id='sent'>Sent for posting</p><p id='sent-when'>{when} by {esc(qb.get('by') or '')}.</p>")
    else:
        if not built['ready']:
            parts.append("<p class='warn' id='summary-warning'>Summary total "
                         f"{summary.format_money(built['summary_cents'])} does not equal the statement total "
                         f"{summary.format_money(built['statement_cents'])}. "
                         f"Difference: {summary.format_money(built['difference_cents'])}."
                         + ("" if built['everyone_submitted'] else " Not everyone has submitted.")
                         + "</p>")
            disabled = ' disabled'
        else:
            disabled = ''
        parts.append(f"<form method='post' action='/admin/summary/{esc(stmt)}/qb-submit'>"
                     f"<button class='qb' type='submit' id='qb-submit'{disabled}>Submit to QuickBooks for Pay</button></form>")
    parts.append("<p style='color:#555;font-size:14px'>Littledude creates the bill and files the attachments. This server does not.</p></div></body></html>")
    return ''.join(parts)

def write_summary_files(built):
    folder = summary_folder(built['period']['folder_label'])
    sub_dir = os.path.join(folder, 'submissions')
    os.makedirs(sub_dir, exist_ok=True)
    files = {
        summary.summary_filename(built, 'pdf'): summary.build_pdf(built),
        summary.summary_filename(built, 'xlsx'): summary.build_xlsx(built),
        summary.summary_filename(built, 'csv'): summary.build_csv(built),
    }
    for name, data in files.items():
        with open(os.path.join(folder, name), 'wb') as fh:
            fh.write(data)
    records = []
    for person in built['people']:
        if not person['submitted']:
            continue
        raw = json.dumps(summary.submission_record(built, person), indent=2).encode()
        fn = person['id'] + '-submission.json'
        with open(os.path.join(sub_dir, fn), 'wb') as fh:
            fh.write(raw)
        records.append((fn, raw))
    return folder, records

def qb_email(built, actor):
    text, html_body = summary.qb_bodies(built, actor)
    msg = EmailMessage()
    msg['Subject'] = summary.qb_subject(built)
    msg['From'] = MAIL_FROM
    msg['To'] = RECORD_ADDRESS
    msg.set_content(text)
    msg.add_alternative(html_body, subtype='html')
    msg.add_attachment(summary.build_pdf(built), maintype='application', subtype='pdf',
                       filename=summary.summary_filename(built, 'pdf'))
    msg.add_attachment(summary.build_xlsx(built), maintype='application',
                       subtype='vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                       filename=summary.summary_filename(built, 'xlsx'))
    for person in built['people']:
        if not person['submitted']:
            continue
        raw = json.dumps(summary.submission_record(built, person), indent=2).encode()
        msg.add_attachment(raw, maintype='application', subtype='json', filename=person['id'] + '-submission.json')
    return msg

@app.get('/admin/summary')
def summary_index():
    actor = summary_actor()
    esc = html.escape
    rows = []
    for stmt, doc in sorted(load_statements().items(), reverse=True):
        built = summary.build_summary(doc, state)
        qb = qb_record(stmt)
        status = 'Sent for posting' if qb else ('Ready' if built['ready'] else 'Waiting')
        rows.append(f"<tr><td><a href='/admin/summary/{esc(stmt)}'>{esc(stmt)}</a></td>"
                    f"<td>{esc(summary.statement_period(doc)['month_label'])}</td><td>{esc(status)}</td></tr>")
    return (f"<!doctype html><meta charset='utf-8'><title>Monthly summary</title><body style='font-family:Arial;padding:16px'>"
            f"<h1>Monthly summary</h1><p>Signed in as {esc(actor)}.</p>"
            f"<table border='1' cellpadding='6' cellspacing='0'><tr><th>Statement</th><th>Month</th><th>Status</th></tr>{''.join(rows)}</table>")

@app.get('/admin/summary/<stmt>')
def summary_page(stmt):
    actor = summary_actor()
    doc = load_statements().get(stmt)
    if not doc:
        abort(404)
    built = summary.build_summary(doc, state)
    return Response(render_summary_page(built, actor, qb_record(stmt)), headers={'Cache-Control': 'no-store'})

def _summary_download(stmt, kind):
    summary_actor()
    doc = load_statements().get(stmt)
    if not doc:
        abort(404)
    built = summary.build_summary(doc, state)
    if kind == 'pdf':
        data, mime, ext = summary.build_pdf(built), 'application/pdf', 'pdf'
    elif kind == 'csv':
        data, mime, ext = summary.build_csv(built), 'text/csv', 'csv'
    else:
        data, mime, ext = summary.build_xlsx(built), 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', 'xlsx'
    name = summary.summary_filename(built, ext)
    return Response(data, mimetype=mime, headers={
        'Content-Disposition': f'attachment; filename="{name}"', 'Cache-Control': 'no-store'})

@app.get('/admin/summary/<stmt>/summary.pdf')
def summary_pdf(stmt):
    return _summary_download(stmt, 'pdf')

@app.get('/admin/summary/<stmt>/summary.csv')
def summary_csv(stmt):
    return _summary_download(stmt, 'csv')

@app.get('/admin/summary/<stmt>/summary.xlsx')
def summary_xlsx(stmt):
    return _summary_download(stmt, 'xlsx')

@app.post('/admin/summary/<stmt>/qb-submit')
def summary_qb_submit(stmt):
    actor = summary_actor()
    doc = load_statements().get(stmt)
    if not doc:
        abort(404)
    with SUBMIT_LOCK:
        if qb_record(stmt):
            return redirect(f'/admin/summary/{stmt}', 303)
        built = summary.build_summary(doc, state)
        if not built['ready']:
            return jsonify(error='not ready', difference=round(built['difference_cents'] / 100, 2)), 409
        folder, _records = write_summary_files(built)
        try:
            status = send_prepared(qb_email(built, actor))
        except Exception as ex:
            return Response(f'Summary files were saved but the email failed: {type(ex).__name__}: {ex}', 502)
        payload = summary.category_payload(built)
        append({'type': 'qb_submit', 'stmt': stmt, 'by': actor, **meta(),
                'close_date': built['period']['close_date'], 'month': built['period']['month_label'],
                'folder': built['period']['folder_label'],
                'summary_total': payload['grand_total'], 'statement_total': payload['statement_total'],
                'categories': payload['categories'], 'to': RECORD_ADDRESS, 'email_status': status,
                'files': folder})
    return redirect(f'/admin/summary/{stmt}', 303)

def require_chris():
    who = need_admin()
    if who != 'Chris':
        abort(Response('Chris only', 403))
    return who

def _employee(doc, emp_id):
    return next((emp for emp in (doc or {}).get('employees') or [] if emp.get('id') == emp_id), None)

@app.get('/admin/pending.json')
def admin_pending():
    """Who has not submitted. Names and ids only. Newest statement unless ?stmt= is set."""
    need_admin()
    statements = load_statements()
    stmt = (request.args.get('stmt') or '').strip()
    if not stmt:
        if not statements:
            abort(404)
        stmt = max(statements)
    doc = statements.get(stmt)
    if not doc:
        abort(404)
    missing, submitted = pending_people(doc)
    return jsonify(statement=stmt, month=summary.statement_period(doc)['month_label'],
                   not_submitted=[{'name': emp.get('name') or emp['id'], 'id': emp['id']} for emp in missing],
                   submitted=submitted, total=len(doc.get('employees') or []))

@app.get('/admin/summary/<stmt>/approve/<emp_id>')
def approve_confirm(stmt, emp_id):
    require_chris()
    doc = load_statements().get(stmt)
    emp = _employee(doc, emp_id)
    if not emp:
        abort(404)
    if qb_record(stmt):
        return Response('This month was sent for posting', 409)
    _flags, sub, _comment, _categories = state(stmt, emp_id)
    if sub:
        return Response('Already submitted', 409)
    esc = html.escape
    name = emp.get('name') or emp_id
    return (f"<!doctype html><meta charset='utf-8'><title>Approve {esc(name)}</title>"
            f"<body style='font-family:Arial;padding:16px'><h1>Approve {esc(name)}'s charges</h1>"
            f"<p>This approves every charge as submitted. No email goes to {esc(name)}.</p>"
            f"<form method='post' action='/admin/summary/{esc(stmt)}/approve/{esc(emp_id)}'>"
            f"<button type='submit' id='approve'>Approve {esc(name)}'s charges</button></form>")

@app.post('/admin/summary/<stmt>/approve/<emp_id>')
def admin_approve(stmt, emp_id):
    """Chris approves one cardholder as-is. No email to that employee."""
    require_chris()
    doc = load_statements().get(stmt)
    emp = _employee(doc, emp_id)
    if not emp:
        abort(404)
    with SUBMIT_LOCK:
        if qb_record(stmt):
            return jsonify(error='month locked'), 409
        _flags, sub, _comment, _categories = state(stmt, emp_id)
        if sub:
            return jsonify(error='already submitted'), 409
        rows = sorted_rows(emp.get('rows') or [])
        rec = {'type': 'admin_approve', 'stmt': stmt, 'emp_id': emp_id,
               'emp': emp.get('name') or emp_id, 'by': 'Chris', 'note': APPROVAL_NOTE,
               'flagged': [], 'approved': [r['id'] for r in rows], 'flagged_total': 0,
               'approved_total': round(sum(float(r['amount']) for r in rows), 2), 'comment': '', **meta()}
        append(rec)
        maybe_send_all_submitted(doc)
    return redirect(f'/admin/summary/{stmt}', 303)

@app.post('/admin/summary/<stmt>/pending-email')
def admin_pending_email(stmt):
    """Ask Chris who is still out. Littledude reads any APPROVE reply and calls the approve endpoint."""
    need_admin()
    doc = load_statements().get(stmt)
    if not doc:
        abort(404)
    missing, _submitted = pending_people(doc)
    try:
        status = send_prepared(pending_reminder_email(doc, missing))
    except Exception as ex:
        return Response(f'The reminder email failed: {type(ex).__name__}: {ex}', 502)
    append({'type': 'pending_email', 'stmt': stmt, 'ts': now(), 'to': CHRIS_EMAIL,
            'pending': [emp['id'] for emp in missing], 'email_status': status})
    return jsonify(ok=True, to=CHRIS_EMAIL, pending=[emp['id'] for emp in missing])

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', '10000')))
