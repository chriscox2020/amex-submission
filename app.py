"""Cox Research - American Express employee verification page.
One page per employee per statement, reached by an unguessable token link.
  Flag   = one click, toggles a row ("Needs review before the bill is paid.")
  Submit = one click, approves all unflagged rows, appends to the log on the
           persistent disk, and emails a record to RECORD_TO automatically.
"""
import csv, hashlib, hmac, html, io, json, os, smtplib, ssl, threading, base64
from datetime import datetime
from email.message import EmailMessage
from zoneinfo import ZoneInfo
from flask import Flask, request, abort, Response, jsonify, redirect

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get('DATA_DIR', '/var/data')
LOG = os.path.join(DATA_DIR, 'log.jsonl')
OUTBOX = os.path.join(DATA_DIR, 'outbox')
TOKEN_SECRET = os.environ.get('TOKEN_SECRET', '')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', '')
PUBLIC_BASE = os.environ.get('PUBLIC_BASE', 'https://amex.coxresearch.com').rstrip('/')
EMAIL_MODE = os.environ.get('EMAIL_MODE', 'dryrun')          # dryrun | smtp | resend
RECORD_TO = os.environ.get('RECORD_TO', 'littledude@coxresearch.com')
RECORD_CC = os.environ.get('RECORD_CC', '')                   # optional, e.g. chris@coxresearch.com
MAIL_FROM = os.environ.get('MAIL_FROM', 'Chris AI <littledude@coxresearch.com>')
TZ = ZoneInfo('America/Chicago')
LOCK = threading.Lock()
SUBMIT_LOCK = threading.Lock()   # run gunicorn with 1 worker so these locks are global
app = Flask(__name__)
os.makedirs(OUTBOX, exist_ok=True)

# ---------- statements & tokens ----------
def load_statements():
    """Repo statements/ plus any uploaded to DATA_DIR/statements (disk wins)."""
    out = {}
    for d in (os.path.join(APP_DIR, 'statements'), os.path.join(DATA_DIR, 'statements')):
        if os.path.isdir(d):
            for f in sorted(os.listdir(d)):
                if f.endswith('.json'):
                    doc = json.load(open(os.path.join(d, f)))
                    out[doc['stmt']] = doc
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
    flags, submitted = set(), None
    for r in read_log():
        if r.get('stmt') != stmt or r.get('emp_id') != emp_id: continue
        if r['type'] == 'flag': flags.add(r['row'])
        elif r['type'] == 'unflag': flags.discard(r['row'])
        elif r['type'] == 'submit': submitted = r
        elif r['type'] == 'reset': flags, submitted = set(), None
    return flags, submitted

# ---------- email ----------
def money(a): return f'${a:,.2f}'

def record_email(doc, e, flagged, approved, ts):
    subj = f"Amex verification SUBMITTED: {e['name']} - {doc['label']} - {len(flagged)} flagged"
    def tbl(rows):
        if not rows: return '<p><i>None</i></p>'
        t = ''.join(f"<tr><td style='padding:4px 8px'>{r['date']}</td><td style='padding:4px 8px'>{html.escape(r['category'])}</td>"
                    f"<td style='padding:4px 8px'>{html.escape(r['company'])}</td><td style='padding:4px 8px;text-align:right'>{money(r['amount'])}</td></tr>" for r in rows)
        return f"<table style='border-collapse:collapse;font-size:13px' border='1' cellspacing='0'><tr><th>Date</th><th>Description</th><th>Company</th><th>Amount</th></tr>{t}</table>"
    ft = sum(r['amount'] for r in flagged); at = sum(r['amount'] for r in approved)
    body = (f"<div style='font-family:Arial,Helvetica,sans-serif'><p><b>{html.escape(e['name'])}</b> submitted the {html.escape(doc['label'])} "
            f"(statement {doc['stmt']}) on {nice(ts)}.</p>"
            f"<p><b>Flagged - needs review before the bill is paid ({len(flagged)}, {money(ft)}):</b></p>{tbl(flagged)}"
            f"<p><b>Approved ({len(approved)}, {money(at)}):</b></p>{tbl(approved)}"
            f"<p style='color:#777;font-size:12px'>Automatic record from {PUBLIC_BASE}. Saved to the log on the server.</p></div>")
    text = (f"{e['name']} submitted {doc['label']} ({doc['stmt']}) at {ts}.\nFlagged ({len(flagged)}, {money(ft)}):\n" +
            ''.join(f"  {r['date']} {r['category']} | {r['company']} | {money(r['amount'])}\n" for r in flagged) +
            f"Approved ({len(approved)}, {money(at)}).\n")
    return subj, body, text

