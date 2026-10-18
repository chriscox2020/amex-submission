"""Real-browser test: one click on Flag toggles the row without reload; one click on Submit records."""
import json, os, shutil, sys, urllib.request, base64
from playwright.sync_api import sync_playwright
B = 'http://127.0.0.1:10077'
req = urllib.request.Request(B + '/admin/links.json', headers={'Authorization': 'Basic ' + base64.b64encode(b'x:local-admin').decode()})
url = json.load(urllib.request.urlopen(req))['2026-09-22']['chris-cox']['url']
# reopen the page for the browser test
urllib.request.urlopen(urllib.request.Request(B + '/admin/reset/2026-09-22/chris-cox', method='POST', headers=req.headers))
exe = shutil.which('google-chrome') or shutil.which('chromium')
with sync_playwright() as p:
    br = p.chromium.launch(executable_path=exe, args=['--no-sandbox'])
    pg = br.new_page(viewport={'width': 1000, 'height': 1400})
    pg.goto(url)
    loads = []; pg.on('load', lambda: loads.append(1))
    btns = pg.locator('button.fl')
    btns.nth(2).click(); pg.wait_for_selector('tr.flagged')
    btns.nth(9).click(); pg.wait_for_function("document.querySelectorAll('tr.flagged').length==2")
    print('after 2 single clicks: flagged rows =', pg.locator('tr.flagged').count(), '| counter =', pg.inner_text('#nf'), '| page reloads =', len(loads))
    pg.screenshot(path='screenshots/browser-after-flag-clicks.png')
    pg.locator('button.sub').click(); pg.wait_for_selector('.done')
    print('after Submit click:', pg.inner_text('.done'))
    pg.screenshot(path='screenshots/browser-after-submit.png')
    br.close()
