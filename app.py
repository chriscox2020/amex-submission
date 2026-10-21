"""Cox Research - American Express employee verification page.
One page per employee per statement, reached by an unguessable token link.
  Flag   = click opens an optional note (Save/Cancel, 500 characters). Save
           toggles the row on and stores the note with the flag; clicking a
           flagged row again clears it. "Needs review before the bill is paid."
  Submit = one click, approves all unflagged rows, appends to the log on the
           persistent disk, and emails a record to RECORD_TO automatically.
           Each flagged line is charge date, merchant, amount, flagged date,
           reference (or "ref pending"), then the note.
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
    """Current flags (row id -> {note, ts}) and the latest submit, if any.

    A dict still supports the `in` and `len` checks callers already use.
    Log lines written before notes existed have no note key; those stay valid.
    """
    flags, submitted = {}, None
    for r in read_log():
        if r.get('stmt') != stmt or r.get('emp_id') != emp_id: continue
        if r['type'] == 'flag':
            flags[r['row']] = {'note': r.get('note') or '', 'ts': r.get('ts') or ''}
        elif r['type'] == 'unflag': flags.pop(r.get('row'), None)
        elif r['type'] == 'submit': submitted = r
        elif r['type'] == 'reset': flags, submitted = {}, None
    return flags, submitted

# ---------- email ----------
def money(a): return f'${a:,.2f}'

NOTE_MAX = 500
REF_KEYS = ('reference_number', 'reference', 'ref', 'ref_number', 'ref_no', 'referenceNumber')

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

def posted_note():
    """Note from a JSON or form POST. Never from the query string."""
    data = request.get_json(silent=True)
    if isinstance(data, dict) and 'note' in data:
        return clean_note(data.get('note'))
    if 'note' in request.form:
        return clean_note(request.form.get('note'))
    return ''

def record_email(doc, e, flagged, approved, ts):
    subj = f"Amex verification SUBMITTED: {e['name']} - {doc['label']} - {len(flagged)} flagged"
    def tbl(rows):
        if not rows: return '<p><i>None</i></p>'
        t = ''.join(f"<tr><td style='padding:4px 8px'>{r['date']}</td><td style='padding:4px 8px'>{html.escape(r['category'])}</td>"
                    f"<td style='padding:4px 8px'>{html.escape(r['company'])}</td><td style='padding:4px 8px;text-align:right'>{money(r['amount'])}</td></tr>" for r in rows)
        return f"<table style='border-collapse:collapse;font-size:13px' border='1' cellspacing='0'><tr><th>Date</th><th>Description</th><th>Company</th><th>Amount</th></tr>{t}</table>"
    def flag_html(rows):
        if not rows: return '<p><i>None</i></p>'
        return ''.join("<p style='margin:4px 0;font-size:13px'>" + html.escape(flag_line(r, r.get('note', ''), r.get('flagged_ts', ''))) + "</p>" for r in rows)
    ft = sum(r['amount'] for r in flagged); at = sum(r['amount'] for r in approved)
    body = (f"<div style='font-family:Arial,Helvetica,sans-serif'><p><b>{html.escape(e['name'])}</b> submitted the {html.escape(doc['label'])} "
            f"(statement {doc['stmt']}) on {nice(ts)}.</p>"
            f"<p><b>Flagged - needs review before the bill is paid ({len(flagged)}, {money(ft)}):</b></p>{flag_html(flagged)}"
            f"<p><b>Approved ({len(approved)}, {money(at)}):</b></p>{tbl(approved)}"
            f"<p style='color:#777;font-size:12px'>Automatic record from {PUBLIC_BASE}. Saved to the log on the server.</p></div>")
    text = (f"{e['name']} submitted {doc['label']} ({doc['stmt']}) at {ts}.\nFlagged ({len(flagged)}, {money(ft)}):\n" +
            ''.join(f"  {flag_line(r, r.get('note', ''), r.get('flagged_ts', ''))}\n" for r in flagged) +
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
#flagbox{background:#fff3cd;border:1px solid #e0b000;border-radius:8px;padding:12px 16px;margin:16px 0}
#flagbox[hidden]{display:none!important}
#flagbox li,.done li{margin:6px 0;font-size:14px;line-height:1.4}
#notebox{position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:30;display:flex;align-items:center;justify-content:center;padding:16px}
#notebox[hidden]{display:none!important}
.notecard{background:#fff;color:#222;border-radius:8px;padding:18px 18px 14px;max-width:480px;width:100%;box-shadow:0 8px 28px rgba(0,0,0,.25)}
.notecard textarea{width:100%;box-sizing:border-box;margin-top:6px;font:16px Arial,Helvetica,sans-serif;padding:8px;border:1px solid #c5c9d1;border-radius:4px;resize:vertical}
.notesave{margin-left:8px;padding:10px 22px;border:2px solid #2e7d32;border-radius:6px;background:#2e7d32;color:#fff;font-size:16px;font-weight:bold;cursor:pointer}
.notecancel{padding:10px 18px;border:1px solid #c5c9d1;border-radius:6px;background:#fff;color:#333;font-size:16px;cursor:pointer}
@media(max-width:640px){.wrap{padding:8px}.fl{padding:9px 8px!important;min-width:0!important;font-size:13px!important}.fl .t{display:none}td,th{padding:9px 5px!important;font-size:13px!important}}"""

