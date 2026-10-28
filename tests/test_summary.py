#!/usr/bin/env python3
"""Monthly summary for Chris and Meghan. Run from the repo root:

  .venv/bin/python tests/test_summary.py
"""
import email, html, json, os, pathlib, tempfile, unittest, zipfile

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
        if os.path.exists(app.LOG):
            os.remove(app.LOG)
        out = pathlib.Path(app.OUTBOX)
        out.mkdir(parents=True, exist_ok=True)
        for f in out.glob('*'):
            f.unlink()
        summaries = pathlib.Path(app.DATA_DIR, 'summaries')
        if summaries.exists():
            for p in sorted(summaries.rglob('*'), reverse=True):
                if p.is_file():
                    p.unlink()
                else:
                    p.rmdir()
            summaries.rmdir()

    def tearDown(self):
        app.now = self._now
        app.send_prepared = self._send

    def auth_get(self, path, user='Chris', password='local-admin'):
        return self.client.get(path, auth=(user, password))

    def auth_post(self, path, user='Chris', password='local-admin'):
        return self.client.post(path, auth=(user, password))

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
        self.assertEqual(summary.final_category({'id': 1, 'category': 'Meals'}, {1: 'Gift'}), 'Gift')
        self.assertEqual(summary.final_category({'id': 1, 'category': 'Meals'}, {}), 'Meals')
        self.assertEqual(summary.final_category({'id': 2, 'category': ' travel '}, {}), 'Travel')
        self.assertEqual(summary.final_category({'id': 3, 'category': 'Restaurant'}, {3: ''}), 'Uncategorized')
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
        self.assertIn(">Submit</button>", ada_page)

        self.submit_employee(self.ada, 'Gift', 2)
        types = [r['type'] for r in app.read_log() if r.get('stmt') == AUG and r['type'] != 'open']
        self.assertEqual(types, ['category', 'submit', 'dropbox_file', 'email', 'submit_confirm_email'])
        self.assertFalse(pathlib.Path(app.DATA_DIR, 'summaries').exists())
        self.assertEqual(self.qb_messages(), [])
        employee_mail = [email.message_from_bytes(p.read_bytes())
                         for p in pathlib.Path(app.OUTBOX).glob('*.eml')]
        msg = next(m for m in employee_mail if str(m['Subject'] or '').startswith('Amex verification SUBMITTED:'))
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
        self.assertIn('Summary total $0.00 does not equal the statement total $17.50. Difference: $17.50.', waiting)
        self.assertIn('Not everyone has submitted.', waiting)
        for name in summary.AMEX_CATEGORIES:
            self.assertIn(html.escape(name), waiting)
        blocked = self.auth_post(f'/admin/summary/{AUG}/qb-submit')
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(blocked.get_json()['error'], 'not ready')
        self.assertEqual(blocked.get_json()['difference'], 17.5)
        self.assertEqual([r for r in app.read_log() if r['type'] == 'qb_submit'], [])

        self.submit_employee(self.ada, 'Gift', 2)
        partial = self.page()
        self.assertIn('Submitted Oct 21, 2026 9:30 AM CT', partial)
        self.assertIn('>Ben</td><td><b style=\'color:#c62828\'>Not submitted</b>', partial)
        self.assertIn('Gift', partial)
        self.assertIn('$5.00', partial)
        self.assertIn('Summary total $14.00 does not equal the statement total $17.50. Difference: $3.50.', partial)
        self.assertIn("id='qb-submit' disabled", partial)
        built = summary.build_summary(app.load_statements()[AUG], app.state)
        totals = dict(built['categories'])
        self.assertEqual(totals['Gift'], 500)
        self.assertEqual(totals['Travel'], 0)
        self.assertEqual(totals['Restaurant'], 1000)
        self.assertEqual(totals['Merchandise & Supplies'], -100)
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
        built = summary.build_summary(app.load_statements()[AUG], app.state)
        self.assertTrue(built['ready'])
        self.assertEqual(built['summary_cents'], 1750)
        self.assertEqual(dict(built['categories'])['Merchandise & Supplies'], 250)
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
        self.assertEqual(rec['categories']['Gift'], 5.0)
        self.assertEqual(rec['categories']['Travel'], 0.0)
        self.assertTrue(str(rec['email_status']).startswith('dryrun:'))
        self.assertTrue(str(rec['files']).endswith('/summaries/American Express charges August 2026'))
        self.assertEqual(rec['folder'], 'American Express charges August 2026')

        done = self.page()
        self.assertIn("id='sent'>Sent for posting", done)
        self.assertIn('Oct 21, 2026 9:30 AM CT by Chris.', done)
        self.assertNotIn("id='qb-submit'", done)
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
        self.assertIn(b'Gift', files[pdf_name])
        self.assertIn('American Express charges August 2026', text)
        self.assertIn('did not contact QuickBooks or Dropbox', text)
        self.assertIn('did not contact QuickBooks or Dropbox', html_body)
        block = json.loads(text[text.index('{'):text.rindex('}') + 1])
        self.assertEqual(block['close_date'], '2026-08-22')
        self.assertEqual(block['month'], 'August 2026')
        self.assertEqual(block['folder'], 'American Express charges August 2026')
        self.assertEqual(block['grand_total'], 17.5)
        self.assertEqual(block['statement_total'], 17.5)
        self.assertEqual(block['categories']['Gift'], 5.0)
        self.assertEqual(block['categories']['Utilities'], 0.0)
        ada = json.loads(files['ada-submission.json'])
        air = next(c for c in ada['charges'] if c['merchant'] == 'AIR')
        self.assertEqual(air['original_category'], 'Travel')
        self.assertEqual(air['final_category'], 'Gift')

        for name in (pdf_name, xlsx_name, csv_name):
            self.assertTrue((folder / name).is_file())
        self.assertTrue((folder / 'submissions' / 'ada-submission.json').is_file())
        self.assertTrue((folder / 'submissions' / 'ben-submission.json').is_file())
        pdf = (folder / pdf_name).read_bytes()
        self.assertTrue(pdf.startswith(b'%PDF-1.4'))
        self.assertIn(b'%%EOF', pdf)
        self.assertIn(b'Gift: $5.00', pdf)
        csv_text = (folder / csv_name).read_text()
        self.assertIn('Gift', csv_text)
        self.assertIn('Grand total', csv_text)
        with zipfile.ZipFile(folder / xlsx_name) as zf:
            sheet = zf.read('xl/worksheets/sheet1.xml').decode()
        self.assertIn('American Express charges August 2026', sheet)
        self.assertIn('Gift', sheet)
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
        self.assertEqual(again.status_code, 303)
        self.assertEqual(len(self.qb_messages()), 1)
        self.assertEqual(len([r for r in app.read_log() if r['type'] == 'qb_submit']), 1)

        flagged = self.client.post(f'/v/{self.ada}/flag/1', data={'note': 'too late'},
                                   headers={'Accept': 'application/json'})
        self.assertEqual(flagged.status_code, 409)
        self.assertEqual(flagged.get_json()['error'], 'month locked')
        self.assertEqual(self.client.post(f'/v/{self.ada}/category/1', data={'category': 'Gift'},
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

    def test_no_service_credentials(self):
        src = ((ROOT / 'app.py').read_text() + '\n' + (ROOT / 'summary.py').read_text()
               + '\n' + (ROOT / 'render.yaml').read_text()).lower()
        for banned in ('intuit.com', 'quickbooks_client', 'quickbooks_secret', 'quickbooks_realm',
                       'americanexpress.com', 'api.amex', 'files/delete', 'permanently_delete',
                       'create_shared_link', 'sharing/create_shared_link'):
            self.assertNotIn(banned, src)
        self.assertNotIn('import requests', src)
        self.assertNotIn('urllib', (ROOT / 'summary.py').read_text().lower())


if __name__ == '__main__':
    unittest.main()
