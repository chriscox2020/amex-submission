"""Real-browser test: Flag opens a note box; Save flags without reload; Submit records the note."""
import json, os, shutil, urllib.request, base64
from playwright.sync_api import sync_playwright
B = 'http://127.0.0.1:10077'
req = urllib.request.Request(B + '/admin/links.json', headers={'Authorization': 'Basic ' + base64.b64encode(b'x:local-admin').decode()})
url = json.load(urllib.request.urlopen(req))['2026-09-22']['chris-cox']['url']
# reopen the page for the browser test
urllib.request.urlopen(urllib.request.Request(B + '/admin/reset/2026-09-22/chris-cox', method='POST', headers=req.headers))
os.makedirs('screenshots', exist_ok=True)
exe = shutil.which('google-chrome') or shutil.which('chromium')

def open_note(pg, n):
    pg.locator('button.fl').nth(n).click()
    pg.locator('#notebox').wait_for(state='visible')

def close_note(pg):
    # The closed box stays in the DOM with the hidden attribute, so wait on state.
    pg.locator('#notebox').wait_for(state='hidden')

def save_note(pg, note):
    pg.fill('#note', note)
    pg.locator('#notesave').click()
    close_note(pg)

with sync_playwright() as p:
    br = p.chromium.launch(executable_path=exe, args=['--no-sandbox'])
    pg = br.new_page(viewport={'width': 1000, 'height': 1400})
    pg.goto(url)
    pg.on('pageerror', lambda e: print('PAGEERROR', e))
    loads = []; pg.on('load', lambda: loads.append(1))
    pg.set_viewport_size({'width': 390, 'height': 844})
    open_note(pg, 1)
    assert pg.locator('#notesave').is_visible() and pg.locator('#notecancel').is_visible()
    pg.screenshot(path='screenshots/browser-note-popup-mobile.png')
    pg.locator('#notecancel').click()
    close_note(pg)
    pg.set_viewport_size({'width': 1000, 'height': 1400})
    open_note(pg, 0)
    pg.keyboard.press('Escape')
    close_note(pg)
    open_note(pg, 2)
    pg.fill('#note', 'should not save')
    pg.locator('#notecancel').click()
    close_note(pg)
    assert pg.locator('tr.flagged').count() == 0 and pg.locator('#flagbox').is_hidden()
    open_note(pg, 2)
    assert pg.locator('#note').get_attribute('maxlength') == '500'
    save_note(pg, 'a <b>tag</b>')
    pg.wait_for_selector('tr.flagged')
    assert pg.locator('#flaglist b').count() == 0
    assert 'Note: a <b>tag</b>' in pg.inner_text('#flaglist')
    assert 'ref pending' in pg.inner_text('#flaglist')
    open_note(pg, 9)
    save_note(pg, 'Second look')
    pg.wait_for_function("document.querySelectorAll('tr.flagged').length==2")
    print('after 2 saves: flagged rows =', pg.locator('tr.flagged').count(), '| counter =', pg.inner_text('#nf'), '| page reloads =', len(loads))
    assert len(loads) == 0
    # Unflag is still one click and does not open the note box.
    pg.locator('button.fl.on').first.click()
    pg.wait_for_function("document.querySelectorAll('tr.flagged').length==1")
    assert pg.locator('#notebox').is_hidden()
    open_note(pg, 2)
    save_note(pg, 'a <b>tag</b>')
    pg.wait_for_function("document.querySelectorAll('tr.flagged').length==2")
    pg.screenshot(path='screenshots/browser-after-flag-clicks.png')
    pg.locator('button.sub').click(); pg.wait_for_selector('.done')
    done = pg.inner_text('.done')
    print('after Submit click:', done)
    assert 'Note: a <b>tag</b>' in done and 'Note: Second look' in done and 'ref pending' in done
    pg.screenshot(path='screenshots/browser-after-submit.png')
    br.close()