JS = """var pending=null;
function noteBox(){return document.getElementById('notebox')}
function openNote(f){
  pending=f;
  var ta=document.getElementById('note');
  ta.value='';
  document.getElementById('notecount').textContent='0';
  var tds=f.closest('tr').querySelectorAll('td');
  document.getElementById('notewho').textContent=(tds[0].innerText+' \\u00b7 '+tds[2].innerText+' \\u00b7 '+tds[3].innerText).replace(/\\s+/g,' ').trim();
  noteBox().hidden=false;
  ta.focus();
}
function closeNote(){pending=null;noteBox().hidden=true}
function placeLi(list,li,row){
  var kids=list.children;
  for(var i=0;i<kids.length;i++){
    if(parseInt(kids[i].getAttribute('data-row'),10)>row){list.insertBefore(li,kids[i]);return}
  }
  list.appendChild(li);
}
function applyFlag(f,j){
  var b=f.querySelector('button');
  var tr=f.closest('tr');
  tr.classList.toggle('flagged',!!j.flagged);
  b.classList.toggle('on',!!j.flagged);
  b.querySelector('.t').textContent=j.flagged?' Flagged':' Flag';
  document.getElementById('nf').textContent=j.count;
  var list=document.getElementById('flaglist');
  var box=document.getElementById('flagbox');
  if(list){
    var li=list.querySelector('li[data-row=\"'+j.row+'\"]');
    if(j.flagged){
      if(!li){li=document.createElement('li');li.setAttribute('data-row',String(j.row));placeLi(list,li,j.row)}
      li.textContent=j.line||'';
      if(box) box.hidden=false;
    }else if(li){
      li.remove();
      if(box && !list.children.length) box.hidden=true;
    }
  }
  b.disabled=false;
}
function postFlag(f,note){
  var b=f.querySelector('button');
  b.disabled=true;
  var hid=f.querySelector('input[name=note]');
  if(hid) hid.value=note==null?'':note;
  var opts={method:'POST',headers:{'Accept':'application/json'}};
  if(note!=null){opts.headers['Content-Type']='application/x-www-form-urlencoded';opts.body='note='+encodeURIComponent(note)}
  fetch(f.action,opts).then(function(r){
    return r.json().then(function(j){return {ok:r.ok,j:j}},function(){throw new Error('bad')});
  }).then(function(x){if(!x.ok){b.disabled=false;return}applyFlag(f,x.j)}).catch(function(){f.submit()});
}
document.querySelectorAll('form.f').forEach(function(f){f.addEventListener('submit',function(ev){
  ev.preventDefault();
  var b=f.querySelector('button');
  if(b.disabled) return;
  if(!noteBox().hidden){if(!b.classList.contains('on')) openNote(f);return}
  if(b.classList.contains('on')) postFlag(f,null); else openNote(f);
})});
document.getElementById('notesave').addEventListener('click',function(){
  if(!pending) return;
  var f=pending, note=document.getElementById('note').value.slice(0,500);
  closeNote(); postFlag(f,note);
});
document.getElementById('notecancel').addEventListener('click',closeNote);
document.getElementById('note').addEventListener('input',function(){document.getElementById('notecount').textContent=String(this.value.length)});
document.addEventListener('keydown',function(ev){if(!noteBox().hidden && ev.key==='Escape'){ev.preventDefault();closeNote()}});
noteBox().addEventListener('click',function(ev){if(ev.target===noteBox()) closeNote()});"""

