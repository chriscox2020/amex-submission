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
# Employee dropdown groups. Headings are optgroup labels and are not categories.
AMEX_CATEGORY_GROUPS = (
    ('Office', ('Meals and Entertainment', 'Gift', 'Utilities', 'Freight & Delivery', 'Office Expenses')),
    ('Shop Supplies', ('Job Materials', 'Supplies', 'Repair & Maintenance')),
    ('Automotive', ('Travel Fuel', 'Auto Maintenance')),
    ('Travel (hotels, other expenses)', ('Travel',)),
)
# After the groups, not inside one, and not a default for blank or off-list charges.
AMEX_UNGROUPED = ('Other Expense',)
AMEX_CATEGORIES = tuple(name for _label, names in AMEX_CATEGORY_GROUPS for name in names) + AMEX_UNGROUPED

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

def flag_line(row, note, flagged_ts, category=None):
    """date | store | amount | current category | flagged date | reference | Note: text

    The note is omitted when blank. Callers HTML-escape the whole line.
    A saved note is shown as stored, including an older 'Not my charge' reason.
    """
    if category is None:
        category = row.get('amex_category') or row.get('category') or ''
    parts = [str(row.get('date') or ''), str(row.get('company') or ''), money(row.get('amount') or 0),
             str(category), flag_day(flagged_ts), charge_ref(row)]
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

def flag_items_html(rows, flags, categories=None):
    items = []
    for r in rows:
        if r['id'] not in flags: continue
        info = flag_meta(flags, r['id'])
        line = flag_line(r, info.get('note', ''), info.get('ts', ''), display_category(r, categories or {}))
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

FLAG_REASONS = ('Wrong category', 'Wrong price', 'Duplicate charge', 'No receipt', 'Other')

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

def display_category(row, categories):
    """Category shown for this charge. An edit wins. Otherwise the statement text, unchanged when it is not on the list."""
    if isinstance(categories, dict) and row.get('id') in categories:
        return str(categories.get(row['id']) or '')
    raw = str(row.get('category') or '').strip()
    return canonical_category(raw) or raw

def accept_category(raw, row, categories):
    """A list category, or this charge's current off-list value kept exactly. None when rejected.
    Empty stays empty. Nothing here substitutes a default category."""
    choice = canonical_category(raw)
    if choice:
        return choice
    kept = str(raw or '').strip()
    if not kept:
        return ''
    current = display_category(row, categories)
    if kept == current:
        return current
    return None

def chosen_category(row, categories):
    """Dropdown choice when the employee set one, otherwise the statement category."""
    if isinstance(categories, dict) and row.get('id') in categories:
        return categories[row['id']] or ''
    return display_category(row, categories)

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
    fn = os.path.join(OUTBOX, datetime.now(TZ).strftime('%Y%m%d-%H%M%S-') + hashlib.sha1(str(msg['Subject'] or '').encode()).hexdigest()[:6] + '.eml')
    with open(fn, 'wb') as fh:
        fh.write(bytes(msg))
    return 'dryrun:' + fn

def send_email(subj, body, text):
    to = [x.strip() for x in RECORD_TO.split(',') if x.strip()]
    cc = [x.strip() for x in RECORD_CC.split(',') if x.strip()]
    msg = EmailMessage(); msg['Subject'] = subj; msg['From'] = MAIL_FROM; msg['To'] = ', '.join(to)
    if cc: msg['Cc'] = ', '.join(cc)
    msg.set_content(text); msg.add_alternative(body, subtype='html')
    return send_prepared(msg)

FLAG_REVIEW_SUBJECT = 'Review your flagged charges'

def employee_address(emp):
    """Address on the employee record. send_to wins; Alice's record is already locked to Chris."""
    if not isinstance(emp, dict):
        return ''
    return str(emp.get('send_to') or emp.get('email') or '').strip()

def flag_review_bodies(doc, flagged):
    """One message listing every flagged charge. Not the filing record."""
    lines = []
    for r in flagged:
        lines.append(' | '.join([
            str(r.get('date') or ''),
            str(r.get('company') or ''),
            money(r.get('amount') or 0),
            str(r.get('amex_category') or r.get('category') or ''),
            str(r.get('note') or ''),
        ]))
    text = ("Review your flagged charges.\n" + ''.join(f"  {line}\n" for line in lines))
    items = ''.join(f"<li>{html.escape(line)}</li>" for line in lines)
    body = (f"<div style='font-family:Arial,Helvetica,sans-serif'><p><b>Review your flagged charges</b> "
            f"({html.escape(str((doc or {}).get('label') or ''))}).</p><ul>{items}</ul></div>")
    return text, body

