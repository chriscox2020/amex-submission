#!/usr/bin/env python3
"""Browser check of the monthly summary page. Does not send email."""
import json, os, pathlib, shutil, tempfile, threading, urllib.request
from base64 import b64encode
from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parents[1]
TMP = tempfile.mkdtemp(prefix='amex-summary-browser-')
os.environ['DATA_DIR'] = TMP
os.environ['TOKEN_SECRET'] = 'local-test-secret'
os.environ['ADMIN_PASSWORD'] = 'local-admin'
os.environ['EMAIL_MODE'] = 'dryrun'
os.environ['RECORD_TO'] = 'other@example.com'
os.environ['RECORD_CC'] = ''
os.environ['PUBLIC_BASE'] = 'http://127.0.0.1:10079'
os.environ['MAIL_FROM'] = 'Chris AI <littledude@coxresearch.com>'

import sys
sys.path.insert(0, str(ROOT))
import app

AUG = '2026-08-22'
DOC = {
    'stmt': AUG,
    'label': 'Aug 22, 2026 statement',
    'close_date': '2026-08-22',
    'employees': [
        {
            'id': 'ada', 'name': 'Ada', 'first': 'Ada', 'email': 'ada@coxresearch.com',
            'rows': [
                {'id': 1, 'date': '08/01/2026', 'category': 'Restaurant', 'company': 'CAFE', 'amount': 10.00},
                {'id': 2, 'date': '08/02/2026', 'category': 'Travel', 'company': 'AIR', 'amount': 5.00},
                {'id': 3, 'date': '08/03/2026', 'category': 'Merchandise & Supplies', 'company': 'STORE', 'amount': -1.00},
            ],
        },
        {
            'id': 'ben', 'name': 'Ben', 'first': 'Ben', 'email': 'ben@coxresearch.com',
            'rows': [
                {'id': 1, 'date': '08/04/2026', 'category': 'Merchandise & Supplies', 'company': 'SHOP', 'amount': 3.50},
            ],
        },
    ],
}
d = pathlib.Path(app.DATA_DIR, 'statements')
d.mkdir(parents=True, exist_ok=True)
(d / (AUG + '.json')).write_text(json.dumps(DOC))

app.now = lambda: '2026-10-21T09:30:00-05:00'
PORT = 10079
BASE = f'http://127.0.0.1:{PORT}'
threading.Thread(target=lambda: app.app.run(host='127.0.0.1', port=PORT, threaded=True, use_reloader=False),
                 daemon=True).start()
for _ in range(50):
    try:
        urllib.request.urlopen(BASE + '/healthz', timeout=0.2)
        break
    except Exception:
        import time
        time.sleep(0.1)
else:
    raise SystemExit('server did not start')

ada = app.make_token(AUG, 'ada')
ben = app.make_token(AUG, 'ben')


def post(path, data=None):
    body = urllib.parse.urlencode(data or {}).encode()
    req = urllib.request.Request(BASE + path, data=body, method='POST')
    with urllib.request.urlopen(req) as res:
        res.read()


import urllib.parse
shots = ROOT / 'screenshots'
shots.mkdir(exist_ok=True)
exe = shutil.which('google-chrome') or shutil.which('chromium')

with sync_playwright() as p:
    browser = p.chromium.launch(executable_path=exe, args=['--no-sandbox'])
    desktop = browser.new_context(
        http_credentials={'username': 'Chris', 'password': 'local-admin'},
        viewport={'width': 1200, 'height': 1000},
    )
    pg = desktop.new_page()
    pg.goto(BASE + f'/admin/summary/{AUG}')
    assert pg.locator('#summary-warning').inner_text() == (
        'Summary total $0.00 does not equal the statement total $17.50. '
        'Difference: $17.50. Not everyone has submitted.')
    assert pg.locator('#qb-submit').is_disabled()
    assert pg.locator('#qb-submit').inner_text() == 'Submit to QuickBooks for Pay'
    assert 'Not submitted' in pg.locator('body').inner_text()
    assert 'Merchandise & Supplies' in pg.locator('body').inner_text()
    assert pg.locator('#sent').count() == 0
    with pg.expect_download() as download_info:
        pg.get_by_role('link', name='Download PDF').click()
    pdf = pathlib.Path(download_info.value.path()).read_bytes()
    assert download_info.value.suggested_filename == 'American Express charges August 2026 - Summary.pdf'
    assert pdf.startswith(b'%PDF')
    assert 'Month folder: American Express charges August 2026.' in pg.locator('body').inner_text()
    pg.screenshot(path=str(shots / 'summary-waiting.png'), full_page=True)

    mobile = browser.new_context(
        http_credentials={'username': 'Meghan', 'password': 'local-admin'},
        viewport={'width': 390, 'height': 844},
    )
    phone = mobile.new_page()
    phone.goto(BASE + f'/admin/summary/{AUG}')
    assert phone.locator('#qb-submit').is_disabled()
    assert 'Difference: $17.50.' in phone.locator('#summary-warning').inner_text()
    assert 'Signed in as Meghan' in phone.locator('body').inner_text()
    phone.screenshot(path=str(shots / 'summary-waiting-mobile.png'), full_page=True)

    post(f'/v/{ada}/category/2', {'category': 'Entertainment'})
    post(f'/v/{ada}/submit')
    pg.reload()
    assert pg.locator('#qb-submit').is_disabled()
    assert 'Difference: $3.50.' in pg.locator('#summary-warning').inner_text()
    assert 'Not submitted' in pg.locator('tr').nth(2).inner_text()
    post(f'/v/{ben}/submit')
    pg.reload()
    assert pg.locator('#summary-warning').count() == 0
    assert pg.locator('#qb-submit').is_enabled()
    assert 'Grand total' in pg.locator('body').inner_text()
    assert '$17.50' in pg.locator('body').inner_text()
    pg.locator('#qb-submit').click()
    pg.locator('#sent').wait_for()
    assert pg.locator('#sent').inner_text() == 'Sent for posting'
    assert 'Oct 21, 2026 9:30 AM CT by Chris.' in pg.locator('#sent-when').inner_text()
    assert pg.locator('#qb-submit').count() == 0
    pg.screenshot(path=str(shots / 'summary-sent.png'), full_page=True)

    employee = browser.new_context(viewport={'width': 1200, 'height': 900}).new_page()
    employee.goto(BASE + f'/v/{ada}')
    assert employee.locator('.banner').inner_text() == 'American Express Charges'
    assert employee.locator('#qb-submit').count() == 0
    assert 'Submit to QuickBooks for Pay' not in employee.content()
    assert 'qb-submit' not in employee.content()
    employee.screenshot(path=str(shots / 'employee-no-qb-button.png'), full_page=True)
    browser.close()

print('BROWSER_SUMMARY_OK')
