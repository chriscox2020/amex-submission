#!/usr/bin/env python3
"""Sample-data test for optional flag notes.

Uses Flask's test client (no extra dependencies). Run from the repo root:

  python tests/test_flag_note.py
"""
import base64, csv, email, hashlib, hmac, html, io, json, os, pathlib, shutil, tempfile, unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
TMP = tempfile.mkdtemp(prefix='amex-note-')
os.environ['DATA_DIR'] = TMP
os.environ['TOKEN_SECRET'] = 'local-test-secret'
os.environ['ADMIN_PASSWORD'] = 'local-admin'
os.environ['EMAIL_MODE'] = 'dryrun'
os.environ['RECORD_TO'] = 'littledude@coxresearch.com'
os.environ['RECORD_CC'] = 'chris@coxresearch.com'
os.environ['PUBLIC_BASE'] = 'http://127.0.0.1:10077'
os.environ['MAIL_FROM'] = 'Chris AI <littledude@coxresearch.com>'

import sys
sys.path.insert(0, str(ROOT))
import app
import summary

STMT = '2026-10-22'
EMP = 'sample-employee'
FROZEN = '2026-10-21T09:30:00-05:00'
LINE_NOTE = '09/15/2026 | AMAZON MARKETPLACE | $1,234.50 | 10/21/2026 | ref pending | Note: not my charge'
LINE_HTML = '09/16/2026 | DELTA AIR LINES | $318.40 | 10/21/2026 | AXP-998877 | Note: fee <b>wrong</b> & high'
LINE_BLANK = '09/18/2026 | STAPLES | $18.25 | 10/21/2026 | ref pending'
SAMPLE_PATH = pathlib.Path('/tmp/sample-flag-email.txt')

DOC = {
    'stmt': STMT,
    'label': 'Oct 22, 2026 statement',
    'due': 'Due by the first',
    'employees': [{
        'id': EMP,
        'name': 'Sample Employee',
        'first': 'Sample',
        'email': 'sample@coxresearch.com',
        'rows': [
            {'id': 1, 'date': '09/15/2026', 'category': 'Office supplies', 'company': 'AMAZON MARKETPLACE', 'amount': 1234.50},
            {'id': 2, 'date': '09/16/2026', 'category': 'Travel', 'company': 'DELTA AIR LINES', 'amount': 318.40,
             'reference_number': 'AXP-998877'},
            {'id': 3, 'date': '09/18/2026', 'category': 'Supplies', 'company': 'STAPLES', 'amount': 18.25},
            {'id': 4, 'date': '09/19/2026', 'category': 'Meals', 'company': "JOE'S DINER", 'amount': 27.50},
        ],
    }],
}


def independent_token(stmt, emp_id):
    mac = hmac.new(b'local-test-secret', f'{stmt}|{emp_id}'.encode(), hashlib.sha256).digest()[:18]
    return base64.urlsafe_b64encode(mac).decode().rstrip('=')


class FlagNoteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        d = pathlib.Path(app.DATA_DIR, 'statements')
        d.mkdir(parents=True, exist_ok=True)
        with open(d / (STMT + '.json'), 'w') as f:
            json.dump(DOC, f)
        cls.token = app.make_token(STMT, EMP)
        cls.client = app.app.test_client()

    def setUp(self):
        self._now = app.now
        app.now = lambda: FROZEN
        if os.path.exists(app.LOG):
            os.remove(app.LOG)
        for f in pathlib.Path(app.OUTBOX).glob('*'):
            f.unlink()

    def tearDown(self):
        app.now = self._now

    def flag(self, row, note=None, accept=True):
        headers = {'Accept': 'application/json'} if accept else {}
        data = {} if note is None else {'note': note}
        return self.client.post(f'/v/{self.token}/flag/{row}', data=data, headers=headers)

    def page(self):
        return self.client.get(f'/v/{self.token}').get_data(as_text=True)

    def flags(self):
        return [r for r in app.read_log() if r.get('type') == 'flag']

    def test_token_is_unchanged(self):
        self.assertEqual(self.token, independent_token(STMT, EMP))
        self.assertEqual(len(self.token), 24)
        self.assertEqual(self.client.get('/v/AAAAAAAAAAAAAAAAAAAAAAAA').status_code, 404)
        self.assertEqual(self.client.get('/admin').status_code, 401)

    def test_popup_is_hidden_until_flag(self):
        page = self.page()
        self.assertIn("id='pop' hidden", page)
        self.assertIn(">Note/Reason</b>", page)
        self.assertIn(">Save</button>", page)
        self.assertIn("placeholder='Type your own note'", page)
        self.assertIn("maxlength='500'", page)
        for reason in app.FLAG_REASONS:
            self.assertIn(f"value='{html.escape(reason)}'", page)
        self.assertIn("class='banner'>American Express Charges", page)
        self.assertIn(">Sample Employee</h1>", page)
        self.assertIn(">Flagged charges</label>", page)
        self.assertIn("id='fcb'", page)
        self.assertIn(">Print</button>", page)
        self.assertIn("button,.pop{display:none!important}", page)
        self.assertIn(f"/v/{self.token}/flag/1", page)
        self.assertNotIn('Note:', page)
        self.assertNotIn('Submit to QuickBooks for Pay', page)
        self.assertNotIn('/qb-submit', page)
        self.assertNotIn("id='qb-submit'", page)
        low = page.lower()
        self.assertNotIn('dry run', low)
        self.assertNotIn('preview', low)
        self.assertNotIn('quickbooks', low)

    def test_helpers(self):
        self.assertEqual(app.charge_ref({}), 'ref pending')
        self.assertEqual(app.charge_ref({'reference': '   '}), 'ref pending')
        self.assertEqual(app.charge_ref({'reference': '', 'ref': ' R2 '}), 'R2')
        self.assertEqual(app.charge_ref({'reference_number': 'N-9', 'ref': 'other'}), 'N-9')
        self.assertEqual(app.clean_note('  hello\r\nworld\tthere \x00'), 'hello world there')
        self.assertEqual(app.clean_note('x' * 600), 'x' * 500)
        self.assertEqual(app.clean_note(None), '')
        self.assertEqual(app.flag_day(FROZEN), '10/21/2026')
        self.assertEqual(app.flag_comment_line('  wrong category  '), 'Flagged charges: wrong category')
        self.assertEqual(app.flag_comment_line('   '), '')
        self.assertEqual(app.canonical_category(' travel '), 'Travel')
        self.assertEqual(app.canonical_category('Meals'), '')
        self.assertEqual(app.AMEX_CATEGORY_GROUPS, (
            ('Office', ('Meals and Entertainment', 'Gift', 'Utilities', 'Freight & Delivery', 'Office Expenses')),
            ('Travel', ('Travel', 'Travel Fuel')),
            ('Shop Supplies', ('Job Materials', 'Supplies', 'Repair & Maintenance')),
            ('Auto Maintenance', ('Auto Maintenance',)),
        ))
        self.assertEqual(tuple(app.AMEX_CATEGORIES), (
            'Meals and Entertainment', 'Gift', 'Utilities', 'Freight & Delivery', 'Office Expenses',
            'Travel', 'Travel Fuel', 'Job Materials', 'Supplies', 'Repair & Maintenance', 'Auto Maintenance'))
        self.assertEqual(tuple(summary.AMEX_CATEGORIES), tuple(app.AMEX_CATEGORIES))
        self.assertNotIn('Other', app.AMEX_CATEGORIES)
        self.assertNotIn('Other Expense', app.AMEX_CATEGORIES)

    def test_sample_page_and_record_email(self):
        amazon = self.flag(1, 'not my charge')
        self.assertEqual(amazon.status_code, 200)
        self.assertEqual(amazon.get_json()['line'], LINE_NOTE)
        self.assertEqual(amazon.get_json()['flagged'], True)
        self.assertEqual(amazon.get_json()['count'], 1)

        delta = self.flag(2, 'fee <b>wrong</b> & high')
        self.assertEqual(delta.get_json()['line'], LINE_HTML)

        # No-JS form post: blank note is stored and the line has no Note suffix.
        blank = self.flag(3, '   \n  ', accept=False)
        self.assertEqual(blank.status_code, 303)
        saved = self.client.post(f'/v/{self.token}/comment', data={'comment': 'wrong category'},
                                 headers={'Accept': 'application/json'})
        self.assertEqual(saved.get_json()['comment'], 'wrong category')
        cat = self.client.post(f'/v/{self.token}/category/4', data={'category': 'meals and entertainment'},
                               headers={'Accept': 'application/json'})
        self.assertEqual(cat.get_json()['category'], 'Meals and Entertainment')
        rejected = self.client.post(f'/v/{self.token}/category/4', data={'category': 'Meals'},
                                    headers={'Accept': 'application/json'})
        self.assertEqual(rejected.status_code, 400)

        page = self.page()
        for line in (LINE_NOTE, LINE_HTML, LINE_BLANK):
            self.assertIn(html.escape(line), page)
        self.assertNotIn('<b>wrong</b>', page)
        self.assertEqual(self.flags()[0]['note'], 'not my charge')
        self.assertEqual(self.flags()[1]['note'], 'fee <b>wrong</b> & high')
        self.assertEqual(self.flags()[2]['note'], '')
        self.assertIn('>wrong category</textarea>', page)
        self.assertIn('>Flagged charges</label>', page)
        self.assertIn("data-orig='Travel'", page)
        self.assertIn("<option value='Meals and Entertainment' selected>Meals and Entertainment</option>", page)
        self.assertIn("<s class='orig'>Meals</s>", page)
        self.assertIn("<optgroup label='Office'>", page)
        self.assertIn("<optgroup label='Travel'>", page)
        self.assertIn("<optgroup label='Shop Supplies'>", page)
        self.assertIn("<optgroup label='Auto Maintenance'>", page)
        self.assertNotIn("<option value='Office'", page)
        self.assertNotIn("<option value='Shop Supplies'", page)
        for name in app.AMEX_CATEGORIES:
            self.assertIn(f"<option value='{html.escape(name)}'", page)

        submitted = self.client.post(f'/v/{self.token}/submit', data={'comment': 'wrong category'})
        self.assertEqual(submitted.status_code, 303)
        again = self.client.post(f'/v/{self.token}/submit')
        self.assertEqual(again.status_code, 303)
        blocked = self.flag(4, 'too late')
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(len(list(pathlib.Path(app.OUTBOX).glob('*.eml'))), 1)

        summary = self.page()
        self.assertIn('Submitted', summary)
        self.assertIn('Flagged charges: wrong category', summary)
        for line in (LINE_NOTE, LINE_HTML, LINE_BLANK):
            self.assertIn(html.escape(line), summary)
        self.assertNotIn('<b>wrong</b>', summary)
        self.assertLess(summary.index(html.escape(LINE_NOTE)), summary.index(html.escape(LINE_HTML)))
        self.assertLess(summary.index(html.escape(LINE_HTML)), summary.index(html.escape(LINE_BLANK)))

        msg = email.message_from_bytes(next(pathlib.Path(app.OUTBOX).glob('*.eml')).read_bytes())
        text = html_body = ''
        for part in msg.walk():
            if part.get_content_type() == 'text/plain':
                text = part.get_payload(decode=True).decode()
            elif part.get_content_type() == 'text/html':
                html_body = part.get_payload(decode=True).decode()
        self.assertEqual(''.join(str(msg['Subject']).splitlines()),
                         'Amex verification SUBMITTED: Sample Employee - American Express charges October 2026 - 3 flagged')
        self.assertEqual(msg['To'], 'littledude@mail.grokbot.com')
        self.assertEqual(app.RECORD_TO, 'littledude@mail.grokbot.com')
        self.assertEqual(msg['Cc'], 'chris@coxresearch.com')
        self.assertIn('\n  ' + LINE_NOTE + '\n', text)
        self.assertIn('\n  ' + LINE_HTML + '\n', text)
        self.assertIn('\n  ' + LINE_BLANK + '\n', text)
        self.assertLess(text.index(LINE_NOTE), text.index(LINE_HTML))
        self.assertLess(text.index(LINE_HTML), text.index(LINE_BLANK))
        self.assertIn('\nFlagged charges: wrong category\n', text)
        self.assertLess(text.index(LINE_BLANK), text.index('Flagged charges: wrong category'))
        self.assertLess(text.index('Flagged charges: wrong category'), text.index('Approved'))
        self.assertNotIn('&amp;', text)
        self.assertNotIn('&lt;', text)
        for line in (LINE_NOTE, LINE_HTML, LINE_BLANK):
            self.assertIn(html.escape(line), html_body)
        self.assertNotIn('<b>wrong</b>', html_body)
        self.assertIn('Flagged charges: wrong category', html_body)
        self.assertLess(html_body.index(html.escape(LINE_BLANK)), html_body.index('Flagged charges: wrong category'))
        self.assertLess(html_body.index('Flagged charges: wrong category'), html_body.index('Approved'))
        flagged_html, approved_html = html_body.split('Approved', 1)
        self.assertIn(html.escape(LINE_NOTE), flagged_html)
        self.assertNotIn('AMAZON MARKETPLACE', approved_html)
        self.assertIn('JOE&#x27;S DINER', approved_html)
        self.assertIn('Meals and Entertainment', approved_html)
        self.assertNotIn(LINE_NOTE, approved_html)

        sample = (
            'PLAIN TEXT\n' + text + '\nHTML FLAGGED LINES\n'
            + html.escape(LINE_NOTE) + '\n'
            + html.escape(LINE_HTML) + '\n'
            + html.escape(LINE_BLANK) + '\n'
        )
        SAMPLE_PATH.write_text(sample)
        print('\n----- SAMPLE RECORD EMAIL (text) -----\n' + text + '----- END -----')

    def test_limits_legacy_and_csv(self):
        long = self.flag(1, 'x' * 600)
        self.assertEqual(len(long.get_json()['line'].split('Note: ', 1)[1]), 500)
        self.assertEqual(self.flags()[-1]['note'], 'x' * 500)

        folded = self.flag(2, 'hello\r\nworld\tthere')
        self.assertEqual(folded.get_json()['line'].split('Note: ', 1)[1], 'hello world there')

        # A query-string note is ignored so it is not stored from a logged URL.
        ignored = self.client.post(f'/v/{self.token}/flag/3?note=from-query', headers={'Accept': 'application/json'})
        self.assertEqual(ignored.get_json()['flagged'], True)
        self.assertNotIn('Note:', ignored.get_json()['line'])
        self.assertEqual(self.flags()[-1]['note'], '')

        # Old log lines have no note. They still list, with ref pending.
        os.remove(app.LOG)
        app.append({'type': 'flag', 'stmt': STMT, 'emp_id': EMP, 'row': 3, 'date': '09/18/2026',
                    'category': 'Supplies', 'company': 'STAPLES', 'amount': 18.25,
                    'ts': '2026-10-01T15:00:00-05:00'})
        legacy = '09/18/2026 | STAPLES | $18.25 | 10/01/2026 | ref pending'
        page = self.page()
        self.assertIn(html.escape(legacy), page)
        self.assertNotIn('Note:', page)

        # Re-flag through the endpoint so the CSV sees a formula-like note.
        os.remove(app.LOG)
        nasty = "=HYPERLINK(\"http://example.test\")"
        self.flag(1, nasty)
        raw = self.flags()[-1]['note']
        self.assertEqual(raw, nasty)
        csv_body = self.client.get('/admin/log.csv', auth=('Chris', 'local-admin')).get_data(as_text=True)
        parsed = list(csv.DictReader(io.StringIO(csv_body)))
        self.assertEqual(parsed[0]['note'], "'" + nasty)
        self.assertTrue(parsed[0]['note'].startswith("'"))

    def test_unflag_drops_the_note_and_bad_token_stores_nothing(self):
        self.flag(1, 'not my charge')
        off = self.flag(1, 'ignored on unflag')
        self.assertEqual(off.get_json()['flagged'], False)
        self.assertNotIn('Note:', self.page())
        self.assertEqual(self.flags()[0]['note'], 'not my charge')
        again = self.flag(1, 'replaced')
        self.assertEqual(again.get_json()['line'].split('Note: ', 1)[1], 'replaced')
        before = len(app.read_log())
        missing = self.client.post('/v/AAAAAAAAAAAAAAAAAAAAAAAA/flag/1', data={'note': 'nope'},
                                   headers={'Accept': 'application/json'})
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(len(app.read_log()), before)

        via_json = self.client.post(f'/v/{self.token}/flag/2', json={'note': 'from json <b>'},
                                    headers={'Accept': 'application/json'})
        self.assertEqual(via_json.get_json()['line'].split('Note: ', 1)[1], 'from json <b>')
        self.assertIn('Note: from json &lt;b&gt;', self.page())

    def test_overall_comment_and_category_are_safe(self):
        missing = self.client.post(f'/v/{self.token}/comment?comment=from-query', headers={'Accept': 'application/json'})
        self.assertEqual(missing.status_code, 400)
        self.flag(1, 'n')
        nasty = 'a <b> & "q"'
        self.client.post(f'/v/{self.token}/comment', data={'comment': nasty}, headers={'Accept': 'application/json'})
        self.assertEqual(self.client.post(f'/v/{self.token}/category/1', data={'category': '=Other'},
                                          headers={'Accept': 'application/json'}).status_code, 400)
        self.assertEqual(self.client.post(f'/v/{self.token}/category/1', data={'category': 'Gift'},
                                          headers={'Accept': 'application/json'}).get_json()['category'], 'Gift')
        self.client.post(f'/v/{self.token}/submit', data={'comment': nasty})
        msg = email.message_from_bytes(next(pathlib.Path(app.OUTBOX).glob('*.eml')).read_bytes())
        text = html_body = ''
        for part in msg.walk():
            if part.get_content_type() == 'text/plain':
                text = part.get_payload(decode=True).decode()
            elif part.get_content_type() == 'text/html':
                html_body = part.get_payload(decode=True).decode()
        self.assertIn('Flagged charges: a <b> & "q"', text)
        self.assertNotIn('&amp;', text)
        self.assertIn('Flagged charges: a &lt;b&gt; &amp; &quot;q&quot;', html_body)
        self.assertNotIn('a <b>', html_body)
        page = self.page()
        self.assertIn('Flagged charges: a &lt;b&gt; &amp; &quot;q&quot;', page)
        self.assertEqual(self.client.post(f'/v/{self.token}/comment', data={'comment': 'later'},
                                          headers={'Accept': 'application/json'}).status_code, 409)
        self.assertEqual(self.client.post(f'/v/{self.token}/category/1', data={'category': 'Travel'},
                                          headers={'Accept': 'application/json'}).status_code, 409)
        before = len(app.read_log())
        self.assertEqual(self.client.post('/v/AAAAAAAAAAAAAAAAAAAAAAAA/comment', data={'comment': 'x'},
                                          headers={'Accept': 'application/json'}).status_code, 404)
        self.assertEqual(self.client.post('/v/AAAAAAAAAAAAAAAAAAAAAAAA/category/1', data={'category': 'Travel'},
                                          headers={'Accept': 'application/json'}).status_code, 404)
        self.assertEqual(len(app.read_log()), before)

    def test_off_list_category_stays_selected_and_submits(self):
        """A stored category that is no longer on the list stays selected and is kept on submit."""
        bundled = ROOT / 'statements' / 'bundled' / '2026-09-22.json'
        digest = hashlib.sha256(bundled.read_bytes()).hexdigest()
        page = self.page()
        self.assertIn("<optgroup label='Office'>", page)
        self.assertIn("<optgroup label='Travel'>", page)
        self.assertIn("<optgroup label='Shop Supplies'>", page)
        self.assertIn("<optgroup label='Auto Maintenance'>", page)
        self.assertIn("<optgroup label='Shop Supplies'><option value='Job Materials'>Job Materials</option><option value='Supplies'>Supplies</option><option value='Repair &amp; Maintenance'>Repair &amp; Maintenance</option></optgroup>", page)
        self.assertIn("<optgroup label='Auto Maintenance'><option value='Auto Maintenance'>Auto Maintenance</option></optgroup>", page)
        self.assertNotIn("<option value='Office'", page)
        self.assertNotIn("<option value='Shop Supplies'", page)
        self.assertNotIn("<option value='Other'", page)
        self.assertNotIn("<option value='Other Expense'", page)
        self.assertNotIn("<option value='Equipment Rental'", page)
        self.assertIn("<option value='Office supplies' selected>Office supplies (current)</option>", page)
        self.assertIn("<option value='Meals' selected>Meals (current)</option>", page)
        self.assertIn("<option value='Travel' selected>Travel</option>", page)
        self.assertNotIn('Travel (current)', page)
        for name in app.AMEX_CATEGORIES:
            self.assertIn(f"<option value='{html.escape(name)}'", page)

        rejected = self.client.post(f'/v/{self.token}/category/1', data={'category': 'Equipment Rental'},
                                    headers={'Accept': 'application/json'})
        self.assertEqual(rejected.status_code, 400)
        kept = self.client.post(f'/v/{self.token}/category/1', data={'category': 'Office supplies'},
                                headers={'Accept': 'application/json'})
        self.assertEqual(kept.status_code, 200)
        self.assertEqual(kept.get_json()['category'], 'Office supplies')
        still = self.page()
        self.assertIn("<option value='Office supplies' selected>Office supplies (current)</option>", still)

        submitted = self.client.post(f'/v/{self.token}/submit')
        self.assertEqual(submitted.status_code, 303)
        done = self.page()
        self.assertNotIn('<select', done)
        self.assertIn('Office supplies', done)
        self.assertIn('Meals', done)
        self.assertNotIn('(current)', done)
        msg = email.message_from_bytes(next(pathlib.Path(app.OUTBOX).glob('*.eml')).read_bytes())
        html_body = ''
        for part in msg.walk():
            if part.get_content_type() == 'text/html':
                html_body = part.get_payload(decode=True).decode()
        self.assertIn(
            "<td style='padding:4px 8px'>Office supplies</td>"
            "<td style='padding:4px 8px'>AMAZON MARKETPLACE</td>"
            "<td style='padding:4px 8px;text-align:right'>$1,234.50</td>"
            "<td style='padding:4px 8px'>Office supplies</td>",
            html_body)
        self.assertIn(
            "<td style='padding:4px 8px'>Meals</td>"
            "<td style='padding:4px 8px'>JOE&#x27;S DINER</td>"
            "<td style='padding:4px 8px;text-align:right'>$27.50</td>"
            "<td style='padding:4px 8px'>Meals</td>",
            html_body)
        self.assertEqual(hashlib.sha256(bundled.read_bytes()).hexdigest(), digest)

    def test_record_address_credits_and_alice_routing(self):
        self.assertEqual(app.normalize_record_to('littledude@coxresearch.com'), 'littledude@mail.grokbot.com')
        self.assertEqual(app.normalize_record_to(''), 'littledude@mail.grokbot.com')
        self.assertEqual(app.normalize_record_to('other@example.com'), 'other@example.com')
        src = ((ROOT / 'app.py').read_text() + '\n' + (ROOT / 'summary.py').read_text()).lower()
        for banned in ('intuit.com', 'americanexpress.com', 'api.amex', 'api.dropbox',
                       'dropbox_token', 'dropbox_app_secret', 'quickbooks_client', 'quickbooks_secret'):
            self.assertNotIn(banned, src)
        self.assertNotIn('Submit to QuickBooks for Pay', self.page())
        self.assertNotIn('/qb-submit', self.page())

        doc = {
            'stmt': '2026-11-01',
            'label': 'Nov 1, 2026 statement',
            'employees': [{
                'id': 'alice-cox',
                'name': 'Alice E Cox',
                'first': 'Alice',
                'email': 'alice@coxresearch.com',
                'rows': [
                    {'id': 1, 'date': '09/02/2026', 'category': 'Restaurant', 'company': 'CAFE', 'amount': 10.0},
                    {'id': 2, 'date': '08/15/2026', 'category': 'Merchandise & Supplies', 'company': 'AUTOZONE', 'amount': -2.5},
                ],
            }],
        }
        path = pathlib.Path(app.DATA_DIR, 'statements', '2026-11-01.json')
        path.write_text(json.dumps(doc))
        loaded = app.load_statements()['2026-11-01']['employees'][0]
        self.assertEqual(loaded['email'], 'chris@coxresearch.com')
        self.assertEqual(loaded['send_to'], 'chris@coxresearch.com')
        token = app.make_token('2026-11-01', 'alice-cox')
        page = self.client.get(f'/v/{token}').get_data(as_text=True)
        self.assertIn("class='banner'>American Express Charges", page)
        self.assertIn('>Alice E Cox</h1>', page)
        self.assertLess(page.index('AUTOZONE'), page.index('CAFE'))
        self.assertIn('Total charges', page)
        self.assertIn('>Credits</td>', page)
        self.assertIn('Net Total', page)
        self.assertIn('$10.00', page)
        self.assertIn('&minus;$2.50', page)
        self.assertIn('$7.50', page)
        self.assertIn('>Credit</span>', page)
        self.assertEqual(page.count("class='fb'"), 1)
        self.assertIn(f'/v/{token}/flag/1', page)
        self.assertNotIn(f'/v/{token}/flag/2', page)
        self.assertNotIn(f'/v/{token}/category/2', page)
        self.assertNotIn('dry run', page.lower())
        self.assertNotIn('preview', page.lower())
        self.assertEqual(self.client.post(f'/v/{token}/flag/2', data={'note': 'x'},
                                          headers={'Accept': 'application/json'}).status_code, 400)
        self.assertEqual(self.client.post(f'/v/{token}/category/2', data={'category': 'Travel'},
                                          headers={'Accept': 'application/json'}).status_code, 400)
        joined = self.client.post(f'/v/{token}/flag/1', data={'reason': 'Wrong price', 'note': 'too high'},
                                  headers={'Accept': 'application/json'})
        self.assertEqual(joined.get_json()['note'], 'Wrong price: too high')
        self.assertTrue(joined.get_json()['line'].endswith('Note: Wrong price: too high'))

        bundled = app.load_statements()['2026-09-22']
        by_id = {e['id']: e for e in bundled['employees']}
        chris, alice = by_id['chris-cox'], by_id['alice-cox']
        self.assertEqual(alice['email'], 'chris@coxresearch.com')
        self.assertEqual(alice['send_to'], 'chris@coxresearch.com')
        positive = [r for r in chris['rows'] if r['amount'] > 0]
        negative = [r for r in chris['rows'] if r['amount'] < 0]
        self.assertEqual(len(positive), 72)
        self.assertEqual(round(sum(r['amount'] for r in positive), 2), 17048.40)
        self.assertEqual(len(negative), 1)
        self.assertEqual(round(negative[0]['amount'], 2), -24.31)
        self.assertIn('AUTOZONE', negative[0]['company'].upper())
        self.assertEqual(round(sum(r['amount'] for r in chris['rows']), 2), 17024.09)
        self.assertEqual(len(alice['rows']), 9)
        self.assertEqual(round(sum(r['amount'] for r in alice['rows']), 2), 480.62)
        dates = [r['date'] for r in chris['rows']]
        from datetime import datetime
        self.assertEqual(dates, sorted(dates, key=lambda d: datetime.strptime(d, '%m/%d/%Y')))
        chris_page = self.client.get(f"/v/{app.make_token('2026-09-22', 'chris-cox')}").get_data(as_text=True)
        self.assertIn("class='banner'>American Express Charges", chris_page)
        self.assertIn('>Chris Cox</h1>', chris_page)
        self.assertEqual(chris_page.count("class='fb'"), 72)
        self.assertIn('$17,048.40', chris_page)
        self.assertIn('&minus;$24.31', chris_page)
        self.assertIn('$17,024.09', chris_page)
        self.assertNotIn(f"/v/{app.make_token('2026-09-22', 'chris-cox')}/flag/54", chris_page)


if __name__ == '__main__':
    try:
        unittest.main(verbosity=2)
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
