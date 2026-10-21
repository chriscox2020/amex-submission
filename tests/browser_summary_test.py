#!/usr/bin/env python3
"""Browser check of the monthly summary page. Does not send email."""
import email, json, os, pathlib, shutil, tempfile, threading, urllib.request
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
    picker_page = browser.new_context(viewport={'width': 1100, 'height': 900}).new_page()
    picker_page.goto(BASE + f'/v/{ada}')
    assert picker_page.locator('.banner').inner_text() == 'American Express Charges'
    assert picker_page.locator('td.cat').nth(0).inner_text() == 'Meals'
    assert picker_page.locator('td.cat').nth(1).inner_text() == 'Travel'
    assert 'Restaurant' not in picker_page.locator('body').inner_text()
    assert 'Merchandise' not in picker_page.locator('body').inner_text()
    picker_page.locator('td.cat').nth(0).click()
    picker_page.locator('.dd').wait_for(state='visible')
    picker = picker_page.locator('.dd .ddo').all_inner_texts()
    assert picker == ['Travel', 'Fuel', 'Meals', 'Supplies', 'Parts & Materials',
                      'Software', 'Entertainment', 'Other', 'Cancel'], picker
    picker_page.locator('td.cat').nth(0).scroll_into_view_if_needed()
    picker_page.screenshot(path=str(shots / 'category-picker.png'))
    picker_page.locator('.dd .ddo.cx').click()
    picker_page.close()

    desktop = browser.new_context(
        http_credentials={'username': 'Chris', 'password': 'local-admin'},
        viewport={'width': 1200, 'height': 1000},
    )
    pg = desktop.new_page()
    pg.goto(BASE + f'/admin/summary/{AUG}')
    assert pg.locator('#summary-warning').inner_text() == (
        'Category total $0.00 does not match the Amex statement total $17.50. '
        'Difference: $17.50. Not everyone has submitted.')
    assert pg.locator('#amex-total').inner_text() == 'Amex statement total $17.50'
    assert pg.locator('#qb-submit').is_disabled()
    assert pg.locator('#qb-submit').inner_text() == 'Submit to QuickBooks for Pay'
    assert 'Not submitted' in pg.locator('body').inner_text()
    assert 'Merchandise' not in pg.locator('body').inner_text()
    assert 'Restaurant' not in pg.locator('body').inner_text()
    assert 'Office Supplies' not in pg.locator('body').inner_text()
    grid_names = pg.locator('#category-grid input.amt').evaluate_all(
        "els => els.map(e => e.getAttribute('data-category'))")
    assert grid_names == ['Travel', 'Fuel', 'Meals', 'Supplies', 'Parts & Materials',
                          'Software', 'Entertainment', 'Other'], grid_names
    assert pg.locator('input[data-category="Fuel"]').input_value() == '$0.00'
    assert pg.locator('input[data-category="Parts & Materials"]').input_value() == '$0.00'
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
    assert phone.locator('#amex-total').inner_text() == 'Amex statement total $17.50'
    assert phone.locator('input[data-category="Meals"]').is_visible()
    assert 'Signed in as Meghan' in phone.locator('body').inner_text()
    phone.screenshot(path=str(shots / 'summary-waiting-mobile.png'), full_page=True)

    post(f'/v/{ada}/category/2', {'category': 'Entertainment'})
    post(f'/v/{ada}/submit')
    pg.reload()
    assert pg.locator('#qb-submit').is_disabled()
    pg.locator('#qb-submit').click(force=True)
    assert pg.locator('#qb-confirm').is_hidden()
    assert 'Difference: $3.50.' in pg.locator('#summary-warning').inner_text()
    assert 'Not submitted' in pg.locator('#cardholders tr').nth(2).inner_text()
    assert pg.locator('input[data-category="Entertainment"]').input_value() == '$5.00'
    assert pg.locator('input[data-category="Meals"]').input_value() == '$10.00'
    assert pg.locator('input[data-category="Supplies"]').input_value() == '-$1.00'
    assert pg.locator('#running-sum').inner_text() == '$14.00'
    pg.screenshot(path=str(shots / 'summary-category-grid.png'), full_page=True)
    post(f'/v/{ben}/submit')
    pg.reload()
    assert pg.locator('#summary-warning').count() == 0
    assert pg.locator('#qb-submit').is_enabled()
    assert pg.locator('#running-sum').inner_text() == '$17.50'
    assert '$17.50' in pg.locator('#amex-total').inner_text()

    def saved_subjects():
        found = []
        for path in pathlib.Path(app.OUTBOX).glob('*.eml'):
            msg = email.message_from_bytes(path.read_bytes())
            found.append(''.join(str(msg['Subject']).splitlines()))
        return found

    assert not any(s.startswith('Amex summary grid SAVED:') for s in saved_subjects())
    pg.locator('#save-categories').click()
    pg.locator('#grid-saved').wait_for()
    assert pg.locator('#grid-saved').inner_text() == 'Saved Oct 21, 2026 9:30 AM CT by Chris.'
    assert saved_subjects().count('Amex summary grid SAVED: American Express charges August 2026') == 1
    fuel = pg.locator('input[data-category="Fuel"]')
    fuel.fill('1.00')
    assert pg.locator('#running-sum').inner_text() == '$18.50'
    assert 'Category total $18.50 does not match the Amex statement total $17.50.' in pg.locator('#summary-warning').inner_text()
    assert 'Difference: -$1.00.' in pg.locator('#summary-warning').inner_text()
    assert pg.locator('#qb-submit').is_disabled()
    fuel.fill('$0.00')
    fuel.blur()
    pg.wait_for_timeout(300)
    assert len([r for r in app.read_log() if r.get('kind') == 'summary_grid']) == 1
    assert pg.locator('#summary-warning').count() == 0
    assert pg.locator('#qb-submit').is_enabled()
    assert pg.locator('#qb-confirm').is_hidden()
    pg.locator('#qb-submit').click()
    pg.locator('#qb-confirm').wait_for(state='visible')
    assert pg.locator('#qb-confirm-text').inner_text() == (
        'Are you sure? This will submit the August 2026 category totals.')
    assert pg.locator('#qb-yes').inner_text() == 'Yes'
    assert pg.locator('#qb-cancel').inner_text() == 'Cancel'
    pg.screenshot(path=str(shots / 'qb-confirm.png'))
    qb_before = [r for r in app.read_log() if r['type'] == 'qb_submit']
    pg.locator('#qb-cancel').click()
    assert pg.locator('#qb-confirm').is_hidden()
    assert pg.locator('#sent').count() == 0
    assert pg.locator('#qb-submit').is_enabled()
    assert [r for r in app.read_log() if r['type'] == 'qb_submit'] == qb_before
    assert not any(s.startswith('QB SUBMIT:') for s in saved_subjects())
    assert not any(s.startswith('American Express summary:') for s in saved_subjects())
    pg.locator('#qb-submit').click()
    pg.locator('#qb-confirm').wait_for(state='visible')
    pg.locator('#qb-yes').click()
    pg.locator('#sent').wait_for()
    assert pg.locator('#sent').inner_text() == 'Sent for posting'
    assert 'Oct 21, 2026 9:30 AM CT by Chris.' in pg.locator('#sent-when').inner_text()
    assert pg.locator('#qb-submit').count() == 0
    owner_mail = []
    for path in pathlib.Path(app.OUTBOX).glob('*.eml'):
        msg = email.message_from_bytes(path.read_bytes())
        if ''.join(str(msg['Subject']).splitlines()) == 'American Express summary: August 2026':
            owner_mail.append(msg)
    assert len(owner_mail) == 1, saved_subjects()
    assert owner_mail[0]['To'] == 'chris@coxresearch.com, meghan@coxresearch.com'
    assert owner_mail[0]['Cc'] is None
    assert 'ada@' not in str(owner_mail[0]['To'])
    assert 'ben@' not in str(owner_mail[0]['To'])
    pg.screenshot(path=str(shots / 'summary-sent.png'), full_page=True)

    employee = browser.new_context(viewport={'width': 1100, 'height': 900}).new_page()
    employee.goto(BASE + f'/v/{ada}')
    assert employee.locator('.banner').inner_text() == 'American Express Charges'
    assert employee.locator('td.cat').count() == 0
    assert 'Restaurant' not in employee.locator('body').inner_text()
    assert 'Merchandise' not in employee.locator('body').inner_text()
    assert employee.locator('#qb-submit').count() == 0
    assert 'Submit to QuickBooks for Pay' not in employee.content()
    assert 'qb-submit' not in employee.content()
    employee.screenshot(path=str(shots / 'employee-no-qb-button.png'), full_page=True)
    browser.close()

print('BROWSER_SUMMARY_OK')
