#!/usr/bin/env python3
"""STANDARD TEMPLATE (Chris, Oct 18 2026): "<Name>'s Amex charges", then name, due line, short instructions, and one Open my charges button. No charges in the email.
make_v6-style builder (v7): one email HTML per employee, linking to their
private page on https://amex.coxresearch.com.  The email has NO flag/submit
GET links on purpose - Outlook Safe Links / scanners open links automatically,
which would fake flags. All flagging and submitting happens on the page (POST).

  TOKEN_SECRET=<same value as on Render> python tools/make_email.py 2026-09-22 [emp-id ...]
Writes out/<stmt>/<emp-id>.html and prints each link.  Builds files only; sends nothing."""
import html, json, os, sys
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ.setdefault('DATA_DIR', os.path.join(ROOT, 'local-data'))
BASE = os.environ.get('PUBLIC_BASE', 'https://amex.coxresearch.com').rstrip('/')
import app
stmt = sys.argv[1]; only = set(sys.argv[2:])
doc = app.load_statements()[stmt]
outdir = os.path.join(ROOT, 'out', stmt); os.makedirs(outdir, exist_ok=True)
TH = 'background:#006fcf;color:#ffffff;padding:12px;text-align:left;font-size:15px;white-space:nowrap'
TD = 'padding:12px;border-bottom:1px solid #d9dde3;font-size:15px'
FLB = ('display:inline-block;min-width:90px;text-align:center;padding:12px 28px;border:2px solid #e0b000;border-radius:6px;background:#ffd600;color:#4a3b00;font-size:16px;font-weight:bold;text-decoration:none;white-space:nowrap')
BTN = ('display:inline-block;text-align:center;padding:14px 40px;border:2px solid #2e7d32;border-radius:6px;background:#2e7d32;'
       'color:#ffffff;font-size:18px;font-weight:bold;text-decoration:none;white-space:nowrap')
for e in doc['employees']:
    if only and e['id'] not in only: continue
    send_to = e.get('send_to') or e.get('email') or ''
    if e['id'] == 'alice-cox' or ' '.join(e['name'].upper().split()) in ('ALICE E COX', 'ALICE COX'):
        if send_to != 'chris@coxresearch.com':
            raise SystemExit(f"Alice must route to chris@coxresearch.com, got {send_to}")
    url = f"{BASE}/v/{app.make_token(stmt, e['id'])}"; esc = html.escape
    tot = sum(r['amount'] for r in e['rows'])
    rows = ''.join(f"<tr style='background:{'#ffffff' if i % 2 == 0 else '#f5f8fc'}'><td style='{TD}'><b>{esc(r['date'][:5])}</b></td>"
                   f"<td style='{TD}'>{esc(r['category'])}</td><td style='{TD}'>{esc(r['company'])}</td>"
                   f"<td style='{TD}' align='right'><b>${r['amount']:,.2f}</b></td>"
                   f"<td style='{TD}' align='center'><a href='{esc(url)}?do=flag&amp;row={r['id']}' style='{FLB}'>&#9873; Flag</a></td></tr>" for i, r in enumerate(e['rows']))
    first = esc(e['first'])
    h = (f"<table width='100%' cellspacing='0' cellpadding='0' style='width:100%;font-family:Arial,Helvetica,sans-serif;color:#222'><tr><td style='padding:8px'>"
         f"<p style='margin:8px 0 14px;font-size:22px;font-weight:bold'>{first}'s Amex charges</p>"
         f"<p style='margin:0 0 4px;font-size:18px;font-weight:bold'>{first}</p>"
         f"<p style='margin:0 0 16px;font-size:14px;color:#555'>{esc(doc.get('due', 'Due by the first'))}</p>"
         "<p style='font-size:16px;margin:0 0 18px'>Your American Express charges are ready to review. Open your page, tap &#9873; Flag on anything that needs review, then tap Submit.</p>"
         f"<div style='text-align:center;margin:24px 0'><a href='{esc(url)}' style='{BTN}'>Open my charges</a></div>"
         "<p style='font-size:14px;margin-top:20px'>Chris AI<br>Cox Research, Assistant<br>2252780616<br>Littledude@coxresearch.com<br><br>https://coxresearch.com</p></td></tr></table>")
    subject = f"{e['first']}'s Amex charges"
    open(os.path.join(outdir, e['id'] + '.subject.txt'), 'w').write(subject)
    open(os.path.join(outdir, e['id'] + '.to.txt'), 'w').write(send_to + '\n')
    fn = os.path.join(outdir, e['id'] + '.html'); open(fn, 'w').write(h)
    assert 'mailto' not in h and h.count(url) == 1
    print(e['id'], len(e['rows']), f'{tot:.2f}', 'to', send_to, url, fn)