def send_email(subj, body, text):
    to = [x.strip() for x in RECORD_TO.split(',') if x.strip()]
    cc = [x.strip() for x in RECORD_CC.split(',') if x.strip()]
    msg = EmailMessage(); msg['Subject'] = subj; msg['From'] = MAIL_FROM; msg['To'] = ', '.join(to)
    if cc: msg['Cc'] = ', '.join(cc)
    msg.set_content(text); msg.add_alternative(body, subtype='html')
    if EMAIL_MODE == 'smtp':
        host = os.environ.get('SMTP_HOST', 'smtp.office365.com'); port = int(os.environ.get('SMTP_PORT', '587'))
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls(context=ssl.create_default_context())
            s.login(os.environ['SMTP_USER'], os.environ['SMTP_PASSWORD'])
            s.send_message(msg)
        return 'sent-smtp'
    if EMAIL_MODE == 'resend':
        import urllib.request
        req = urllib.request.Request('https://api.resend.com/emails', method='POST',
            data=json.dumps({'from': MAIL_FROM, 'to': to, 'cc': cc, 'subject': subj, 'html': body, 'text': text}).encode(),
            headers={'Authorization': 'Bearer ' + os.environ['RESEND_API_KEY'], 'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=30) as r: r.read()
        return 'sent-resend'
    fn = os.path.join(OUTBOX, datetime.now(TZ).strftime('%Y%m%d-%H%M%S-') + hashlib.sha1(subj.encode()).hexdigest()[:6] + '.eml')
    open(fn, 'wb').write(bytes(msg)); return 'dryrun:' + fn

# ---------- page ----------
TH = 'background:#006fcf;color:#ffffff;padding:12px;text-align:left;font-size:15px;white-space:nowrap'
TD = 'padding:14px 12px;border-bottom:1px solid #d9dde3;font-size:15px;vertical-align:middle'
CSS = """body{margin:0;background:#fff;font-family:Arial,Helvetica,sans-serif;color:#222}
.wrap{max-width:1000px;margin:0 auto;padding:16px}
.fl{display:inline-block;min-width:90px;text-align:center;padding:12px 28px;border:2px solid #e0b000;border-radius:6px;background:#ffd600;color:#4a3b00;font-size:16px;font-weight:bold;white-space:nowrap;cursor:pointer}
.fl.on{background:#c62828;border-color:#8e0000;color:#fff}
tr.flagged td{background:#fff3cd!important}
.sub{display:inline-block;min-width:60px;padding:14px 40px;border:2px solid #2e7d32;border-radius:6px;background:#2e7d32;color:#fff;font-size:18px;font-weight:bold;cursor:pointer}
button:disabled{opacity:.6;cursor:default}
.done{background:#e8f5e9;border:1px solid #2e7d32;border-radius:8px;padding:14px 16px;font-size:16px;margin:12px 0}
@media(max-width:640px){.wrap{padding:8px}.fl{padding:9px 8px!important;min-width:0!important;font-size:13px!important}.fl .t{display:none}td,th{padding:9px 5px!important;font-size:13px!important}}"""

JS = """document.querySelectorAll('form.f').forEach(function(f){f.addEventListener('submit',function(ev){ev.preventDefault();
var b=f.querySelector('button');b.disabled=true;fetch(f.action,{method:'POST',headers:{'Accept':'application/json'}}).then(function(r){return r.json()}).then(function(j){
var tr=f.closest('tr');tr.classList.toggle('flagged',j.flagged);b.classList.toggle('on',j.flagged);b.querySelector('.t').textContent=j.flagged?' Flagged':' Flag';
document.getElementById('nf').textContent=j.count;b.disabled=false}).catch(function(){f.submit()})})});"""

def render(doc, e, token, flags, submitted):
    esc = html.escape; rows = e['rows']; locked = submitted is not None
    out = [f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
           f"<meta name='robots' content='noindex'><title>Amex verification - {esc(e['first'])}</title><style>{CSS}</style></head><body><div class='wrap'>",
           f"<p style='margin:8px 0 2px;font-size:20px;font-weight:bold'>{esc(e['first'])}</p>"
           f"<p style='margin:0 0 16px;font-size:14px;color:#555'>{esc(doc.get('due','Due by the first'))}</p>"]
    if locked:
        out.append(f"<div class='done'><b>Submitted {esc(nice(submitted['ts']))}.</b> All unflagged charges approved. "
                   f"{len(submitted['flagged'])} flagged for review. Thank you.</div>")
    out.append(f"<table width='100%' cellspacing='0' cellpadding='0' style='width:100%;border-collapse:collapse;border:1px solid #d9dde3'>"
               f"<tr><th style='{TH}'>Date</th><th style='{TH}'>Description</th><th style='{TH}'>Company</th>"
               f"<th style='{TH};text-align:right'>Amount</th><th style='{TH};text-align:center'>Flag</th></tr>")
    for i, r in enumerate(rows):
        on = r['id'] in flags; bg = '#ffffff' if i % 2 == 0 else '#f5f8fc'
        btn = (f"<form class='f' method='post' action='/v/{token}/flag/{r['id']}' style='margin:0'>"
               f"<button class='fl{' on' if on else ''}'{' disabled' if locked else ''}>&#9873;<span class='t'> {'Flagged' if on else 'Flag'}</span></button></form>")
        out.append(f"<tr class='{'flagged' if on else ''}' style='background:{bg}'><td style='{TD}'><b>{esc(r['date'][:5])}</b></td>"
                   f"<td style='{TD}'>{esc(r['category'])}</td><td style='{TD}'>{esc(r['company'])}</td>"
                   f"<td style='{TD};text-align:right'><b>{money(r['amount'])}</b></td><td style='padding:14px 8px;border-bottom:1px solid #d9dde3;text-align:center'>{btn}</td></tr>")
    tot = sum(r['amount'] for r in rows)
    out.append(f"<tr><td colspan='3' style='padding:14px 12px;font-size:16px;font-weight:bold'>Total ({len(rows)} charges)</td>"
               f"<td style='padding:14px 12px;font-size:16px;font-weight:bold;text-align:right'>{money(tot)}</td>"
               f"<td style='padding:14px 8px;text-align:center;font-size:14px;color:#555'><span id='nf'>{len(flags)}</span> flagged</td></tr></table>")
    if not locked:
        out.append(f"<form method='post' action='/v/{token}/submit' style='text-align:center;margin:24px 0'>"
                   f"<button class='sub' onclick=\"this.disabled=true;this.form.submit()\">Submit</button></form>")
    out.append("<table width='100%' style='width:100%;border-top:2px solid #d9dde3;font-size:15px;margin-top:16px'><tr><td style='padding-top:14px'>"
               "<p style='font-weight:bold;font-size:17px;margin:0 0 10px'>Key</p>"
               "<p style='margin:8px 0'><span style='display:inline-block;width:90px;font-weight:bold;color:#b38f00'>&#9873; Flag</span>Needs review before the bill is paid.</p>"
               "<p style='margin:8px 0'><span style='display:inline-block;width:90px;font-weight:bold;color:#2e7d32'>Submit</span>Approves all unflagged charges on this chart.</p></td></tr></table>"
               "<p style='font-size:14px;margin-top:20px'>Chris AI<br>Cox Research, Assistant<br>2252780616<br>Littledude@coxresearch.com<br><br>https://coxresearch.com</p>"
               f"</div><script>{JS}</script></body></html>")
    return ''.join(out)

def nice(ts):
    return datetime.fromisoformat(ts).strftime('%b %-d, %Y %-I:%M %p') + ' CT'

def meta():
    return {'ts': now(), 'ip': request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[0].strip(),
            'ua': request.headers.get('User-Agent', '')[:300]}

@app.get('/healthz')
def healthz(): return 'ok'

@app.get('/')
def root(): return Response('Cox Research', mimetype='text/plain')

@app.get('/v/<token>')
def view(token):
    doc, e = resolve(token)
    if not doc: abort(404)
    flags, sub = state(doc['stmt'], e['id'])
    append({'type': 'open', 'stmt': doc['stmt'], 'emp_id': e['id'], 'emp': e['name'], **meta()})
    return Response(render(doc, e, token, flags, sub), headers={'Cache-Control': 'no-store', 'X-Robots-Tag': 'noindex'})

@app.post('/v/<token>/flag/<int:row>')
def flag(token, row):
    doc, e = resolve(token)
    if not doc: abort(404)
    r = next((x for x in e['rows'] if x['id'] == row), None)
    if not r: abort(404)
    flags, sub = state(doc['stmt'], e['id'])
    if sub: return jsonify(error='already submitted', flagged=row in flags, count=len(flags)), 409
    on = row not in flags
    append({'type': 'flag' if on else 'unflag', 'stmt': doc['stmt'], 'emp_id': e['id'], 'emp': e['name'], 'row': row,
            'date': r['date'], 'category': r['category'], 'company': r['company'], 'amount': r['amount'], **meta()})
    count = len(flags) + (1 if on else -1)
    if 'application/json' in request.headers.get('Accept', ''):
        return jsonify(flagged=on, count=count)
    return redirect(f'/v/{token}', 303)

@app.post('/v/<token>/submit')
def submit(token):
    doc, e = resolve(token)
    if not doc: abort(404)
    with SUBMIT_LOCK:
      flags, sub = state(doc['stmt'], e['id'])
      if not sub:
          flagged = [r for r in e['rows'] if r['id'] in flags]; approved = [r for r in e['rows'] if r['id'] not in flags]
          rec = {'type': 'submit', 'stmt': doc['stmt'], 'emp_id': e['id'], 'emp': e['name'], **meta(),
                 'flagged': [r['id'] for r in flagged], 'approved': [r['id'] for r in approved],
                 'flagged_total': round(sum(r['amount'] for r in flagged), 2), 'approved_total': round(sum(r['amount'] for r in approved), 2)}
          append(rec)
          try:
              status = send_email(*record_email(doc, e, flagged, approved, rec['ts']))
          except Exception as ex:
              status = f'error: {type(ex).__name__}: {ex}'
          append({'type': 'email', 'stmt': doc['stmt'], 'emp_id': e['id'], 'emp': e['name'], 'ts': now(),
                  'to': RECORD_TO, 'cc': RECORD_CC, 'status': status})
    return redirect(f'/v/{token}', 303)

# ---------- Chris-only admin ----------
def need_admin():
    a = request.authorization
    if not ADMIN_PASSWORD or not a or not hmac.compare_digest(a.password or '', ADMIN_PASSWORD):
        abort(Response('Login required', 401, {'WWW-Authenticate': 'Basic realm="Cox Amex admin"'}))

@app.get('/admin')
def admin():
    need_admin(); esc = html.escape
    rows = []
    for stmt, doc in sorted(load_statements().items(), reverse=True):
        for e in doc['employees']:
            flags, sub = state(stmt, e['id'])
            link = f"{PUBLIC_BASE}/v/{make_token(stmt, e['id'])}"
            st = f"<b style='color:#2e7d32'>Submitted {esc(nice(sub['ts']))}</b>" if sub else "<b style='color:#c62828'>Not submitted</b>"
            rows.append(f"<tr><td>{stmt}</td><td>{esc(e['name'])}</td><td>{st}</td><td>{len(sub['flagged']) if sub else len(flags)}</td>"
                        f"<td>{len(e['rows'])}</td><td>{money(sum(r['amount'] for r in e['rows']))}</td><td style='font-size:12px'>{esc(link)}</td></tr>")
    return (f"<!doctype html><meta charset='utf-8'><title>Amex admin</title><body style='font-family:Arial;padding:16px'>"
            f"<h2>Amex verification - status</h2><p>Record email: {esc(EMAIL_MODE)} to {esc(RECORD_TO)}{' cc ' + esc(RECORD_CC) if RECORD_CC else ''}. "
            f"<a href='/admin/log.csv'>Download full log (CSV)</a></p>"
            f"<table border='1' cellpadding='6' cellspacing='0'><tr><th>Statement</th><th>Employee</th><th>Status</th><th>Flagged</th><th>Charges</th><th>Total</th><th>Link</th></tr>{''.join(rows)}</table>")

@app.get('/admin/log.csv')
def admin_csv():
    need_admin()
    cols = ['ts', 'type', 'stmt', 'emp_id', 'emp', 'row', 'date', 'category', 'company', 'amount',
            'flagged', 'approved', 'flagged_total', 'approved_total', 'to', 'cc', 'status', 'ip', 'ua']
    buf = io.StringIO(); w = csv.DictWriter(buf, cols, extrasaction='ignore'); w.writeheader()
    for r in read_log():
        w.writerow({k: (';'.join(map(str, v)) if isinstance(v, list) else v) for k, v in r.items()})
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
    d = os.path.join(DATA_DIR, 'statements'); os.makedirs(d, exist_ok=True)
    json.dump(doc, open(os.path.join(d, doc['stmt'] + '.json'), 'w'), indent=1)
    return jsonify(ok=True, stmt=doc['stmt'], employees=len(doc['employees']))

@app.post('/admin/reset/<stmt>/<emp_id>')
def admin_reset(stmt, emp_id):
    """Reopen one employee's page (clears flags + submission; history stays in the log)."""
    need_admin(); append({'type': 'reset', 'stmt': stmt, 'emp_id': emp_id, 'ts': now()})
    return jsonify(ok=True)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', '10000')))