def render(doc, e, token, flags, submitted):
    esc = html.escape; rows = e['rows']; locked = submitted is not None
    lis = flag_items_html(rows, flags)
    out = [f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
           f"<meta name='robots' content='noindex'><title>Amex verification - {esc(e['first'])}</title><style>{CSS}</style></head><body><div class='wrap'>",
           f"<p style='margin:8px 0 2px;font-size:20px;font-weight:bold'>{esc(e['first'])}</p>"
           f"<p style='margin:0 0 16px;font-size:14px;color:#555'>{esc(doc.get('due','Due by the first'))}</p>"]
    if locked:
        listed = f"<ul id='flaglist' style='margin:8px 0 0;padding-left:18px'>{lis}</ul>" if lis else ''
        out.append(f"<div class='done'><b>Submitted {esc(nice(submitted['ts']))}.</b> All unflagged charges approved. "
                   f"{len(submitted['flagged'])} flagged for review. Thank you.{listed}</div>")
    out.append(f"<table width='100%' cellspacing='0' cellpadding='0' style='width:100%;border-collapse:collapse;border:1px solid #d9dde3'>"
               f"<tr><th style='{TH}'>Date</th><th style='{TH}'>Description</th><th style='{TH}'>Company</th>"
               f"<th style='{TH};text-align:right'>Amount</th><th style='{TH};text-align:center'>Flag</th></tr>")
    for i, r in enumerate(rows):
        on = r['id'] in flags; bg = '#ffffff' if i % 2 == 0 else '#f5f8fc'
        btn = (f"<form class='f' method='post' action='/v/{token}/flag/{r['id']}' style='margin:0'>"
               f"<input type='hidden' name='note' value=''>"
               f"<button class='fl{' on' if on else ''}'{' disabled' if locked else ''}>&#9873;<span class='t'> {'Flagged' if on else 'Flag'}</span></button></form>")
        out.append(f"<tr class='{'flagged' if on else ''}' style='background:{bg}'><td style='{TD}'><b>{esc(r['date'][:5])}</b></td>"
                   f"<td style='{TD}'>{esc(r['category'])}</td><td style='{TD}'>{esc(r['company'])}</td>"
                   f"<td style='{TD};text-align:right'><b>{money(r['amount'])}</b></td><td style='padding:14px 8px;border-bottom:1px solid #d9dde3;text-align:center'>{btn}</td></tr>")
    tot = sum(r['amount'] for r in rows)
    out.append(f"<tr><td colspan='3' style='padding:14px 12px;font-size:16px;font-weight:bold'>Total ({len(rows)} charges)</td>"
               f"<td style='padding:14px 12px;font-size:16px;font-weight:bold;text-align:right'>{money(tot)}</td>"
               f"<td style='padding:14px 8px;text-align:center;font-size:14px;color:#555'><span id='nf'>{len(flags)}</span> flagged</td></tr></table>")
    if not locked:
        hidden = '' if lis else ' hidden'
        out.append(f"<div id='flagbox'{hidden}><p style='font-weight:bold;margin:0 0 8px'>Flagged for review</p>"
                   f"<ul id='flaglist' style='margin:0;padding-left:18px'>{lis}</ul></div>")
        out.append(f"<form method='post' action='/v/{token}/submit' style='text-align:center;margin:24px 0'>"
                   f"<button class='sub' onclick=\"this.disabled=true;this.form.submit()\">Submit</button></form>")
    out.append("<table width='100%' style='width:100%;border-top:2px solid #d9dde3;font-size:15px;margin-top:16px'><tr><td style='padding-top:14px'>"
               "<p style='font-weight:bold;font-size:17px;margin:0 0 10px'>Key</p>"
               "<p style='margin:8px 0'><span style='display:inline-block;width:90px;font-weight:bold;color:#b38f00'>&#9873; Flag</span>Needs review before the bill is paid.</p>"
               "<p style='margin:8px 0'><span style='display:inline-block;width:90px;font-weight:bold;color:#2e7d32'>Submit</span>Approves all unflagged charges on this chart.</p></td></tr></table>"
               "<p style='font-size:14px;margin-top:20px'>Chris AI<br>Cox Research, Assistant<br>2252780616<br>Littledude@coxresearch.com<br><br>https://coxresearch.com</p>"
               "<div id='notebox' hidden><div class='notecard' role='dialog' aria-modal='true' aria-labelledby='notetitle'>"
               "<p id='notetitle' style='margin:0 0 6px;font-size:18px;font-weight:bold'>Flag this charge</p>"
               "<p id='notewho' style='margin:0 0 10px;font-size:14px;color:#555'></p>"
               "<label for='note' style='font-size:14px'>Reason note (optional)</label>"
               "<textarea id='note' maxlength='500' rows='4' placeholder='Why does this need review?'></textarea>"
               "<p style='margin:6px 0 12px;font-size:12px;color:#777;text-align:right'><span id='notecount'>0</span>/500</p>"
               "<div style='text-align:right'><button type='button' id='notecancel' class='notecancel'>Cancel</button>"
               "<button type='button' id='notesave' class='notesave'>Save</button></div></div></div>"
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
    page = render(doc, e, token, flags, sub)
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
    flags, sub = state(doc['stmt'], e['id'])
    if sub: return jsonify(error='already submitted', flagged=row in flags, count=len(flags)), 409
    on = row not in flags
    rec = {'type': 'flag' if on else 'unflag', 'stmt': doc['stmt'], 'emp_id': e['id'], 'emp': e['name'], 'row': row,
           'date': r['date'], 'category': r['category'], 'company': r['company'], 'amount': r['amount'], **meta()}
    if on: rec['note'] = posted_note()
    append(rec)
    count = len(flags) + (1 if on else -1)
    if 'application/json' in request.headers.get('Accept', ''):
        payload = {'flagged': on, 'count': count, 'row': row}
        if on: payload['line'] = flag_line(r, rec.get('note', ''), rec['ts'])
        return jsonify(payload)
    return redirect(f'/v/{token}', 303)

@app.post('/v/<token>/submit')
def submit(token):
    doc, e = resolve(token)
    if not doc: abort(404)
    with SUBMIT_LOCK:
      flags, sub = state(doc['stmt'], e['id'])
      if not sub:
          flagged = attach_flag_meta([r for r in e['rows'] if r['id'] in flags], flags)
          approved = [r for r in e['rows'] if r['id'] not in flags]
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
            'flagged', 'approved', 'flagged_total', 'approved_total', 'to', 'cc', 'status', 'ip', 'ua', 'note']
    buf = io.StringIO(); w = csv.DictWriter(buf, cols, extrasaction='ignore'); w.writeheader()
    for r in read_log():
        row = {}
        for k, v in r.items():
            if isinstance(v, list): v = ';'.join(map(str, v))
            elif k == 'note' and isinstance(v, str) and v[:1] in '=+-@\t\r': v = "'" + v
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
