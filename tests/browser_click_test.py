"""Real-browser test of the employee review page against the local server."""
import base64, json, os, shutil, urllib.request
from playwright.sync_api import sync_playwright

B = 'http://127.0.0.1:10077'
auth = {'Authorization': 'Basic ' + base64.b64encode(b'Chris:local-admin').decode()}
links = json.load(urllib.request.urlopen(urllib.request.Request(B + '/admin/links.json', headers=auth)))['2026-09-22']
assert links['alice-cox']['email'] == 'chris@coxresearch.com'
url = links['chris-cox']['url']
urllib.request.urlopen(urllib.request.Request(B + '/admin/reset/2026-09-22/chris-cox', method='POST', headers=auth))
os.makedirs('screenshots', exist_ok=True)
exe = shutil.which('google-chrome') or shutil.which('chromium')

with sync_playwright() as p:
    br = p.chromium.launch(executable_path=exe, args=['--no-sandbox'])
    pg = br.new_page(viewport={'width': 1200, 'height': 1400})
    pg.goto(url)
    pg.on('pageerror', lambda e: print('PAGEERROR', e))
    assert pg.locator('.banner').inner_text() == 'American Express Charges'
    assert pg.locator('h1').inner_text() == 'Chris Cox'
    body = pg.locator('body').inner_text().lower()
    assert 'dry run' not in body and 'preview' not in body
    assert pg.locator('button.fb').count() == 72
    credit = pg.locator('tr.cr')
    assert credit.count() == 1
    assert credit.locator('button').count() == 0
    assert '−$24.31' in credit.inner_text() or '-$24.31' in credit.inner_text()
    assert 'Credit' in credit.inner_text()
    assert '$17,048.40' in pg.locator('tr.ft').nth(0).inner_text()
    assert '$17,024.09' in pg.locator('tr.net').inner_text()
    sel = pg.locator('select.catsel').first
    groups = sel.locator('optgroup').evaluate_all('els => els.map(e => e.label)')
    assert groups == ['Office', 'Travel', 'Shop Supplies'], groups
    options = sel.locator('option').evaluate_all('els => els.map(e => e.value)')
    for name in ('Meals and Entertainment', 'Gift', 'Utilities', 'Freight & Delivery', 'Office Expenses',
                 'Travel', 'Travel Fuel', 'Job Materials', 'Supplies', 'Auto Maintenance', 'Repair & Maintenance'):
        assert name in options, options
    assert sel.input_value() == 'Restaurant'
    assert sel.locator('option[selected]').inner_text() == 'Restaurant (current)'
    assert 'Other' not in options and 'Equipment Rental' not in options

    pg.emulate_media(media='print')
    assert pg.locator('button.sub').is_hidden()
    assert pg.locator('button.prt').is_hidden()
    assert pg.locator('#pop').is_hidden()
    pg.emulate_media(media='screen')
    assert pg.locator('button.sub').is_visible()

    loads = []
    pg.on('load', lambda: loads.append(1))
    pg.set_viewport_size({'width': 390, 'height': 844})
    pg.locator('button.fb').nth(0).click()
    pg.locator('#pop').wait_for(state='visible')
    assert pg.locator('#poptitle').inner_text() == 'Note/Reason'
    assert pg.locator('#popsave').is_visible()
    pg.screenshot(path='screenshots/browser-note-popup-mobile.png')
    pg.keyboard.press('Escape')
    pg.locator('#pop').wait_for(state='hidden')
    pg.set_viewport_size({'width': 1200, 'height': 1400})

    pg.locator('button.fb').nth(0).click()
    pg.locator('#pop').wait_for(state='visible')
    pg.locator('#pop label').filter(has_text='Not my charge').click()
    pg.fill('#popnote', 'a <b>tag</b>')
    pg.locator('#popsave').click()
    pg.locator('#pop').wait_for(state='hidden')
    pg.wait_for_selector('tr.f')
    assert pg.locator('tr.nx').count() == 1
    assert pg.locator('#flaglist b').count() == 0
    assert 'Note: Not my charge: a <b>tag</b>' in pg.inner_text('#flaglist')
    assert 'ref pending' in pg.inner_text('#flaglist')
    pg.locator('button.fb').nth(2).click()
    pg.locator('#pop').wait_for(state='visible')
    pg.locator('#pop label').filter(has_text='Wrong price').click()
    pg.fill('#popnote', 'Second look')
    pg.locator('#popsave').click()
    pg.locator('#pop').wait_for(state='hidden')
    pg.wait_for_function("document.querySelectorAll('tr.f').length==2")
    print('after 2 saves: flagged rows =', pg.locator('tr.f').count(), '| page reloads =', len(loads))
    assert len(loads) == 0
    pg.locator('button.fb.on').first.click()
    pg.wait_for_function("document.querySelectorAll('tr.f').length==1")
    assert pg.locator('#pop').is_hidden()
    pg.locator('button.fb').nth(0).click()
    pg.locator('#pop').wait_for(state='visible')
    pg.locator('#pop label').filter(has_text='Not my charge').click()
    pg.fill('#popnote', 'a <b>tag</b>')
    pg.locator('#popsave').click()
    pg.wait_for_function("document.querySelectorAll('tr.f').length==2")
    pg.screenshot(path='screenshots/browser-after-flag-clicks.png')

    assert pg.locator('label[for="fcb"]').inner_text() == 'Flagged charges'
    pg.fill('#fcb', 'wrong category')
    first_cat = pg.locator('td.cat').first
    original = first_cat.get_attribute('data-orig')
    with pg.expect_response(lambda r: '/category/' in r.url and r.status == 200):
        first_cat.locator('select.catsel').select_option('Gift')
    edited = pg.locator('td.cat').first
    assert edited.locator('select.catsel').input_value() == 'Gift'
    assert edited.locator('s.orig').inner_text() == original
    pg.screenshot(path='screenshots/browser-category-edit.png')
    pg.locator('button.sub').click()
    pg.wait_for_selector('.done')
    done = pg.inner_text('.done')
    print('after Submit click:', done)
    assert 'Note: Not my charge: a <b>tag</b>' in done and 'Note: Wrong price: Second look' in done
    assert 'ref pending' in done
    assert 'Flagged charges: wrong category' in done
    assert pg.locator('#fcb').count() == 0
    assert 'Gift' in pg.locator('table tr').nth(1).inner_text()
    pg.screenshot(path='screenshots/browser-after-submit.png')

    pg.goto(links['alice-cox']['url'])
    assert pg.locator('.banner').inner_text() == 'American Express Charges'
    assert pg.locator('h1').inner_text() == 'Alice E Cox'
    assert pg.locator('button.fb').count() == 9
    assert '$480.62' in pg.locator('body').inner_text()
    alice_text = pg.locator('body').inner_text().lower()
    assert 'dry run' not in alice_text and 'preview' not in alice_text
    pg.screenshot(path='screenshots/browser-alice.png')
    br.close()
    print('browser test ok')
