#!/usr/bin/env python3
"""make_v6-style builder (v7): one email HTML per employee, linking to their
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
    url = f"{BASE}/v/{app.make_token(stmt, e['id'])}"; esc = html.escape
    tot = sum(r['amount'] for r in e['rows'])
    rows = ''.join(f"<tr style='background:{'#ffffff' if i % 2 == 0 else '#f5f8fc'}'><td style='{TD}'><b>{esc(r['date'][:5])}</b></td>"
                   f"<td style='{TD}'>{esc(r['category'])}</td><td style='{TD}'>{esc(r['company'])}</td>"
                   f"<td style='{TD}' align='right'><b>${r['amount']:,.2f}</b></td>"
                   f"<td style='{TD}' align='center'><a href='{esc(url)}?do=flag&amp;row={r['id']}' style='{FLB}'>&#9873; Flag</a></td></tr>" for i, r in enumerate(e['rows']))
    h = (f"<table width='100%' cellspacing='0' cellpadding='0' style='width:100%;font-family:Arial,Helvetica,sans-serif;color:#222'><tr><td style='padding:8px'>"
         f"<p style='margin:8px 0 2px;font-size:20px;font-weight:bold'>{esc(e['first'])}</p>"
         f"<p style='margin:0 0 16px;font-size:14px;color:#555'>{esc(doc.get('due', 'Due by the first'))}</p>"
         f"<p style='font-size:16px;margin:0 0 18px'>Your American Express charges for the {esc(doc['label'])} are ready: "
         f"<b>{len(e['rows'])} charges, ${tot:,.2f}</b>. Tap &#9873; Flag on anything that needs review, then tap Submit.</p>"
         f"<table width='100%' cellspacing='0' cellpadding='0' style='width:100%;border-collapse:collapse;border:1px solid #d9dde3'>"
         f"<tr><th style='{TH}'>Date</th><th style='{TH}'>Description</th><th style='{TH}'>Company</th><th style='{TH}' align='right'>Amount</th><th style='{TH}' align='center'>Flag</th></tr>{rows}"
         f"<tr><td colspan='3' style='padding:14px 12px;font-size:16px;font-weight:bold'>Total ({len(e['rows'])} charges)</td>"
         f"<td style='padding:14px 12px;font-size:16px;font-weight:bold' align='right'>${tot:,.2f}</td><td></td></tr></table>"
         f"<div style='text-align:center;margin:24px 0'><a href='{esc(url)}?do=submit' style='{BTN}'>Submit</a></div>"
         "<table width='100%' cellspacing='0' cellpadding='0' style='width:100%;border-top:2px solid #d9dde3;font-size:15px'><tr><td style='padding-top:14px'>"
         "<p style='font-weight:bold;font-size:17px;margin:0 0 10px'>Key</p>"
         "<p style='margin:8px 0'><span style='display:inline-block;width:90px;font-weight:bold;color:#b38f00'>&#9873; Flag</span>Needs review before the bill is paid.</p>"
         "<p style='margin:8px 0'><span style='display:inline-block;width:90px;font-weight:bold;color:#2e7d32'>Submit</span>Approves all unflagged charges on this chart.</p></td></tr></table>"
         "<p style='font-size:14px;margin-top:20px'>Chris AI<br>Cox Research, Assistant<br>2252780616<br>Littledude@coxresearch.com<br><br>https://coxresearch.com</p></td></tr></table>")
    fn = os.path.join(outdir, e['id'] + '.html'); open(fn, 'w').write(h)
    assert 'mailto' not in h and 'do=submit' in h and 'do=flag' in h
    print(e['id'], len(e['rows']), f'{tot:.2f}', url, fn)