def send_flag_review(doc, emp, flagged):
    """One email to the employee. Raises only after the caller has already sent the filing record."""
    addr = employee_address(emp)
    if not addr:
        raise ValueError('no employee email on record')
    text, body = flag_review_bodies(doc, flagged)
    msg = EmailMessage()
    msg['Subject'] = FLAG_REVIEW_SUBJECT
    msg['From'] = MAIL_FROM
    msg['To'] = addr
    msg.set_content(text)
    msg.add_alternative(body, subtype='html')
    return send_prepared(msg)

# ---------- page ----------
CSS = """body{margin:0;background:#fff;color:#222;font-family:Arial,Helvetica,sans-serif;font-size:20px}
.banner{background:#006fcf;color:#fff;font-size:44px;font-weight:bold;text-align:center;padding:22px 12px;margin:0 0 18px;width:100%;box-sizing:border-box;-webkit-print-color-adjust:exact;print-color-adjust:exact}
h1{font-size:36px;margin:0 18px 16px;text-align:left;font-weight:bold}
.sheet{margin:0 18px 40px}
table{border-collapse:collapse;width:100%}td,th{border:1px solid #999;padding:8px 10px;vertical-align:top}th{background:#e8e8e8;text-align:left}
.n{text-align:right;white-space:nowrap}.c{text-align:center}.fc{position:relative;width:70px}
tr.f td{background:#ffecec}tr.ed td{background:#e3f0ff}tr.cr td{background:#f3fbf3}tr.nx td{background:#fff7cc}
.lbl{color:#c00;font-weight:bold;font-size:15px}
.fb,.sub,.prt,.sv{box-sizing:border-box;font-weight:700;line-height:1.2;cursor:pointer;border-radius:12px;transition:background .15s ease,box-shadow .15s ease,border-color .15s ease}
.fb{min-width:44px;min-height:44px;padding:8px 12px;font-size:18px;background:#fff;color:#c44747;border:1.5px solid #f0b4b4;box-shadow:0 1px 2px rgba(160,40,40,.12)}
.fb:hover{background:#fff6f6;border-color:#e08a8a}
.fb.on{background:#e25b5b;color:#fff;border-color:#e25b5b;box-shadow:0 2px 6px rgba(180,50,50,.22)}
.fb.on:hover{background:#d24e4e}
.fb:focus-visible,.sub:focus-visible,.prt:focus-visible,.sv:focus-visible{outline:3px solid #9ec5ff;outline-offset:2px}
button:disabled{opacity:.55;cursor:default;box-shadow:none}
.pop{position:fixed;left:50%;top:12%;transform:translateX(-50%);width:min(380px,calc(100vw - 24px));z-index:20;text-align:left;border:1px solid #d3e4f4;background:#fff;padding:12px;border-radius:14px;box-shadow:0 10px 28px rgba(20,60,110,.16)}
.pop[hidden]{display:none!important}
.chips{margin:8px 0;display:flex;flex-wrap:wrap;gap:8px}.pop .chip{display:inline-block;margin:0;font-weight:600;font-size:16px;color:#1a2332;border:1px solid #c5d7ea;background:#f7fbff;border-radius:999px;padding:8px 12px;cursor:pointer;min-height:44px;box-sizing:border-box}
.pop .chip:hover{background:#e3f2fc}
.sv{min-height:44px;padding:10px 22px;font-size:17px;background:#006fcf;color:#fff;border:0;box-shadow:0 2px 6px rgba(0,80,160,.2)}
.sv:hover{background:#005bb5}
.pop textarea{width:95%;height:50px;font-size:18px;display:block;margin:6px 0;border:1px solid #c5d7ea;border-radius:10px;padding:8px}
label.boxlabel{display:block;font-weight:bold;margin:24px 0 6px}
#fcb{width:100%;box-sizing:border-box;height:110px;font-size:20px;border:2px solid #333;padding:8px}
#flaglist{margin:16px 0 0;padding-left:18px}#flaglist li,.done li{margin:6px 0;font-size:15px;line-height:1.4}
td.cat{position:relative}
.catpick{position:relative}
.catbtn{display:block;width:100%;box-sizing:border-box;text-align:left;min-height:48px;padding:12px 14px;
font:inherit;font-size:18px;font-weight:700;line-height:1.3;color:#1a2332;background:#fff;border:1.5px solid #b7d0ea;border-radius:10px;box-shadow:0 1px 2px rgba(20,70,130,.08);cursor:pointer}
.catbtn:hover{background:#f4f8fd;border-color:#8eb7e0}
.catbtn.ed-cat{color:#006fcf}
.catbtn:focus-visible,.catopt:focus-visible{outline:3px solid #7eb6ff;outline-offset:2px}
.catmenu{position:absolute;z-index:30;right:0;top:calc(100% + 4px);width:min(340px,calc(100vw - 24px));max-height:70vh;overflow:auto;
background:#f7fbff;color:#1a2332;border:1px solid #d3e4f4;border-radius:12px;box-shadow:0 8px 24px rgba(20,60,110,.12);padding:6px;text-align:left}
.catmenu[hidden]{display:none!important}
.cathead{margin:6px 4px 2px;padding:12px 14px;font-size:16px;font-weight:800;line-height:1.2;color:#3d5270;background:#e7eef6;border-radius:8px}
.catopt{display:block;width:100%;box-sizing:border-box;text-align:left;min-height:48px;padding:14px 16px;
font:inherit;font-size:18px;font-weight:600;line-height:1.3;color:#1a2332;background:transparent;border:0;border-radius:8px;cursor:pointer}
.catopt:hover{background:#e3f2fc}
.catopt.cur{background:#b9d7f5;color:#0b3d73}
.catopt.ungrouped{margin-top:8px;border-top:1px solid #d3e4f4;border-radius:0 0 8px 8px}
.edited{font-weight:bold}.ed-cat{color:#06c;font-weight:bold}.ed-note{color:#d00}.rsn{font-size:16px;margin-top:2px}
s.orig{display:block;font-size:14px;color:#888;font-weight:normal}
.crn{color:#080;font-weight:bold}.crl{font-size:14px;color:#080;font-weight:bold;border:1px solid #080;border-radius:4px;padding:1px 6px;margin-left:6px}
tr.ft td{background:#f6f6f6;font-size:22px}tr.net td{font-weight:bold;font-size:24px;background:#eaeaea}
.done{background:#e8f5e9;border:1px solid #2e7d32;border-radius:8px;padding:14px 16px;font-size:16px;margin:0 0 16px}
.sub{margin-top:24px;min-height:48px;padding:12px 28px;font-size:20px;background:#006fcf;color:#fff;border:0;box-shadow:0 2px 6px rgba(0,80,160,.22)}
.sub:hover{background:#005bb5}
.prt{margin-top:24px;margin-left:12px;min-height:48px;padding:12px 24px;font-size:20px;background:#fff;color:#1a4f86;border:1.5px solid #8eb7e0;box-shadow:0 1px 2px rgba(0,60,120,.08)}
.prt:hover{background:#f4f8fd;border-color:#6aa0d4}
@media(max-width:700px){body{font-size:15px}.banner{font-size:28px;padding:16px 8px}h1{font-size:26px;margin:0 8px 12px}.sheet{margin:0 8px 24px}
td,th{padding:6px}.sub,.prt,.sv,.fb{min-height:44px;font-size:18px}#fcb{height:80px;font-size:16px}tr.ft td,tr.net td{font-size:16px}}
@media print{button.fb,button.sub,button.prt,button.sv,.pop,.catmenu{display:none!important}.catbtn{background:transparent;color:#000;border:0;padding:0;min-height:0;font-size:12pt}body{margin:10mm;font-size:12pt}.banner{font-size:22pt}
tr{page-break-inside:avoid}td,th{padding:4px 6px}#fcb{height:60px}tr.ft td{font-size:12pt}tr.net td{font-size:13pt}
*{-webkit-print-color-adjust:exact;print-color-adjust:exact}}
"""

