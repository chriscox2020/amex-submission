#!/usr/bin/env python3
"""Employee katie-henderson on statement 2026-10-22 keeps Sep 23 through Oct 21.

Rows in this file are fictional. It does not submit, flag, email, or call Dropbox.
"""
import hashlib, json, os, pathlib, tempfile, unittest, zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
TMP = tempfile.mkdtemp(prefix='amex-katie-scope-')
os.environ['DATA_DIR'] = TMP
os.environ['TOKEN_SECRET'] = 'local-test-secret'
os.environ['ADMIN_PASSWORD'] = 'local-admin'
os.environ['EMAIL_MODE'] = 'dryrun'
os.environ['RECORD_TO'] = 'littledude@mail.grokbot.com'
os.environ['RECORD_CC'] = ''
os.environ['PUBLIC_BASE'] = 'http://127.0.0.1:10077'
os.environ['MAIL_FROM'] = 'Chris AI <littledude@coxresearch.com>'
for key in ('DROPBOX_APP_KEY', 'DROPBOX_APP_SECRET', 'DROPBOX_REFRESH_TOKEN'):
    os.environ.pop(key, None)

import sys
sys.path.insert(0, str(ROOT))
import app
import summary

STMT = '2026-10-22'
LATER = '2026-11-22'
INSIDE = (
    {'id': 1, 'date': '09/23/2026', 'category': 'Other', 'company': 'FICTIONAL INSIDE START', 'amount': 1.00},
    {'id': 2, 'date': '10/10/2026', 'category': 'Other', 'company': 'FICTIONAL INSIDE MID', 'amount': 2.00},
    {'id': 3, 'date': '10/21/2026', 'category': 'Other', 'company': 'FICTIONAL INSIDE END', 'amount': 4.00},
)
OUTSIDE = (
    {'id': 901, 'date': '09/21/2026', 'category': 'Other', 'company': 'FICTIONAL BEFORE START', 'amount': 8.00},
    {'id': 902, 'date': '09/22/2026', 'category': 'Other', 'company': 'FICTIONAL DAY BEFORE', 'amount': 16.00},
    {'id': 903, 'date': '10/22/2026', 'category': 'Other', 'company': 'FICTIONAL AFTER END', 'amount': 32.00},
)


def write_doc(doc):
    folder = pathlib.Path(app.DATA_DIR, 'statements')
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (doc['stmt'] + '.json')
    prior = path.read_bytes() if path.exists() else None
    path.write_text(json.dumps(doc))
    return path, prior


