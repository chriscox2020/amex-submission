#!/usr/bin/env python3
"""Monthly summary for Chris and Meghan. Run from the repo root:

  .venv/bin/python tests/test_summary.py
"""
import email, html, io, json, os, pathlib, re, tempfile, unittest, zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
TMP = tempfile.mkdtemp(prefix='amex-summary-')
os.environ['DATA_DIR'] = TMP
os.environ['TOKEN_SECRET'] = 'local-test-secret'
os.environ['ADMIN_PASSWORD'] = 'local-admin'
os.environ['EMAIL_MODE'] = 'dryrun'
os.environ['RECORD_TO'] = 'other@example.com'
os.environ['RECORD_CC'] = ''
os.environ['PUBLIC_BASE'] = 'http://127.0.0.1:10077'
os.environ['MAIL_FROM'] = 'Chris AI <littledude@coxresearch.com>'

import sys
sys.path.insert(0, str(ROOT))
import app
import summary

AUG = '2026-08-22'
JUL = '2026-07-22'
FROZEN = '2026-10-21T09:30:00-05:00'
AUG_DOC = {
    'stmt': AUG,
    'label': 'Aug 22, 2026 statement',
    'close_date': '2026-08-22',
    'employees': [
        {
            'id': 'ada',
            'name': 'Ada',
            'first': 'Ada',
            'email': 'ada@coxresearch.com',
            'rows': [
                {'id': 1, 'date': '08/01/2026', 'category': 'Restaurant', 'company': 'CAFE', 'amount': 10.00},
                {'id': 2, 'date': '08/02/2026', 'category': 'Travel', 'company': 'AIR', 'amount': 5.00},
                {'id': 3, 'date': '08/03/2026', 'category': 'Merchandise & Supplies', 'company': 'STORE', 'amount': -1.00},
            ],
        },
        {
            'id': 'ben',
            'name': 'Ben',
            'first': 'Ben',
            'email': 'ben@coxresearch.com',
            'rows': [
                {'id': 1, 'date': '08/04/2026', 'category': 'Merchandise & Supplies', 'company': 'SHOP', 'amount': 3.50},
            ],
        },
    ],
}
JUL_DOC = {
    'stmt': JUL,
    'label': 'Jul 22, 2026 statement',
    'close_date': '2026-07-22',
    'employees': [{
        'id': 'cara',
        'name': 'Cara',
        'first': 'Cara',
        'email': 'cara@coxresearch.com',
        'rows': [
            {'id': 1, 'date': '07/04/2026', 'category': 'Restaurant', 'company': 'DINER', 'amount': 1.00},
        ],
    }],
}


def write_stmt(doc):
    d = pathlib.Path(app.DATA_DIR, 'statements')
    d.mkdir(parents=True, exist_ok=True)
    with open(d / (doc['stmt'] + '.json'), 'w') as fh:
        json.dump(doc, fh)


class SummaryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        write_stmt(AUG_DOC)
        write_stmt(JUL_DOC)
        cls.client = app.app.test_client()
        cls.ada = app.make_token(AUG, 'ada')
        cls.ben = app.make_token(AUG, 'ben')
        cls.cara = app.make_token(JUL, 'cara')

    def setUp(self):
        self._now = app.now
        app.now = lambda: FROZEN
        self._send = app.send_prepared
        self._summary_to = app.SUMMARY_TO
        if os.path.exists(app.LOG):
            os.remove(app.LOG)
        out = pathlib.Path(app.OUTBOX)
        out.mkdir(parents=True, exist_ok=True)
        for f in out.glob('*'):
            f.unlink()
        for folder_name in ('summaries', 'summary-grids'):
            folder = pathlib.Path(app.DATA_DIR, folder_name)
            if folder.exists():
                for p in sorted(folder.rglob('*'), reverse=True):
                    if p.is_file():
                        p.unlink()
                    else:
                        p.rmdir()
                folder.rmdir()

    def tearDown(self):
        app.now = self._now
        app.send_prepared = self._send
        app.SUMMARY_TO = self._summary_to

    def auth_get(self, path, user='Chris', password='local-admin'):
        return self.client.get(path, auth=(user, password))

    def _summary_post(self, path):
        tail = path.rstrip('/').rsplit('/', 1)[-1]
        return '/admin/summary/' in path and tail in ('categories', 'qb-submit')

    def csrf_token(self, user='Chris', stmt=AUG):
        page = self.auth_get(f'/admin/summary/{stmt}', user=user).get_data(as_text=True)
        match = re.search(r"name='csrf' value='([^']+)'", page)
        if match:
            self._csrf = match.group(1)
            return self._csrf
        cached = getattr(self, '_csrf', None)
        if cached:
            return cached
        self.fail('summary page did not include a csrf token')

    def auth_post(self, path, user='Chris', password='local-admin', data=None, headers=None):
        payload = None if data is None else dict(data)
        if self._summary_post(path) and user.casefold() in ('chris', 'meghan'):
            if payload is None:
                payload = {}
            payload.setdefault('csrf', self.csrf_token(user=user))
        return self.client.post(path, data=payload, auth=(user, password), headers=headers)

    def grid_data(self, amounts):
        cats, amts = [], []
        for name, dollars in amounts:
            cats.append(name)
            amts.append(dollars if isinstance(dollars, str) else f'{dollars:.2f}')
        return {'cat': cats, 'amt': amts}

    def post_grid(self, amounts, stmt=AUG, user='Chris', path_suffix='categories'):
        data = self.grid_data(amounts)
        data['save'] = '1'
        data['csrf'] = self.csrf_token(user=user, stmt=stmt)
        return self.client.post(
            f'/admin/summary/{stmt}/{path_suffix}',
            data=data,
            auth=(user, 'local-admin'),
        )

    def page(self, stmt=AUG, user='Chris'):
        return self.auth_get(f'/admin/summary/{stmt}', user=user).get_data(as_text=True)

    def submit_employee(self, token, category=None, row=None):
        if category is not None:
            res = self.client.post(f'/v/{token}/category/{row}', data={'category': category},
                                   headers={'Accept': 'application/json'})
            self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        res = self.client.post(f'/v/{token}/submit')
        self.assertEqual(res.status_code, 303)
        return res

    def qb_messages(self):
        found = []
        for path in pathlib.Path(app.OUTBOX).glob('*.eml'):
            msg = email.message_from_bytes(path.read_bytes())
            if str(msg['Subject'] or '').startswith('QB SUBMIT:'):
                found.append(msg)
        return found

    def test_final_category_edit_wins(self):
        self.assertEqual(summary.final_category({'id': 1, 'category': 'Meals'}, {1: 'Entertainment'}), 'Meals and Entertainment')
        self.assertEqual(summary.final_category({'id': 1, 'category': 'Meals'}, {}), 'Meals and Entertainment')
        self.assertEqual(summary.final_category({'id': 2, 'category': ' travel '}, {}), 'Travel')
        self.assertEqual(summary.final_category({'id': 3, 'category': 'Restaurant'}, {3: ''}), 'Other General and Admin Expenses')
        self.assertEqual(summary.final_category({'id': 4, 'category': 'Restaurant'}, {}), 'Meals and Entertainment')
        self.assertEqual(summary.final_category({'id': 5, 'category': 'Merchandise & Supplies'}, {}), 'Supplies')
        self.assertEqual(summary.final_category({'id': 6, 'category': 'Office supplies'}, {}), 'Supplies')
        self.assertEqual(summary.final_category({'id': 7, 'category': 'Transportation'}, {}), 'Other General and Admin Expenses')
        self.assertEqual(summary.final_category({'id': 8, 'category': 'Fuel'}, {}), 'Travel Fuel')
        self.assertEqual(summary.final_category({'id': 9, 'category': 'Software'}, {}), 'Office Expenses:Software')
        self.assertEqual(summary.final_category({'id': 10, 'category': 'Parts & Materials'}, {}), 'Job Materials')
        self.assertEqual(tuple(app.AMEX_CATEGORIES), summary.CATEGORIES)
        period = summary.statement_period({'stmt': '2026-10-22'})
        self.assertEqual(period['month_label'], 'October 2026')
        self.assertEqual(period['folder_label'], 'American Express charges October 2026')
        self.assertEqual(summary.summary_filename({'period': period}, 'pdf'),
                         'American Express charges October 2026 - Summary.pdf')
        self.assertEqual(summary.qb_subject({'period': period}),
                         'QB SUBMIT: American Express charges October 2026')

    def test_summary_is_separate_from_employees(self):
        self.assertEqual(self.client.get(f'/admin/summary/{AUG}').status_code, 401)
        self.assertEqual(self.auth_get(f'/admin/summary/{AUG}', user='Pat').status_code, 403)
        self.assertEqual(self.auth_get(f'/admin/summary/{AUG}', user='x').status_code, 403)
        self.assertIn('Chris or Meghan only', self.auth_get(f'/admin/summary/{AUG}', user='x').get_data(as_text=True))
        self.assertEqual(self.auth_get('/admin', user='x').status_code, 403)
        self.assertEqual(self.auth_get('/admin', user='Chris').status_code, 200)
        self.assertEqual(self.auth_get(f'/admin/summary/{AUG}', user='CHRIS').status_code, 200)
        self.assertEqual(self.auth_get(f'/admin/summary/{AUG}', user='meghan').status_code, 200)
        self.assertEqual(self.auth_post(f'/admin/summary/{AUG}/qb-submit', user='Pat').status_code, 403)

        chris = app.make_token('2026-09-22', 'chris-cox')
        employee = self.client.get(f'/v/{chris}').get_data(as_text=True)
        self.assertIn('American Express Charges', employee)
        self.assertNotIn('Submit to QuickBooks for Pay', employee)
        self.assertNotIn('/qb-submit', employee)
        self.assertNotIn("id='qb-submit'", employee)
        ada_page = self.client.get(f'/v/{self.ada}').get_data(as_text=True)
        self.assertNotIn('Submit to QuickBooks for Pay', ada_page)
        self.assertNotIn('category-grid', ada_page)
        self.assertNotIn('save-categories', ada_page)
        self.assertNotIn('qb-confirm', ada_page)
        self.assertNotIn("id='amex-total'", ada_page)
        self.assertIn(">Meals and Entertainment<", ada_page)
        self.assertIn(">Travel<", ada_page)
        self.assertIn(">Supplies<", ada_page)
        self.assertNotIn('Restaurant', ada_page)
        self.assertNotIn('Merchandise', ada_page)
        self.assertIn(json.dumps(list(summary.CATEGORIES)), ada_page)
        self.assertIn(">Submit</button>", ada_page)

        self.submit_employee(self.ada, 'Entertainment', 2)
        types = [r['type'] for r in app.read_log() if r.get('stmt') == AUG and r['type'] != 'open']
        self.assertEqual(types, ['category', 'submit', 'email'])
        self.assertFalse(pathlib.Path(app.DATA_DIR, 'summaries').exists())
        self.assertEqual(self.qb_messages(), [])
        employee_mail = list(pathlib.Path(app.OUTBOX).glob('*.eml'))
        self.assertEqual(len(employee_mail), 1)
        msg = email.message_from_bytes(employee_mail[0].read_bytes())
        self.assertEqual(msg['To'], 'other@example.com')
        self.assertEqual(''.join(str(msg['Subject']).splitlines()),
                         'Amex verification SUBMITTED: Ada - American Express charges August 2026 - 0 flagged')
        self.assertNotIn('QB SUBMIT', str(msg['Subject']))
        self.assertNotIn('dropbox', ada_page.lower())
        self.assertNotIn('/qb-submit', ada_page)

    def test_disabled_until_ready_then_chris_sends(self):
        waiting = self.page()
        self.assertIn('Signed in as Chris', waiting)
        self.assertIn('Not submitted', waiting)
        self.assertIn("id='qb-submit' disabled", waiting)
        self.assertIn('Submit to QuickBooks for Pay', waiting)
        self.assertIn("id='summary-warning'", waiting)
        self.assertIn("class='warn'", waiting)
        self.assertIn('Category total $0.00 does not match the Amex statement total $17.50. Difference: $17.50.', waiting)
        self.assertIn('Not everyone has submitted.', waiting)
        self.assertLess(waiting.index("id='cardholders'"), waiting.index("id='amex-total'"))
        self.assertLess(waiting.index("id='amex-total'"), waiting.index("data-category='Travel'"))
        self.assertIn('>Ada</td><td><b style=\'color:#c62828\'>Not submitted</b></td><td class=\'n\'>$14.00</td>', waiting)
        self.assertIn('>Ben</td><td><b style=\'color:#c62828\'>Not submitted</b></td><td class=\'n\'>$3.50</td>', waiting)
        for i, name in enumerate(summary.CATEGORIES):
            token = f"data-category='{html.escape(name)}'"
            self.assertIn(token, waiting)
            if i:
                prev = f"data-category='{html.escape(summary.CATEGORIES[i - 1])}'"
                self.assertLess(waiting.index(prev), waiting.index(token))
        self.assertEqual(summary.CATEGORIES[-1], 'Other General and Admin Expenses')
        self.assertNotIn('Restaurant', waiting)
        self.assertNotIn('Merchandise', waiting)
        self.assertNotIn('Office Supplies', waiting)
        self.assertNotIn('Communications', waiting)
        blocked = self.auth_post(f'/admin/summary/{AUG}/qb-submit')
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(blocked.get_json()['error'], 'not ready')
        self.assertEqual(blocked.get_json()['difference'], 17.5)
        self.assertEqual([r for r in app.read_log() if r['type'] == 'qb_submit'], [])

        self.submit_employee(self.ada, 'Entertainment', 2)
        partial = self.page()
        self.assertIn('Submitted Oct 21, 2026 9:30 AM CT', partial)
        self.assertIn('>Ben</td><td><b style=\'color:#c62828\'>Not submitted</b>', partial)
        self.assertIn('Meals and Entertainment', partial)
        self.assertIn('$15.00', partial)
        self.assertIn('Category total $14.00 does not match the Amex statement total $17.50. Difference: $3.50.', partial)
        self.assertIn("id='qb-submit' disabled", partial)
        built = summary.build_summary(app.load_statements()[AUG], app.state)
        totals = dict(built['categories'])
        self.assertEqual(totals['Meals and Entertainment'], 1500)
        self.assertEqual(totals['Travel'], 0)
        self.assertEqual(totals['Travel Fuel'], 0)
        self.assertEqual(totals['Supplies'], -100)
        self.assertNotIn('Entertainment', totals)
        self.assertNotIn('Fuel', totals)
        self.assertNotIn('Meals', totals)
        self.assertNotIn('Restaurant', totals)
        self.assertNotIn('Merchandise & Supplies', totals)
        self.assertEqual(list(totals), list(summary.CATEGORIES))
        self.assertEqual(built['summary_cents'], 1400)
        self.assertEqual(built['statement_cents'], 1750)
        self.assertFalse(built['ready'])
        still = self.auth_post(f'/admin/summary/{AUG}/qb-submit')
        self.assertEqual(still.status_code, 409)
        self.assertEqual(still.get_json()['difference'], 3.5)
        self.assertFalse(pathlib.Path(app.DATA_DIR, 'summaries').exists())

        self.submit_employee(self.ben)
        ready = self.page()
        self.assertNotIn("id='summary-warning'", ready)
        self.assertIn("id='qb-submit'>Submit to QuickBooks for Pay", ready)
        self.assertNotIn("id='qb-submit' disabled", ready)
        self.assertIn("id='qb-confirm' class='modal' hidden", ready)
        self.assertIn('Are you sure? This will submit the August 2026 category totals.', ready)
        self.assertIn("id='qb-cancel'>Cancel</button>", ready)
        self.assertIn("id='qb-yes'", ready)
        built = summary.build_summary(app.load_statements()[AUG], app.state)
        self.assertTrue(built['ready'])
        self.assertEqual(built['summary_cents'], 1750)
        self.assertEqual(dict(built['categories'])['Supplies'], 250)
        self.assertEqual(dict(built['categories'])['Meals and Entertainment'], 1500)
        self.assertEqual(built['period']['close_date'], '2026-08-22')
        self.assertEqual(built['period']['month_label'], 'August 2026')

        def fail(msg):
            raise OSError('smtp down')
        app.send_prepared = fail
        failed = self.auth_post(f'/admin/summary/{AUG}/qb-submit')
        self.assertEqual(failed.status_code, 502)
        self.assertEqual([r for r in app.read_log() if r['type'] == 'qb_submit'], [])
        folder = pathlib.Path(app.DATA_DIR, 'summaries', 'American Express charges August 2026')
        pdf_name = 'American Express charges August 2026 - Summary.pdf'
        xlsx_name = 'American Express charges August 2026 - Summary.xlsx'
        csv_name = 'American Express charges August 2026 - Summary.csv'
        self.assertTrue((folder / pdf_name).is_file())
        app.send_prepared = self._send

        sent = self.auth_post(f'/admin/summary/{AUG}/qb-submit')
        self.assertEqual(sent.status_code, 303)
        self.assertTrue(sent.headers['Location'].endswith(f'/admin/summary/{AUG}'))
        records = [r for r in app.read_log() if r['type'] == 'qb_submit']
        self.assertEqual(len(records), 1)
        rec = records[0]
        self.assertEqual(rec['by'], 'Chris')
        self.assertEqual(rec['stmt'], AUG)
        self.assertEqual(rec['close_date'], '2026-08-22')
        self.assertEqual(rec['to'], 'littledude@mail.grokbot.com')
        self.assertEqual(rec['summary_total'], 17.5)
        self.assertEqual(rec['statement_total'], 17.5)
        self.assertEqual(rec['categories']['Meals and Entertainment'], 15.0)
        self.assertEqual(rec['categories']['Travel'], 0.0)
        self.assertEqual(rec['categories']['Travel Fuel'], 0.0)
        self.assertTrue(str(rec['email_status']).startswith('dryrun:'))
        self.assertTrue(str(rec['files']).endswith('/summaries/American Express charges August 2026'))
        self.assertEqual(rec['folder'], 'American Express charges August 2026')

        done = self.page()
        self.assertIn("id='sent'>Sent for posting", done)
        self.assertIn('Oct 21, 2026 9:30 AM CT by Chris.', done)
        self.assertNotIn("id='qb-submit'", done)
        self.assertNotIn("id='qb-confirm'", done)
        self.assertNotIn('Submit to QuickBooks for Pay', done)

        messages = self.qb_messages()
        self.assertEqual(len(messages), 1)
        msg = messages[0]
        self.assertEqual(msg['Subject'], 'QB SUBMIT: American Express charges August 2026')
        self.assertEqual(msg['To'], 'littledude@mail.grokbot.com')
        self.assertIsNone(msg['Cc'])
        text = html_body = ''
        files = {}
        for part in msg.walk():
            name = part.get_filename()
            if name:
                files[name] = part.get_payload(decode=True)
            elif part.get_content_type() == 'text/plain':
                text = part.get_payload(decode=True).decode()
            elif part.get_content_type() == 'text/html':
                html_body = part.get_payload(decode=True).decode()
        self.assertEqual(set(files), {pdf_name, xlsx_name, 'ada-submission.json', 'ben-submission.json'})
        self.assertTrue(files[pdf_name].startswith(b'%PDF'))
        self.assertIn(b'Meals and Entertainment: $15.00', files[pdf_name])
        self.assertIn(b'Office Expenses Software: $0.00', files[pdf_name])
        self.assertNotIn(b'Office Expenses:Software:', files[pdf_name])
        self.assertIn('American Express charges August 2026', text)
        self.assertIn('did not contact QuickBooks or Dropbox', text)
        self.assertIn('did not contact QuickBooks or Dropbox', html_body)
        block = json.loads(text[text.index('{'):text.rindex('}') + 1])
        self.assertEqual(block['close_date'], '2026-08-22')
        self.assertEqual(block['month'], 'August 2026')
        self.assertEqual(block['folder'], 'American Express charges August 2026')
        self.assertEqual(block['grand_total'], 17.5)
        self.assertEqual(block['statement_total'], 17.5)
        self.assertEqual(block['categories']['Meals and Entertainment'], 15.0)
        self.assertEqual(block['categories']['Travel Fuel'], 0.0)
        self.assertEqual(block['categories']['Auto Allowance:Fuel'], 0.0)
        self.assertNotIn('Communications', block['categories'])
        self.assertNotIn('Fuel', block['categories'])
        self.assertNotIn('Entertainment', block['categories'])
        for name in summary.CATEGORIES:
            self.assertIn(name, block['categories'])
        self.assertEqual(list(block['categories']), list(summary.CATEGORIES))
        self.assertEqual([row['account'] for row in block['accounts']], list(summary.CATEGORIES))
        self.assertEqual(block['accounts'][2], {'account': 'Auto Allowance:Fuel', 'amount': 0.0})
        self.assertEqual(block['accounts'][5], {'account': 'Office Expenses:Software', 'amount': 0.0})
        self.assertNotIn('Office Supplies', block['categories'])
        self.assertNotIn('Restaurant', block['categories'])
        ada = json.loads(files['ada-submission.json'])
        air = next(c for c in ada['charges'] if c['merchant'] == 'AIR')
        self.assertEqual(air['original_category'], 'Travel')
        self.assertEqual(air['final_category'], 'Meals and Entertainment')

        for name in (pdf_name, xlsx_name, csv_name):
            self.assertTrue((folder / name).is_file())
        self.assertTrue((folder / 'submissions' / 'ada-submission.json').is_file())
        self.assertTrue((folder / 'submissions' / 'ben-submission.json').is_file())
        pdf = (folder / pdf_name).read_bytes()
        self.assertTrue(pdf.startswith(b'%PDF-1.4'))
        self.assertIn(b'%%EOF', pdf)
        self.assertIn(b'Meals and Entertainment: $15.00', pdf)
        self.assertIn(b'Auto Allowance Fuel: $0.00', pdf)
        self.assertNotIn(b'Auto Allowance:Fuel:', pdf)
        csv_text = (folder / csv_name).read_text()
        self.assertIn('Meals and Entertainment', csv_text)
        self.assertIn('Auto Allowance:Fuel', csv_text)
        self.assertIn('QuickBooks account', csv_text)
        self.assertIn('Grand total', csv_text)
        with zipfile.ZipFile(folder / xlsx_name) as zf:
            sheet = zf.read('xl/worksheets/sheet1.xml').decode()
        self.assertIn('American Express charges August 2026', sheet)
        self.assertIn('Meals and Entertainment', sheet)
        self.assertIn('Office Expenses:Software', sheet)
        self.assertIn('2.50', sheet)

        downloaded = self.auth_get(f'/admin/summary/{AUG}/summary.pdf')
        self.assertEqual(downloaded.status_code, 200)
        self.assertTrue(downloaded.data.startswith(b'%PDF'))
        self.assertIn('filename="American Express charges August 2026 - Summary.pdf"',
                      downloaded.headers['Content-Disposition'])
        csv_dl = self.auth_get(f'/admin/summary/{AUG}/summary.csv', user='Meghan')
        self.assertEqual(csv_dl.mimetype, 'text/csv')
        xlsx_dl = self.auth_get(f'/admin/summary/{AUG}/summary.xlsx')
        self.assertEqual(xlsx_dl.status_code, 200)
        self.assertEqual(self.auth_get(f'/admin/summary/{AUG}/summary.pdf', user='x').status_code, 403)

        again = self.auth_post(f'/admin/summary/{AUG}/qb-submit')
        self.assertEqual(again.status_code, 409)
        again_page = again.get_data(as_text=True)
        self.assertIn("id='already-sent'>This month was already sent.", again_page)
        self.assertIn("id='sent'>Sent for posting", again_page)
        self.assertEqual(len(self.qb_messages()), 1)
        self.assertEqual(len([r for r in app.read_log() if r['type'] == 'qb_submit']), 1)
        again_json = self.client.post(
            f'/admin/summary/{AUG}/qb-submit',
            data={'csrf': self.csrf_token()},
            headers={'Accept': 'application/json'},
            auth=('Chris', 'local-admin'),
        )
        self.assertEqual(again_json.status_code, 409)
        self.assertEqual(again_json.get_json()['error'], 'already sent')
        self.assertEqual(len(self.qb_messages()), 1)

        flagged = self.client.post(f'/v/{self.ada}/flag/1', data={'note': 'too late'},
                                   headers={'Accept': 'application/json'})
        self.assertEqual(flagged.status_code, 409)
        self.assertEqual(flagged.get_json()['error'], 'month locked')
        self.assertEqual(self.client.post(f'/v/{self.ada}/category/1', data={'category': 'Other General and Admin Expenses'},
                                          headers={'Accept': 'application/json'}).status_code, 409)
        self.assertEqual(self.client.post(f'/v/{self.ada}/comment', data={'comment': 'nope'},
                                          headers={'Accept': 'application/json'}).status_code, 409)
        reset = self.client.post(f'/admin/reset/{AUG}/ada', auth=('Chris', 'local-admin'))
        self.assertEqual(reset.status_code, 409)
        self.assertEqual(reset.get_json()['error'], 'month locked')
        self.assertEqual(self.client.get(f'/v/{self.ada}').status_code, 200)
        index = self.auth_get('/admin/summary').get_data(as_text=True)
        self.assertIn(AUG, index)
        self.assertIn('Sent for posting', index)

    def test_meghan_is_recorded(self):
        self.submit_employee(self.cara)
        sent = self.auth_post(f'/admin/summary/{JUL}/qb-submit', user='Meghan')
        self.assertEqual(sent.status_code, 303)
        rec = next(r for r in app.read_log() if r['type'] == 'qb_submit')
        self.assertEqual(rec['by'], 'Meghan')
        self.assertEqual(rec['stmt'], JUL)
        msg = self.qb_messages()[0]
        self.assertEqual(msg['Subject'], 'QB SUBMIT: American Express charges July 2026')
        self.assertEqual(msg['To'], 'littledude@mail.grokbot.com')
        page = self.page(JUL, user='meghan')
        self.assertIn('Sent for posting', page)
        self.assertIn('by Meghan.', page)
        folder = pathlib.Path(app.DATA_DIR, 'summaries', 'American Express charges July 2026')
        self.assertTrue((folder / 'American Express charges July 2026 - Summary.pdf').is_file())
        self.assertTrue((folder / 'American Express charges July 2026 - Summary.xlsx').is_file())
        self.assertTrue((folder / 'submissions' / 'cara-submission.json').is_file())

    def test_employee_token_cannot_see_another_or_admin(self):
        ada_note = 'ada-private-note'
        ben_note = 'ben-private-note'
        ada_flag = self.client.post(f'/v/{self.ada}/flag/1', data={'note': ada_note},
                                    headers={'Accept': 'application/json'})
        ben_flag = self.client.post(f'/v/{self.ben}/flag/1', data={'note': ben_note},
                                    headers={'Accept': 'application/json'})
        self.assertEqual(ada_flag.status_code, 200)
        self.assertIn('CAFE', ada_flag.get_json()['line'])
        self.assertNotIn('SHOP', ada_flag.get_json()['line'])
        self.assertIn('SHOP', ben_flag.get_json()['line'])
        self.assertNotIn('CAFE', ben_flag.get_json()['line'])
        self.assertNotIn(ada_note, ben_flag.get_json()['line'])

        ada_page = self.client.get(f'/v/{self.ada}?employee=ben&emp_id=ben&download=all').get_data(as_text=True)
        self.assertIn("data-employee='ada'", ada_page)
        self.assertIn('>Ada</h1>', ada_page)
        self.assertIn('CAFE', ada_page)
        self.assertIn(ada_note, ada_page)
        self.assertNotIn('SHOP', ada_page)
        self.assertNotIn(ben_note, ada_page)
        self.assertNotIn('>Ben</h1>', ada_page)
        self.assertNotIn('/admin', ada_page)
        self.assertNotIn('summary.pdf', ada_page)
        self.assertNotIn(self.ben, ada_page)
        self.assertIn('window.print()', ada_page)
        self.assertEqual(ada_page.count("data-id="), 3)

        ben_page = self.client.get(f'/v/{self.ben}').get_data(as_text=True)
        self.assertIn("data-employee='ben'", ben_page)
        self.assertIn('SHOP', ben_page)
        self.assertIn(ben_note, ben_page)
        self.assertNotIn('CAFE', ben_page)
        self.assertNotIn('AIR', ben_page)
        self.assertNotIn(ada_note, ben_page)
        self.assertNotIn(self.ada, ben_page)
        self.assertEqual(ben_page.count("data-id="), 1)

        self.assertNotEqual(self.ada, self.ben)
        self.assertEqual(len(self.ada), 24)
        self.assertNotEqual(self.ada, 'ada')
        swapped = self.ada[:-1] + ('A' if not self.ada.endswith('A') else 'B')
        for guess in ('ada', 'ben', 'alice-cox', 'chris-cox', swapped, ''):
            body = self.client.get('/v/' + guess).get_data(as_text=True)
            self.assertNotEqual(self.client.get('/v/' + guess).status_code, 200)
            self.assertNotIn('CAFE', body)
            self.assertNotIn('SHOP', body)
            self.assertNotIn(ada_note, body)
            self.assertNotIn(ben_note, body)
        self.assertEqual(self.client.get('/employees').status_code, 404)
        self.assertEqual(self.client.get('/v').status_code, 404)

        routes = [
            ('get', '/admin'),
            ('get', '/admin/log.csv'),
            ('get', '/admin/links.json'),
            ('get', '/admin/summary'),
            ('get', f'/admin/summary/{AUG}'),
            ('get', f'/admin/summary/{AUG}/summary.pdf'),
            ('get', f'/admin/summary/{AUG}/summary.csv'),
            ('get', f'/admin/summary/{AUG}/summary.xlsx'),
            ('post', f'/admin/summary/{AUG}/qb-submit'),
            ('post', f'/admin/summary/{AUG}/categories'),
            ('post', '/admin/upload'),
            ('post', f'/admin/reset/{AUG}/ben'),
        ]
        for method, path in routes:
            attempts = (
                {},
                {'auth': ('Ada', self.ada)},
                {'auth': ('Chris', self.ada)},
                {'auth': ('ben', self.ben)},
                {'headers': {'Authorization': 'Bearer ' + self.ada}},
            )
            for kwargs in attempts:
                res = getattr(self.client, method)(path, **kwargs)
                self.assertIn(res.status_code, (401, 403), (method, path, res.status_code))
                body = res.get_data(as_text=True)
                self.assertNotIn('CAFE', body)
                self.assertNotIn('SHOP', body)
                self.assertNotIn(ada_note, body)
                self.assertNotIn(ben_note, body)
                self.assertNotIn(self.ada, body)
                self.assertNotIn(self.ben, body)
            refused = getattr(self.client, method)(path, auth=('Ada', 'local-admin'))
            self.assertEqual(refused.status_code, 403, path)
            refused_body = refused.get_data(as_text=True)
            self.assertNotIn('SHOP', refused_body)
            self.assertNotIn(ben_note, refused_body)
            self.assertNotIn(self.ben, refused_body)

        opened = self.client.get('/admin/log.csv', auth=('Meghan', 'local-admin'))
        self.assertEqual(opened.status_code, 200)
        log_body = opened.get_data(as_text=True)
        self.assertIn(ada_note, log_body)
        self.assertIn(ben_note, log_body)

    def test_grid_prefill_order_and_money_parse(self):
        self.assertEqual(summary.parse_money_cents('$1,234.50'), 123450)
        self.assertEqual(summary.parse_money_cents('-$1.00'), -100)
        self.assertEqual(summary.parse_money_cents('0'), 0)
        with self.assertRaises(ValueError):
            summary.parse_money_cents('12.345')
        self.assertEqual(summary.grid_category_name(' fuel '), 'Travel Fuel')
        self.assertEqual(summary.grid_category_name('travel'), 'Travel')
        self.assertEqual(summary.grid_category_name('Restaurant'), 'Meals and Entertainment')
        self.assertEqual(summary.grid_category_name('Meals'), 'Meals and Entertainment')
        self.assertEqual(summary.grid_category_name('Entertainment'), 'Meals and Entertainment')
        self.assertEqual(summary.grid_category_name('Merchandise & Supplies'), 'Supplies')
        self.assertEqual(summary.grid_category_name('Office supplies'), 'Supplies')
        self.assertEqual(summary.grid_category_name('Software'), 'Office Expenses:Software')
        self.assertEqual(summary.grid_category_name('Office Expenses Software'), 'Office Expenses:Software')
        self.assertEqual(summary.grid_category_name('Parts & Materials'), 'Job Materials')
        self.assertEqual(summary.grid_category_name('Auto Allowance Fuel'), 'Auto Allowance:Fuel')
        self.assertEqual(summary.grid_category_name('Transportation'), 'Other General and Admin Expenses')
        self.assertEqual(summary.grid_category_name('Business Services'), 'Other General and Admin Expenses')
        self.assertEqual(summary.grid_category_name('Communications'), 'Other General and Admin Expenses')
        self.assertEqual(summary.grid_category_name('Parking'), 'Other General and Admin Expenses')
        self.assertEqual(summary.grid_category_name('Other'), 'Other General and Admin Expenses')
        self.assertIsNone(summary.lookup_category('Not A Real Category'))
        people = [{
            'submitted': True,
            'charges': [
                {'original_category': 'Restaurant', 'final_category': 'Meals', 'amount_cents': 1000},
                {'original_category': 'Fuel', 'final_category': 'Fuel', 'amount_cents': 250},
                {'original_category': 'Parking', 'final_category': 'Parking', 'amount_cents': 75},
                {'original_category': 'Merchandise & Supplies', 'final_category': 'Merchandise & Supplies', 'amount_cents': 40},
            ],
        }]
        rows, total = summary.category_grid(people, None)
        names = [name for name, _cents in rows]
        self.assertEqual(names, list(summary.CATEGORIES))
        amounts = dict(rows)
        self.assertEqual(amounts['Meals and Entertainment'], 1000)
        self.assertEqual(amounts['Travel Fuel'], 250)
        self.assertEqual(amounts['Travel'], 0)
        self.assertEqual(amounts['Supplies'], 40)
        self.assertEqual(amounts['Other General and Admin Expenses'], 75)
        self.assertEqual(amounts['Auto Allowance:Fuel'], 0)
        self.assertNotIn('Parking', amounts)
        self.assertNotIn('Restaurant', amounts)
        self.assertNotIn('Merchandise & Supplies', amounts)
        self.assertNotIn('Fuel', amounts)
        self.assertEqual(total, 1365)
        edited, edited_total = summary.category_grid(people, {'fuel': 500, 'Software': 10, 'Restaurant': 300})
        self.assertEqual(dict(edited)['Travel Fuel'], 500)
        self.assertEqual(dict(edited)['Office Expenses:Software'], 10)
        self.assertEqual(dict(edited)['Meals and Entertainment'], 300)
        self.assertEqual(names, [name for name, _cents in edited])
        self.assertEqual(edited_total, 300 + 500 + 40 + 75 + 10)

    def test_saved_grid_persists_per_statement(self):
        self.submit_employee(self.ada, 'Entertainment', 2)
        self.submit_employee(self.ben)
        built = summary.build_summary(app.load_statements()[AUG], app.state)
        amounts = [(name, cents / 100) for name, cents in built['categories']]
        amounts = [(name, 4.25 if name == 'Meals and Entertainment' else dollars) for name, dollars in amounts]
        saved = self.post_grid(amounts, user='Meghan')
        self.assertEqual(saved.status_code, 303)
        page = self.page()
        self.assertIn("data-category='Meals and Entertainment' value='$4.25'", page)
        self.assertIn('>Auto Allowance Fuel</td>', page)
        self.assertIn("value='Auto Allowance:Fuel'", page)
        self.assertIn('>Office Expenses Software</td>', page)
        self.assertIn("value='Office Expenses:Software'", page)
        self.assertIn("id='grid-saved'>Saved Oct 21, 2026 9:30 AM CT by Meghan.", page)
        self.assertIn('Category total $6.75 does not match the Amex statement total $17.50. Difference: $10.75.', page)
        self.assertIn("id='qb-submit' disabled", page)
        again = self.page(user='Chris')
        self.assertIn("data-category='Meals and Entertainment' value='$4.25'", again)
        grid_file = pathlib.Path(app.DATA_DIR, 'summary-grids', AUG + '.json')
        stored = json.loads(grid_file.read_text())
        self.assertEqual(stored['by'], 'Meghan')
        self.assertEqual(stored['amounts']['Meals and Entertainment'], 425)
        self.assertEqual(stored['stmt'], AUG)
        other = self.page(JUL)
        self.assertIn("data-category='Meals and Entertainment' value='$0.00'", other)
        self.assertFalse(pathlib.Path(app.DATA_DIR, 'summary-grids', JUL + '.json').exists())
        self.assertNotIn('$4.25', other)
        logs = [r for r in app.read_log() if r['type'] == 'summary_grid']
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]['by'], 'Meghan')
        self.assertEqual(logs[0]['stmt'], AUG)
        repeat = self.post_grid(amounts, user='Meghan')
        self.assertEqual(repeat.status_code, 303)
        self.assertEqual(len([r for r in app.read_log() if r['type'] == 'summary_grid']), 1)

    def test_saved_grid_migrates_old_names(self):
        self.submit_employee(self.ada, 'Entertainment', 2)
        self.submit_employee(self.ben)
        path = pathlib.Path(app.DATA_DIR, 'summary-grids', AUG + '.json')
        path.parent.mkdir(parents=True, exist_ok=True)
        old = {
            'stmt': AUG,
            'by': 'Chris',
            'ts': '2026-10-01T12:00:00-05:00',
            'amounts': {
                'Travel': 100,
                'Fuel': 200,
                'Meals': 300,
                'Supplies': 400,
                'Parts & Materials': 500,
                'Software': 600,
                'Entertainment': 700,
                'Other': 800,
            },
        }
        raw_text = json.dumps(old, indent=1) + '\n'
        path.write_text(raw_text)
        page = self.page()
        self.assertIn("data-category='Travel' value='$1.00'", page)
        self.assertIn("data-category='Travel Fuel' value='$2.00'", page)
        self.assertIn("data-category='Auto Allowance:Fuel' value='$0.00'", page)
        self.assertIn("data-category='Meals and Entertainment' value='$10.00'", page)
        self.assertIn("data-category='Supplies' value='$4.00'", page)
        self.assertIn("data-category='Office Expenses:Software' value='$6.00'", page)
        self.assertIn("data-category='Job Materials' value='$5.00'", page)
        self.assertIn("data-category='Other General and Admin Expenses' value='$8.00'", page)
        self.assertIn('>Auto Allowance Fuel</td>', page)
        self.assertIn('>Office Expenses Software</td>', page)
        self.assertEqual(path.read_text(), raw_text)
        stored = json.loads(path.read_text())
        self.assertEqual(stored['by'], 'Chris')
        self.assertEqual(stored['ts'], '2026-10-01T12:00:00-05:00')
        self.assertEqual(stored['amounts']['Fuel'], 200)
        self.assertEqual(stored['amounts']['Meals'], 300)
        self.assertEqual(stored['amounts']['Entertainment'], 700)
        self.assertNotIn('Travel Fuel', stored['amounts'])
        self.assertNotIn('Meals and Entertainment', stored['amounts'])
        self.assertEqual([r for r in app.read_log() if r['type'] == 'summary_grid'], [])
        self.assertEqual([r for r in app.read_log() if r.get('kind') == 'summary_grid'], [])
        kept = [
            ('Travel', 1), ('Travel Fuel', 2), ('Auto Allowance:Fuel', 0),
            ('Meals and Entertainment', 10), ('Supplies', 4),
            ('Office Expenses:Software', 6), ('Job Materials', 5),
            ('Other General and Admin Expenses', 8),
        ]
        self.assertEqual(self.post_grid(kept).status_code, 303)
        saved = json.loads(path.read_text())
        self.assertEqual(saved['by'], 'Chris')
        self.assertEqual(saved['amounts'], {
            'Travel': 100,
            'Travel Fuel': 200,
            'Auto Allowance:Fuel': 0,
            'Meals and Entertainment': 1000,
            'Supplies': 400,
            'Office Expenses:Software': 600,
            'Job Materials': 500,
            'Other General and Admin Expenses': 800,
        })
        self.assertNotIn('Fuel', saved['amounts'])
        self.assertNotIn('Meals', saved['amounts'])
        self.assertNotIn('Entertainment', saved['amounts'])

    def test_explicit_save_emails_the_grid_record(self):
        self.submit_employee(self.ada, 'Entertainment', 2)
        for path in pathlib.Path(app.OUTBOX).glob('*.eml'):
            path.unlink()
        amounts = [
            ('Travel', 0), ('Travel Fuel', 0), ('Auto Allowance:Fuel', 0),
            ('Meals and Entertainment', 15), ('Supplies', -1),
            ('Office Expenses:Software', 0), ('Job Materials', 0),
            ('Other General and Admin Expenses', 0),
        ]
        auto_data = self.grid_data(amounts)
        auto_data['csrf'] = self.csrf_token()
        auto = self.client.post(
            f'/admin/summary/{AUG}/categories',
            data=auto_data,
            headers={'Accept': 'application/json'},
            auth=('Chris', 'local-admin'),
        )
        self.assertEqual(auto.status_code, 200)
        self.assertEqual(auto.get_json()['grid_total'], 14.0)
        self.assertEqual(list(pathlib.Path(app.OUTBOX).glob('*.eml')), [])
        self.assertEqual([r for r in app.read_log() if r.get('kind') == 'summary_grid'], [])
        grid_file = pathlib.Path(app.DATA_DIR, 'summary-grids', AUG + '.json')
        self.assertEqual(json.loads(grid_file.read_text())['amounts']['Supplies'], -100)
        self.assertEqual(json.loads(grid_file.read_text())['by'], 'Chris')

        locked_during_send = []
        real_send = app.send_prepared

        def watch_send(msg):
            locked_during_send.append(app.SUBMIT_LOCK.locked())
            return real_send(msg)

        app.send_prepared = watch_send
        saved = self.post_grid(amounts, user='Meghan')
        app.send_prepared = real_send
        self.assertEqual(saved.status_code, 303)
        self.assertEqual(locked_during_send, [False])
        page = self.page()
        self.assertIn("name='save' value='1' id='save-categories'", page)
        self.assertIn("ev.key === 'Enter'", page)
        self.assertIn('btn.disabled = true', page)
        self.assertIn('emails that grid', page)
        self.assertIn('Typing in a box saves here only.', page)
        messages = list(pathlib.Path(app.OUTBOX).glob('*.eml'))
        self.assertEqual(len(messages), 1)
        msg = email.message_from_bytes(messages[0].read_bytes())
        self.assertEqual(msg['To'], 'other@example.com')
        self.assertIsNone(msg['Cc'])
        self.assertEqual(msg['From'], 'Chris AI <littledude@coxresearch.com>')
        self.assertEqual(''.join(str(msg['Subject']).splitlines()),
                         'Amex summary grid SAVED: American Express charges August 2026')
        text = html_body = ''
        for part in msg.walk():
            if part.get_content_type() == 'text/plain':
                text = part.get_payload(decode=True).decode()
            elif part.get_content_type() == 'text/html':
                html_body = part.get_payload(decode=True).decode()
        expected = (
            "Saved by: Meghan\n"
            "Saved at: 2026-10-21T09:30:00-05:00\n"
            "Statement: 2026-08-22\n"
            "Folder: American Express charges August 2026\n"
            "\n"
            "Category | Amount\n"
            "Travel | $0.00\n"
            "Travel Fuel | $0.00\n"
            "Auto Allowance:Fuel | $0.00\n"
            "Meals and Entertainment | $15.00\n"
            "Supplies | -$1.00\n"
            "Office Expenses:Software | $0.00\n"
            "Job Materials | $0.00\n"
            "Other General and Admin Expenses | $0.00\n"
            "Grand total | $14.00\n"
            "\n"
        )
        grid_json = grid_file.read_text()
        self.assertEqual(text, expected + grid_json)
        self.assertIn(grid_json.rstrip('\n'), html.unescape(html_body))
        self.assertIn('<th>Category</th><th>Amount</th>', html_body)
        self.assertIn('-$1.00', html_body)
        self.assertIn('<b>Grand total</b>', html_body)
        self.assertIn('Saved by: Meghan', html_body)
        self.assertIn('Saved at: 2026-10-21T09:30:00-05:00', html_body)
        self.assertIn('Statement: 2026-08-22', html_body)
        self.assertIn('American Express charges August 2026', html_body)
        self.assertNotIn('QB SUBMIT', str(msg['Subject']))
        mailed = [r for r in app.read_log() if r.get('kind') == 'summary_grid']
        self.assertEqual(len(mailed), 1)
        self.assertEqual(mailed[0]['type'], 'email')
        self.assertEqual(mailed[0]['by'], 'Meghan')
        self.assertEqual(mailed[0]['stmt'], AUG)
        self.assertEqual(mailed[0]['to'], 'other@example.com')
        self.assertTrue(mailed[0]['status'].startswith('dryrun:'))
        self.assertEqual(len([r for r in app.read_log() if r['type'] == 'summary_grid']), 1)

        again = self.post_grid(amounts, user='Meghan')
        self.assertEqual(again.status_code, 303)
        self.assertEqual(len([r for r in app.read_log() if r.get('kind') == 'summary_grid']), 2)
        self.assertEqual(len([r for r in app.read_log() if r['type'] == 'summary_grid']), 1)

        def boom(_msg):
            raise OSError('smtp down')
        app.send_prepared = boom
        changed = [(name, 2 if name == 'Travel Fuel' else dollars) for name, dollars in amounts]
        failed = self.post_grid(changed, user='Chris')
        self.assertEqual(failed.status_code, 303)
        stored = json.loads(grid_file.read_text())
        self.assertEqual(stored['by'], 'Chris')
        self.assertEqual(stored['amounts']['Travel Fuel'], 200)
        self.assertEqual(stored['stmt'], AUG)
        err = [r for r in app.read_log() if r.get('kind') == 'summary_grid'][-1]
        self.assertEqual(err['status'], 'error: OSError: smtp down')
        self.assertEqual(err['to'], 'other@example.com')
        kept = email.message_from_bytes(messages[0].read_bytes())
        kept_text = ''
        for part in kept.walk():
            if part.get_content_type() == 'text/plain':
                kept_text = part.get_payload(decode=True).decode()
        self.assertIn('Supplies | -$1.00', kept_text)
        self.assertNotIn('Travel Fuel | $2.00', kept_text)

        app.send_prepared = self._send
        app.RECORD_CC = 'chris@coxresearch.com'
        try:
            copied = self.post_grid(changed, user='Chris')
        finally:
            app.RECORD_CC = ''
        self.assertEqual(copied.status_code, 303)
        latest = max(pathlib.Path(app.OUTBOX).glob('*.eml'), key=lambda p: p.stat().st_mtime_ns)
        cc_msg = email.message_from_bytes(latest.read_bytes())
        self.assertEqual(cc_msg['To'], 'other@example.com')
        self.assertEqual(cc_msg['Cc'], 'chris@coxresearch.com')
        self.assertEqual(''.join(str(cc_msg['Subject']).splitlines()),
                         'Amex summary grid SAVED: American Express charges August 2026')

    def test_qb_submit_uses_grid_values(self):
        self.submit_employee(self.ada, 'Entertainment', 2)
        self.submit_employee(self.ben)
        built = summary.build_summary(app.load_statements()[AUG], app.state)
        amounts = []
        for name, _cents in built['categories']:
            if name == 'Travel':
                amounts.append((name, 10))
            elif name == 'Travel Fuel':
                amounts.append((name, 7.5))
            else:
                amounts.append((name, 0))
        self.assertEqual(self.post_grid(amounts).status_code, 303)
        edited = self.page()
        self.assertIn("data-category='Travel' value='$10.00'", edited)
        self.assertIn("data-category='Travel Fuel' value='$7.50'", edited)
        self.assertIn("data-category='Meals and Entertainment' value='$0.00'", edited)
        self.assertIn("value='Auto Allowance:Fuel'", edited)
        self.assertNotIn("id='summary-warning'", edited)
        self.assertIn("id='qb-submit'>Submit to QuickBooks for Pay", edited)
        blocked_data = self.grid_data([('Not A Real Category', '1.00')])
        blocked_data['csrf'] = self.csrf_token()
        blocked = self.client.post(
            f'/admin/summary/{AUG}/qb-submit',
            data=blocked_data,
            auth=('Chris', 'local-admin'),
        )
        self.assertEqual(blocked.status_code, 400)
        bad_amount = self.client.post(
            f'/admin/summary/{AUG}/categories',
            data={'cat': 'Travel Fuel', 'amt': 'nope', 'csrf': self.csrf_token()},
            auth=('Chris', 'local-admin'),
        )
        self.assertEqual(bad_amount.status_code, 400)
        bad_page = bad_amount.get_data(as_text=True)
        self.assertIn("id='amount-error'>Enter each amount as dollars, such as 12.50 or -1.00.", bad_page)
        self.assertIn("data-category='Travel Fuel' value='$7.50'", bad_page)
        self.assertNotIn('nope', bad_page)
        bad_json = self.client.post(
            f'/admin/summary/{AUG}/categories',
            data={'cat': 'Travel Fuel', 'amt': 'nope', 'csrf': self.csrf_token()},
            headers={'Accept': 'application/json'},
            auth=('Chris', 'local-admin'),
        )
        self.assertEqual(bad_json.status_code, 400)
        self.assertEqual(bad_json.get_json()['error'],
                         'Enter each amount as dollars, such as 12.50 or -1.00.')
        self.assertEqual(json.loads(pathlib.Path(app.DATA_DIR, 'summary-grids', AUG + '.json').read_text())['amounts']['Travel Fuel'], 750)
        sent = self.auth_post(f'/admin/summary/{AUG}/qb-submit')
        self.assertEqual(sent.status_code, 303)
        rec = next(r for r in app.read_log() if r['type'] == 'qb_submit')
        self.assertEqual(rec['categories']['Travel'], 10.0)
        self.assertEqual(rec['categories']['Travel Fuel'], 7.5)
        self.assertEqual(rec['categories']['Meals and Entertainment'], 0.0)
        self.assertEqual(rec['categories']['Office Expenses:Software'], 0.0)
        self.assertNotIn('Fuel', rec['categories'])
        self.assertNotIn('Entertainment', rec['categories'])
        self.assertNotIn('Restaurant', rec['categories'])
        self.assertNotIn('Office Supplies', rec['categories'])
        self.assertNotIn('Merchandise & Supplies', rec['categories'])
        self.assertEqual(rec['summary_total'], 17.5)
        self.assertEqual(rec['statement_total'], 17.5)
        msg = self.qb_messages()[0]
        text = ''
        files = {}
        for part in msg.walk():
            name = part.get_filename()
            if name:
                files[name] = part.get_payload(decode=True)
            elif part.get_content_type() == 'text/plain':
                text = part.get_payload(decode=True).decode()
        block = json.loads(text[text.index('{'):text.rindex('}') + 1])
        self.assertEqual(block['categories']['Travel'], 10.0)
        self.assertEqual(block['categories']['Travel Fuel'], 7.5)
        self.assertEqual(block['categories']['Meals and Entertainment'], 0.0)
        self.assertEqual(block['accounts'][1], {'account': 'Travel Fuel', 'amount': 7.5})
        self.assertEqual(block['grand_total'], 17.5)
        self.assertEqual(block['statement_total'], 17.5)
        pdf_name = 'American Express charges August 2026 - Summary.pdf'
        self.assertIn(b'Travel: $10.00', files[pdf_name])
        self.assertIn(b'Travel Fuel: $7.50', files[pdf_name])
        self.assertIn(b'Meals and Entertainment: $0.00', files[pdf_name])
        self.assertIn(b'Auto Allowance Fuel: $0.00', files[pdf_name])
        self.assertNotIn(b'Auto Allowance:Fuel:', files[pdf_name])
        ada = json.loads(files['ada-submission.json'])
        air = next(c for c in ada['charges'] if c['merchant'] == 'AIR')
        self.assertEqual(air['final_category'], 'Meals and Entertainment')
        self.assertEqual(air['original_category'], 'Travel')
        locked = self.post_grid(amounts)
        self.assertEqual(locked.status_code, 409)
        self.assertIn('sent for posting', locked.get_data(as_text=True).lower())

    def test_mismatch_blocks_submit_until_grid_matches(self):
        self.submit_employee(self.ada, 'Entertainment', 2)
        self.submit_employee(self.ben)
        built = summary.build_summary(app.load_statements()[AUG], app.state)
        amounts = [(name, 100 if name == 'Travel' else 0) for name, _cents in built['categories']]
        self.assertEqual(self.post_grid(amounts).status_code, 303)
        page = self.page()
        self.assertIn('Category total $100.00 does not match the Amex statement total $17.50. '
                      'Difference: -$82.50.', page)
        self.assertIn("id='qb-submit' disabled", page)
        refused = self.auth_post(f'/admin/summary/{AUG}/qb-submit')
        self.assertEqual(refused.status_code, 409)
        self.assertEqual(refused.get_json()['error'], 'not ready')
        self.assertEqual(refused.get_json()['difference'], -82.5)
        self.assertEqual(self.qb_messages(), [])
        fixed = [(name, 17.5 if name == 'Office Expenses:Software' else 0) for name, _cents in built['categories']]
        self.assertEqual(self.post_grid(fixed, user='Meghan').status_code, 303)
        ready = self.page(user='meghan')
        self.assertNotIn("id='summary-warning'", ready)
        self.assertIn("data-category='Office Expenses:Software' value='$17.50'", ready)
        self.assertIn('>Office Expenses Software</td>', ready)
        sent = self.auth_post(f'/admin/summary/{AUG}/qb-submit', user='Meghan')
        self.assertEqual(sent.status_code, 303)
        rec = next(r for r in app.read_log() if r['type'] == 'qb_submit')
        self.assertEqual(rec['by'], 'Meghan')
        self.assertEqual(rec['categories']['Office Expenses:Software'], 17.5)
        self.assertEqual(rec['categories']['Meals and Entertainment'], 0.0)
        self.assertNotIn('Software', rec['categories'])
        self.assertNotIn('Entertainment', rec['categories'])

    def test_matching_grid_still_waits_for_every_employee(self):
        self.submit_employee(self.ada)
        built = summary.build_summary(app.load_statements()[AUG], app.state)
        amounts = [(name, 17.5 if name == 'Supplies' else 0) for name, _cents in built['categories']]
        self.assertEqual(self.post_grid(amounts).status_code, 303)
        page = self.page()
        self.assertIn("<p class='warn' id='summary-warning'>Not everyone has submitted.</p>", page)
        self.assertIn("id='qb-submit' disabled", page)
        refused = self.auth_post(f'/admin/summary/{AUG}/qb-submit')
        self.assertEqual(refused.status_code, 409)
        self.assertEqual(refused.get_json()['difference'], 0)
        self.assertEqual(self.qb_messages(), [])

    def _summary_copies(self):
        found = []
        for path in pathlib.Path(app.OUTBOX).glob('*.eml'):
            msg = email.message_from_bytes(path.read_bytes())
            if str(msg['Subject'] or '').startswith('American Express summary:'):
                found.append(msg)
        return found

    def test_confirmed_submit_emails_summary_only_to_owners(self):
        self.assertEqual(app.summary_copy_recipients(),
                         ['chris@coxresearch.com', 'meghan@coxresearch.com'])
        self.submit_employee(self.ada, 'Entertainment', 2)
        self.submit_employee(self.ben)
        sent = self.auth_post(f'/admin/summary/{AUG}/qb-submit')
        self.assertEqual(sent.status_code, 303)
        copies = self._summary_copies()
        self.assertEqual(len(copies), 1)
        msg = copies[0]
        self.assertEqual(''.join(str(msg['Subject']).splitlines()),
                         'American Express summary: August 2026')
        self.assertEqual(msg['To'], 'chris@coxresearch.com, meghan@coxresearch.com')
        self.assertIsNone(msg['Cc'])
        self.assertIsNone(msg['Bcc'])
        header_blob = ' '.join(str(msg[h] or '') for h in ('To', 'Cc', 'Bcc'))
        for banned in ('ada@coxresearch.com', 'ben@coxresearch.com', 'other@example.com',
                       'littledude@mail.grokbot.com', 'cara@coxresearch.com'):
            self.assertNotIn(banned, header_blob)
        text = html_body = ''
        files = {}
        for part in msg.walk():
            name = part.get_filename()
            if name:
                files[name] = part.get_payload(decode=True)
            elif part.get_content_type() == 'text/plain':
                text = part.get_payload(decode=True).decode()
            elif part.get_content_type() == 'text/html':
                html_body = part.get_payload(decode=True).decode()
        pdf_name = 'American Express charges August 2026 - Summary.pdf'
        xlsx_name = 'American Express charges August 2026 - Summary.xlsx'
        self.assertEqual(set(files), {pdf_name, xlsx_name})
        self.assertTrue(files[pdf_name].startswith(b'%PDF'))
        self.assertIn(b'Grand total: $17.50', files[pdf_name])
        with zipfile.ZipFile(io.BytesIO(files[xlsx_name])) as zf:
            sheet = zf.read('xl/worksheets/sheet1.xml').decode()
        self.assertIn('Grand total', sheet)
        self.assertIn('17.50', sheet)
        for name in summary.CATEGORIES:
            self.assertIn(f'{name} | ', text)
            self.assertIn(html.escape(name), html_body)
        self.assertIn('Grand total | $17.50', text)
        self.assertIn('<b>Grand total</b>', html_body)
        self.assertIn('Submitted by: Chris', text)
        self.assertIn('Statement: 2026-08-22', text)
        self.assertNotIn('submission.json', text)
        logged = [r for r in app.read_log() if r.get('kind') == 'summary_copy']
        self.assertEqual(len(logged), 1)
        self.assertEqual(logged[0]['to'], 'chris@coxresearch.com, meghan@coxresearch.com')
        self.assertEqual(logged[0]['by'], 'Chris')
        self.assertEqual(logged[0]['stmt'], AUG)
        self.assertTrue(str(logged[0]['status']).startswith('dryrun:'))
        qb = self.qb_messages()
        self.assertEqual(len(qb), 1)
        self.assertEqual(qb[0]['To'], 'littledude@mail.grokbot.com')

    def test_summary_copy_failure_keeps_the_lock(self):
        self.submit_employee(self.ada, 'Entertainment', 2)
        self.submit_employee(self.ben)
        real = app.send_prepared

        def selective(msg):
            if str(msg['Subject'] or '').startswith('QB SUBMIT:'):
                return real(msg)
            raise OSError('summary smtp down')

        app.send_prepared = selective
        sent = self.auth_post(f'/admin/summary/{AUG}/qb-submit')
        self.assertEqual(sent.status_code, 303)
        self.assertEqual(len([r for r in app.read_log() if r['type'] == 'qb_submit']), 1)
        logged = [r for r in app.read_log() if r.get('kind') == 'summary_copy']
        self.assertEqual(len(logged), 1)
        self.assertEqual(logged[0]['status'], 'error: OSError: summary smtp down')
        self.assertEqual(logged[0]['to'], 'chris@coxresearch.com, meghan@coxresearch.com')
        self.assertIn('Sent for posting', self.page())
        locked = self.post_grid([(name, 1) for name in summary.CATEGORIES])
        self.assertEqual(locked.status_code, 409)
        self.assertEqual(self._summary_copies(), [])
        self.assertEqual(len(self.qb_messages()), 1)

    def test_summary_to_drops_employees(self):
        app.SUMMARY_TO = ' ada@coxresearch.com, books@example.com, Meghan@CoxResearch.com '
        self.assertEqual(app.summary_copy_recipients(), ['Meghan@CoxResearch.com'])
        self.assertFalse(app._coxresearch_address('books@example.com'))
        self.assertFalse(app._coxresearch_address('meghan@coxresearch.com.evil'))
        self.assertTrue(app._coxresearch_address('chris@coxresearch.com'))
        self.submit_employee(self.cara)
        sent = self.auth_post(f'/admin/summary/{JUL}/qb-submit', user='Meghan')
        self.assertEqual(sent.status_code, 303)
        copies = self._summary_copies()
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0]['To'], 'Meghan@CoxResearch.com')
        self.assertEqual(copies[0]['Subject'], 'American Express summary: July 2026')
        self.assertIsNone(copies[0]['Cc'])
        self.assertIsNone(copies[0]['Bcc'])
        self.assertNotIn('ada@', copies[0]['To'])
        self.assertNotIn('cara@', copies[0]['To'])
        self.assertNotIn('books@', copies[0]['To'])
        self.assertNotIn('chris@', copies[0]['To'])
        logged = [r for r in app.read_log() if r.get('kind') == 'summary_copy']
        self.assertEqual(logged[0]['to'], 'Meghan@CoxResearch.com')
        self.assertEqual(logged[0]['by'], 'Meghan')

        app.SUMMARY_TO = 'ada@coxresearch.com, ben@coxresearch.com, cara@coxresearch.com'
        self.assertEqual(app.summary_copy_recipients(), [])
        self.submit_employee(self.ada)
        self.submit_employee(self.ben)
        skipped = self.auth_post(f'/admin/summary/{AUG}/qb-submit')
        self.assertEqual(skipped.status_code, 303)
        self.assertEqual(len([r for r in app.read_log() if r['type'] == 'qb_submit' and r['stmt'] == AUG]), 1)
        skip_log = next(r for r in app.read_log() if r.get('kind') == 'summary_copy' and r['stmt'] == AUG)
        self.assertEqual(skip_log['status'], 'skipped: no recipients')
        self.assertEqual(skip_log['to'], '')
        self.assertFalse(any(str(m['Subject']).endswith('August 2026') for m in self._summary_copies()))
        self.assertIn('Sent for posting', self.page())

    def test_summary_posts_require_csrf(self):
        self.submit_employee(self.ada)
        self.submit_employee(self.ben)
        for path in pathlib.Path(app.OUTBOX).glob('*.eml'):
            path.unlink()
        page = self.page()
        self.assertIn("name='csrf' value='", page)
        self.assertIn("input[name='csrf']", page)
        token = self.csrf_token()
        self.assertGreaterEqual(len(token), 32)
        amounts = [(name, 17.5 if name == 'Supplies' else 0) for name in summary.CATEGORIES]
        data = self.grid_data(amounts)
        cat = f'/admin/summary/{AUG}/categories'
        qb = f'/admin/summary/{AUG}/qb-submit'
        grid_file = pathlib.Path(app.DATA_DIR, 'summary-grids', AUG + '.json')

        def post(path, payload, headers=None):
            return self.client.post(path, data=payload, headers=headers, auth=('Chris', 'local-admin'))

        missing = post(cat, dict(data))
        self.assertEqual(missing.status_code, 403)
        missing_qb = post(qb, dict(data))
        self.assertEqual(missing_qb.status_code, 403)
        wrong = post(cat, {**data, 'csrf': 'not-the-session-token'})
        self.assertEqual(wrong.status_code, 403)
        wrong_qb = post(qb, {**data, 'csrf': token + 'x'})
        self.assertEqual(wrong_qb.status_code, 403)
        foreign = post(cat, {**data, 'csrf': token, 'save': '1'},
                       headers={'Origin': 'https://evil.example'})
        self.assertEqual(foreign.status_code, 403)
        foreign_ref = post(qb, {**data, 'csrf': token},
                           headers={'Referer': 'https://evil.example/attack'})
        self.assertEqual(foreign_ref.status_code, 403)
        self.assertFalse(grid_file.exists())
        self.assertEqual(list(pathlib.Path(app.OUTBOX).glob('*.eml')), [])
        self.assertFalse(pathlib.Path(app.DATA_DIR, 'summaries').exists())
        self.assertEqual([r for r in app.read_log() if r['type'] in ('qb_submit', 'summary_grid')], [])
        self.assertEqual([r for r in app.read_log() if r.get('kind') == 'summary_grid'], [])

        auto = post(cat, {**data, 'csrf': token}, headers={'Accept': 'application/json'})
        self.assertEqual(auto.status_code, 200)
        self.assertEqual(auto.get_json()['grid_total'], 17.5)
        self.assertEqual(list(pathlib.Path(app.OUTBOX).glob('*.eml')), [])
        self.assertEqual(json.loads(grid_file.read_text())['amounts']['Supplies'], 1750)
        self.assertEqual([r for r in app.read_log() if r.get('kind') == 'summary_grid'], [])

        saved = post(cat, {**data, 'csrf': token, 'save': '1'})
        self.assertEqual(saved.status_code, 303)
        self.assertEqual(len(list(pathlib.Path(app.OUTBOX).glob('*.eml'))), 1)
        self.assertEqual(len([r for r in app.read_log() if r['type'] == 'qb_submit']), 0)

        sent = post(qb, {**data, 'csrf': token}, headers={
            'Origin': 'http://127.0.0.1:10077',
            'Referer': f'http://127.0.0.1:10077/admin/summary/{AUG}',
        })
        self.assertEqual(sent.status_code, 303)
        self.assertEqual(len([r for r in app.read_log() if r['type'] == 'qb_submit']), 1)
        self.assertIn('Sent for posting', self.page())

    def test_no_service_credentials(self):
        src = ((ROOT / 'app.py').read_text() + '\n' + (ROOT / 'summary.py').read_text()
               + '\n' + (ROOT / 'render.yaml').read_text()).lower()
        for banned in ('intuit.com', 'api.dropbox', 'api.dropboxapi.com', 'dropbox_token',
                       'dropbox_app_key', 'quickbooks_client', 'quickbooks_secret', 'quickbooks_realm',
                       'americanexpress.com', 'api.amex'):
            self.assertNotIn(banned, src)
        self.assertNotIn('import requests', src)
        self.assertNotIn('urllib', (ROOT / 'summary.py').read_text().lower())


if __name__ == '__main__':
    unittest.main()