JS = r"""function closeCats(){
  document.querySelectorAll('.catmenu').forEach(function(m){m.hidden=true});
  document.querySelectorAll('.catbtn').forEach(function(b){b.setAttribute('aria-expanded','false')});
}
function paintCat(btn,v){
  var td=btn.closest('td');if(!td)return;
  var orig=td.getAttribute('data-orig')||'';
  td.setAttribute('data-cur',v);
  btn.setAttribute('data-cur',v);
  var old=td.querySelector('s.orig');
  var changed=v!==orig;
  btn.classList.toggle('ed-cat',changed);
  if(changed){
    if(!old){old=document.createElement('s');old.className='orig';td.appendChild(old)}
    old.textContent=orig;
  }else if(old)old.remove();
  if(td.parentNode)td.parentNode.classList.toggle('ed',changed);
}
function chooseCat(btn,opt){
  var v=opt.getAttribute('data-value');
  var prev=btn.getAttribute('data-cur')||'';
  var prevLabel=btn.textContent;
  fetch(btn.getAttribute('data-action'),{method:'POST',headers:{'Accept':'application/json','Content-Type':'application/x-www-form-urlencoded'},body:'category='+encodeURIComponent(v)}).then(function(r){
    if(!r.ok){btn.textContent=prevLabel;btn.setAttribute('data-cur',prev);return}
    btn.textContent=opt.textContent;
    var menu=btn.parentNode.querySelector('.catmenu');
    menu.querySelectorAll('.catopt').forEach(function(o){
      var on=o.getAttribute('data-value')===v;
      o.classList.toggle('cur',on);
      if(on)o.setAttribute('aria-selected','true');else o.setAttribute('aria-selected','false');
    });
    paintCat(btn,v);
    closeCats();
    btn.focus();
  }).catch(function(){btn.textContent=prevLabel;btn.setAttribute('data-cur',prev)});
}
document.querySelectorAll('.catpick').forEach(function(box){
  var btn=box.querySelector('.catbtn');
  var menu=box.querySelector('.catmenu');
  function openMenu(){
    var was=menu.hidden;
    closeCats();
    if(!was)return;
    menu.hidden=false;
    btn.setAttribute('aria-expanded','true');
    var cur=menu.querySelector('.catopt.cur')||menu.querySelector('.catopt');
    if(cur)cur.focus();
  }
  btn.addEventListener('click',function(ev){ev.preventDefault();ev.stopPropagation();openMenu()});
  btn.addEventListener('keydown',function(ev){
    if(ev.key==='ArrowDown'||ev.key==='Enter'||ev.key===' '){ev.preventDefault();if(menu.hidden)openMenu()}
  });
  menu.addEventListener('keydown',function(ev){
    var list=Array.prototype.slice.call(menu.querySelectorAll('.catopt'));
    var i=list.indexOf(document.activeElement);
    if(ev.key==='ArrowDown'){ev.preventDefault();if(list.length)list[Math.min(list.length-1,Math.max(0,i)+1)].focus()}
    else if(ev.key==='ArrowUp'){ev.preventDefault();if(list.length)list[Math.max(0,(i<0?1:i)-1)].focus()}
    else if(ev.key==='Home'){ev.preventDefault();if(list.length)list[0].focus()}
    else if(ev.key==='End'){ev.preventDefault();if(list.length)list[list.length-1].focus()}
    else if((ev.key==='Enter'||ev.key===' ')&&document.activeElement&&document.activeElement.classList.contains('catopt')){ev.preventDefault();chooseCat(btn,document.activeElement)}
    else if(ev.key==='Escape'){ev.preventDefault();closeCats();btn.focus()}
  });
  menu.querySelectorAll('.catopt').forEach(function(opt){
    opt.addEventListener('click',function(ev){ev.preventDefault();ev.stopPropagation();chooseCat(btn,opt)});
  });
});
document.addEventListener('click',function(ev){if(!ev.target.closest('.catpick'))closeCats()});
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
document.addEventListener('keydown',function(ev){if(ev.key==='Escape'){closeCats();if(!pop.hidden){ev.preventDefault();closePop()}}});
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
    """Current category, the statement original, and whether the employee changed it."""
    original = str(row.get('category') or '')
    explicit = isinstance(categories, dict) and row.get('id') in categories
    current = display_category(row, categories)
    baseline = canonical_category(original) or original.strip()
    edited = bool(explicit and (current or '') != (baseline or ''))
    return current, original, edited

def _cat_option(value, label, selected, extra=''):
    esc = html.escape
    cls = 'catopt' + (' cur' if selected else '') + ((' ' + extra) if extra else '')
    aria = 'true' if selected else 'false'
    return (f"<button type='button' class='{cls}' role='option' data-value='{esc(value)}' aria-selected='{aria}'>"
            f"{esc(label)}</button>")

def category_picker(current, token, row_id, edited):
    """Grouped list any charge can use. Headings are not options. Off-list values stay as '(current)'."""
    esc = html.escape
    cur = str(current or '')
    known = False
    blocks = []
    for label, names in AMEX_CATEGORY_GROUPS:
        opts = []
        for name in names:
            selected = cur.casefold() == name.casefold()
            if selected:
                known = True
            opts.append(_cat_option(name, name, selected))
        blocks.append(f"<div class='catgrp' role='group' aria-label='{esc(label)}'>"
                      f"<div class='cathead'>{esc(label)}</div>{''.join(opts)}</div>")
    ungrouped = []
    for name in AMEX_UNGROUPED:
        selected = cur.casefold() == name.casefold()
        if selected:
            known = True
        ungrouped.append(_cat_option(name, name, selected, 'ungrouped'))
    face = cur
    prefix = ''
    if not cur:
        face = 'Choose category'
    elif not known:
        face = cur + ' (current)'
        prefix = _cat_option(cur, face, True)
    cls = 'catbtn ed-cat' if edited else 'catbtn'
    return (f"<div class='catpick'>"
            f"<button type='button' class='{cls}' aria-haspopup='listbox' aria-expanded='false' "
            f"data-cur='{esc(cur)}' data-action='/v/{token}/category/{int(row_id)}'>{esc(face)}</button>"
            f"<div class='catmenu' hidden role='listbox' aria-label='Pick category'>{prefix}{''.join(blocks)}{''.join(ungrouped)}</div>"
            f"</div>")

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
    lis = flag_items_html(rows, flags, categories)
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
        if credit or locked:
            inner = esc(shown)
        else:
            strike = f"<s class='orig'>{esc(original)}</s>" if edited and original and original != current else ''
            inner = category_picker(current, token, rid, edited) + strike
        cat_cls = '' if credit or locked else " class='cat'"
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
                     f"<td{cat_cls} data-orig='{esc(original)}' data-cur='{esc(shown)}'>{inner}</td>"
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
    parts.append(popup_html() + f"<script>{JS}</script></div></body></html>")
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
            payload['line'] = flag_line(r, rec.get('note', ''), rec['ts'], display_category(r, _categories))
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
                  'to': RECORD_TO, 'cc': RECORD_CC, 'status': status})
          # After the filing record. A failure here does not change the submit.
          if flagged:
              addr = employee_address(e)
              try:
                  freview = send_flag_review(doc, e, flagged)
              except Exception as ex:
                  freview = f'error: {type(ex).__name__}: {ex}'
              append({'type': 'flag_review_email', 'stmt': doc['stmt'], 'emp_id': e['id'], 'emp': e['name'],
                      'ts': now(), 'to': addr, 'count': len(flagged), 'status': freview})
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
    choice = accept_category(raw, r, _categories)
    if choice is None: return jsonify(error='invalid category'), 400
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
            f"<h2>Amex verification - status</h2><p>Record email: {esc(EMAIL_MODE)} to {esc(RECORD_TO)}{' cc ' + esc(RECORD_CC) if RECORD_CC else ''}. "
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
             ".qb{margin-top:16px;min-height:48px;font-size:18px;font-weight:700;padding:12px 22px;background:#006fcf;color:#fff;border:0;border-radius:12px;cursor:pointer;box-shadow:0 2px 6px rgba(0,80,160,.22)}",
             ".qb:hover{background:#005bb5}.qb:focus-visible{outline:3px solid #9ec5ff;outline-offset:2px}",
             ".qb:disabled{background:#9aa;cursor:default;box-shadow:none}a.dl{display:inline-block;margin:12px 16px 0 0}</style></head><body>",
             f"<h1>{esc(period['month_label'])} American Express</h1><div class='sheet'>",
             f"<p>Signed in as {esc(actor)}. Statement close date: {esc(period['close_date'])}.</p>",
             f"<p>Month folder: {esc(period['folder_label'])}.</p>",
             "<h2>Cardholders</h2><table><tr><th>Employee</th><th>Status</th><th>Total</th></tr>"]
    for person in built['people']:
        if person['submitted']:
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

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', '10000')))