class KatieScopeTest(unittest.TestCase):
    def setUp(self):
        self.scope_rows = list(INSIDE) + list(OUTSIDE)
        self.other_rows = [
            {'id': 1, 'date': '09/21/2026', 'category': 'Other', 'company': 'OTHER CARD SEPTEMBER', 'amount': 3.00},
            {'id': 2, 'date': '10/02/2026', 'category': 'Other', 'company': 'OTHER CARD OCTOBER', 'amount': 5.00},
        ]
        self.doc = {
            'stmt': STMT,
            'label': 'Oct 22, 2026 statement',
            'employees': [
                {'id': 'katie-henderson', 'name': 'Scope Example', 'first': 'Scope',
                 'email': 'scope@example.com', 'rows': self.scope_rows},
                {'id': 'other-cardholder', 'name': 'Other Cardholder', 'first': 'Other',
                 'email': 'other@example.com', 'rows': self.other_rows},
            ],
        }
        self.later = {
            'stmt': LATER,
            'label': 'Nov 22, 2026 statement',
            'employees': [
                {'id': 'katie-henderson', 'name': 'Scope Example', 'first': 'Scope',
                 'email': 'scope@example.com', 'rows': [
                     {'id': 1, 'date': '09/21/2026', 'category': 'Other',
                      'company': 'LATER STATEMENT SEPTEMBER', 'amount': 7.00},
                 ]},
            ],
        }
        self.stmt_path, self._stmt_prior = write_doc(self.doc)
        self.later_path, self._later_prior = write_doc(self.later)
        self._stmt_bytes = self.stmt_path.read_bytes()
        self._later_bytes = self.later_path.read_bytes()
        log_path = pathlib.Path(app.LOG)
        self._log_prior = log_path.read_bytes() if log_path.exists() else None
        self._call = app.dropbox_call
        self._send = app.send_prepared
        app.dropbox_call = self._forbid_dropbox
        app.send_prepared = self._forbid_email
        self.client = app.app.test_client()
        self.scope_token = app.make_token(STMT, 'katie-henderson')
        self.other_token = app.make_token(STMT, 'other-cardholder')
        self.later_token = app.make_token(LATER, 'katie-henderson')

    def tearDown(self):
        app.dropbox_call = self._call
        app.send_prepared = self._send
        self._restore(self.stmt_path, self._stmt_prior)
        self._restore(self.later_path, self._later_prior)
        self._restore(pathlib.Path(app.LOG), self._log_prior)

    def _restore(self, path, prior):
        if prior is None:
            if path.exists():
                path.unlink()
        else:
            path.write_bytes(prior)

    def _forbid_dropbox(self, url, data, headers):
        raise AssertionError('Dropbox was called')

    def _forbid_email(self, msg):
        raise AssertionError('email was sent')

    def scope_employee(self):
        return next(e for e in self.doc['employees'] if e['id'] == 'katie-henderson')

    def test_window_is_the_date_range_for_this_employee_only(self):
        self.assertEqual(app.KATIE_SCOPE_STMT, STMT)
        self.assertEqual(app.KATIE_SCOPE_EMP, 'katie-henderson')
        self.assertEqual(app.KATIE_SCOPE_START.isoformat(), '2026-09-23')
        self.assertEqual(app.KATIE_SCOPE_END.isoformat(), '2026-10-21')
        self.assertEqual(app.row_charge_date({'date': '09/23/2026'}).isoformat(), '2026-09-23')
        self.assertEqual(app.row_charge_date({'date': '2026-10-21'}).isoformat(), '2026-10-21')
        self.assertIsNone(app.row_charge_date({'date': 'not-a-date'}))
        kept = app.review_rows(self.doc, self.scope_employee())
        self.assertEqual({r['id'] for r in kept}, {r['id'] for r in INSIDE})
        self.assertTrue(all(app.KATIE_SCOPE_START <= app.row_charge_date(r) <= app.KATIE_SCOPE_END for r in kept))
        self.assertEqual(len(self.scope_employee()['rows']), len(self.scope_rows))
        other_id = {'id': 'someone-else', 'name': 'Scope Example', 'rows': self.scope_rows}
        self.assertEqual(len(app.review_rows(self.doc, other_id)), len(self.scope_rows))
        later_emp = self.later['employees'][0]
        self.assertEqual(len(app.review_rows(self.later, later_emp)), 1)

    def test_review_page_keeps_source_and_hides_outside_dates(self):
        before = hashlib.sha256(self.stmt_path.read_bytes()).hexdigest()
        page = self.client.get(f'/v/{self.scope_token}').get_data(as_text=True)
        self.assertEqual(hashlib.sha256(self.stmt_path.read_bytes()).hexdigest(), before)
        loaded, emp = app.resolve(self.scope_token)
        self.assertEqual(emp['id'], 'katie-henderson')
        self.assertEqual(len(emp['rows']), len(self.scope_rows))
        self.assertEqual(app.make_token(STMT, 'katie-henderson'), self.scope_token)
        self.assertIn('Charges dated 09/23/2026 through 10/21/2026.', page)
        self.assertIn('FICTIONAL INSIDE START', page)
        self.assertIn('FICTIONAL INSIDE MID', page)
        self.assertIn('FICTIONAL INSIDE END', page)
        self.assertNotIn('FICTIONAL BEFORE START', page)
        self.assertNotIn('FICTIONAL DAY BEFORE', page)
        self.assertNotIn('FICTIONAL AFTER END', page)
        kept = app.review_rows(loaded, emp)
        self.assertEqual(sum(r['amount'] for r in kept), 7.00)
        self.assertIn(app.money(sum(r['amount'] for r in kept)), page)
        other = self.client.get(f'/v/{self.other_token}').get_data(as_text=True)
        self.assertIn('OTHER CARD SEPTEMBER', other)
        self.assertIn('09/21/2026', other)
        self.assertNotIn('Charges dated 09/23/2026 through 10/21/2026.', other)
        self.assertEqual(app.make_token(STMT, 'other-cardholder'), self.other_token)
        later = self.client.get(f'/v/{self.later_token}').get_data(as_text=True)
        self.assertIn('LATER STATEMENT SEPTEMBER', later)
        self.assertNotIn('Charges dated 09/23/2026 through 10/21/2026.', later)

    def test_packet_and_summary_use_the_same_rows_as_the_review(self):
        loaded = app.load_statements()[STMT]
        emp = next(e for e in loaded['employees'] if e['id'] == 'katie-henderson')
        other = next(e for e in loaded['employees'] if e['id'] == 'other-cardholder')
        rows = app.sorted_rows(app.review_rows(loaded, emp))
        lines = app.employee_packet_lines(emp, rows, '2026-11-08T08:00:00-06:00', '', 'October 2026')
        text = '\n'.join(lines)
        pdf = summary.pdf_from_lines(lines)
        sheet = zipfile.ZipFile(__import__('io').BytesIO(
            summary.table_xlsx(app.employee_packet_rows(emp, rows, '2026-11-08T08:00:00-06:00', '', 'October 2026'))
        )).read('xl/worksheets/sheet1.xml').decode()
        for blob in (text, pdf.decode('latin-1', 'replace'), sheet):
            self.assertIn('FICTIONAL INSIDE START', blob)
            self.assertIn('FICTIONAL INSIDE END', blob)
            self.assertNotIn('FICTIONAL BEFORE START', blob)
            self.assertNotIn('FICTIONAL DAY BEFORE', blob)
            self.assertNotIn('FICTIONAL AFTER END', blob)
        other_lines = '\n'.join(app.employee_packet_lines(
            other, app.sorted_rows(app.review_rows(loaded, other)), '2026-11-08T08:00:00-06:00', '', 'October 2026'))
        self.assertIn('OTHER CARD SEPTEMBER', other_lines)
        built = summary.build_summary(app.statement_for_review(loaded), lambda stmt, emp_id: ({}, None, '', {}))
        by_id = {p['id']: p for p in built['people']}
        self.assertEqual(by_id['katie-henderson']['total_cents'], summary.to_cents(sum(r['amount'] for r in rows)))
        self.assertEqual(by_id['other-cardholder']['total_cents'], summary.to_cents(8.00))
        self.assertEqual(hashlib.sha256(self.stmt_path.read_bytes()).hexdigest(),
                         hashlib.sha256(self._stmt_bytes).hexdigest())

    def test_admin_shows_scoped_count_without_rewriting_the_statement(self):
        body = self.client.get('/admin', auth=('Chris', 'local-admin')).get_data(as_text=True)
        self.assertIn(f'{len(INSIDE)} of {len(self.scope_rows)}', body)
        self.assertIn('Other Cardholder', body)
        self.assertEqual(self.stmt_path.read_bytes(), self._stmt_bytes)
        self.assertEqual(self.later_path.read_bytes(), self._later_bytes)


if __name__ == '__main__':
    unittest.main()
