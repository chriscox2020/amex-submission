#!/usr/bin/env python3
"""Filing happens once per employee submit. The period label is the stored date string.

Synthetic rows only. No network, no live statement, no account lookup.
"""
import json, os, pathlib, tempfile, unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
TMP = tempfile.mkdtemp(prefix='amex-filing-period-')
os.environ['DATA_DIR'] = TMP
os.environ['TOKEN_SECRET'] = 'local-test-secret'
os.environ['ADMIN_PASSWORD'] = 'local-admin'
os.environ['EMAIL_MODE'] = 'dryrun'
os.environ['RECORD_TO'] = 'record@example.com'
os.environ['RECORD_CC'] = ''
os.environ['PUBLIC_BASE'] = 'https://amex.coxresearch.com'
os.environ['MAIL_FROM'] = 'Chris AI <littledude@coxresearch.com>'
for key in ('DROPBOX_APP_KEY', 'DROPBOX_APP_SECRET', 'DROPBOX_REFRESH_TOKEN'):
    os.environ.pop(key, None)

import sys
sys.path.insert(0, str(ROOT))
import app
import summary

STMT = '2026-04-22'
APP_DROPBOX_URLS = {
    'https://api.dropboxapi.com/oauth2/token',
    'https://content.dropboxapi.com/2/files/upload',
}


def write_doc(doc):
    folder = pathlib.Path(app.DATA_DIR, 'statements')
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (doc['stmt'] + '.json')
    prior = path.read_bytes() if path.exists() else None
    path.write_text(json.dumps(doc))
    return path, prior


class FilingPeriodTest(unittest.TestCase):
    def setUp(self):
        self.doc = {
            'stmt': STMT,
            'label': 'Apr 22, 2026 statement',
            'employees': [
                {
                    'id': 'ada-example',
                    'name': 'Ada Example',
                    'first': 'Ada',
                    'email': 'ada@example.com',
                    'rows': [
                        {'id': 1, 'date': '03/15/2026', 'category': 'Supplies',
                         'company': 'FICTIONAL MARKET', 'amount': 1.25},
                        {'id': 2, 'date': '04/22/2026', 'category': 'Supplies',
                         'company': 'FICTIONAL FUEL', 'amount': 2.50},
                    ],
                },
                {
                    'id': 'ben-example',
                    'name': 'Ben Example',
                    'first': 'Ben',
                    'email': 'ben@example.com',
                    'rows': [
                        {'id': 1, 'date': '04/02/2026', 'category': 'Travel',
                         'company': 'FICTIONAL TICKET', 'amount': 4.00},
                    ],
                },
            ],
        }
        self.path, self._prior = write_doc(self.doc)
        log_path = pathlib.Path(app.LOG)
        self._log_prior = log_path.read_bytes() if log_path.exists() else None
        self.calls = []
        self.sent = []
        self._call = app.dropbox_call
        self._send = app.send_prepared
        app.dropbox_call = self._fake_dropbox
        app.send_prepared = self._fake_send
        self.client = app.app.test_client()
        self.ada = app.make_token(STMT, 'ada-example')

    def tearDown(self):
        app.dropbox_call = self._call
        app.send_prepared = self._send
        for key in ('DROPBOX_APP_KEY', 'DROPBOX_APP_SECRET', 'DROPBOX_REFRESH_TOKEN'):
            os.environ.pop(key, None)
        if self._prior is None:
            if self.path.exists():
                self.path.unlink()
        else:
            self.path.write_bytes(self._prior)
        log_path = pathlib.Path(app.LOG)
        if self._log_prior is None:
            if log_path.exists():
                log_path.unlink()
        else:
            log_path.write_bytes(self._log_prior)

    def _fake_dropbox(self, url, data, headers):
        self.calls.append((url, data, headers))
        if url.endswith('/oauth2/token'):
            return b'{"access_token":"short-lived","expires_in":14400}'
        return b'{}'

    def _fake_send(self, msg):
        self.sent.append(str(msg['Subject']))
        return 'mocked'

    def enable_dropbox(self):
        os.environ['DROPBOX_APP_KEY'] = 'app-key'
        os.environ['DROPBOX_APP_SECRET'] = 'app-secret'
        os.environ['DROPBOX_REFRESH_TOKEN'] = 'refresh-token'

    def upload_paths(self):
        found = []
        for url, _data, headers in self.calls:
            if url.endswith('/files/upload'):
                found.append(json.loads(headers['Dropbox-API-Arg'])['path'])
        return found

    def test_employee_submit_files_once_through_the_app_client(self):
        self.enable_dropbox()
        first = self.client.post(f'/v/{self.ada}/submit')
        self.assertEqual(first.status_code, 303)
        urls = [url for url, _data, _headers in self.calls]
        self.assertEqual(set(urls), APP_DROPBOX_URLS)
        self.assertNotIn('https://api.dropboxapi.com/2/users/get_current_account', urls)
        paths = self.upload_paths()
        self.assertEqual(len(paths), 2)
        self.assertTrue(all(path.startswith(app.DROPBOX_ROOT + '/April 22/Ada Example/') for path in paths))
        self.assertEqual(sorted(path.rsplit('.', 1)[-1] for path in paths), ['pdf', 'xlsx'])
        filed = [r for r in app.read_log() if r['type'] == 'dropbox_file']
        self.assertEqual(len(filed), 1)
        self.assertEqual(filed[0]['status'], 'ok')
        submits = [r for r in app.read_log() if r['type'] == 'submit']
        self.assertEqual(len(submits), 1)
        subjects = list(self.sent)
        self.assertTrue(subjects)
        self.assertFalse(any('Combined' in subject or 'QB SUBMIT' in subject for subject in subjects))
        self.calls.clear()
        self.sent.clear()
        second = self.client.post(f'/v/{self.ada}/submit')
        self.assertEqual(second.status_code, 303)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.sent, [])
        self.assertEqual(len([r for r in app.read_log() if r['type'] == 'submit']), 1)
        self.assertEqual(len([r for r in app.read_log() if r['type'] == 'dropbox_file']), 1)
        before = len(self.calls)
        app.maybe_file_final_chart(app.load_statements()[STMT], submits[0]['ts'])
        self.assertEqual(len(self.calls), before)
        self.assertFalse(app.statement_all_submitted(app.load_statements()[STMT]))

    def test_period_label_is_the_stored_date_not_the_charge_dates(self):
        labeled = summary.statement_period(self.doc)
        self.assertEqual(labeled['close_date'], '2026-04-22')
        self.assertEqual(labeled['month_label'], 'April 2026')
        self.assertNotIn('March', labeled['month_label'])
        overridden = summary.statement_period({'stmt': STMT, 'close_date': '2026-05-01', 'employees': self.doc['employees']})
        self.assertEqual(overridden['close_date'], '2026-05-01')
        self.assertEqual(overridden['month_label'], 'May 2026')
        unknown = summary.statement_period({
            'stmt': 'not-a-date',
            'employees': [{'id': 'ada-example', 'rows': [{'id': 1, 'date': '09/23/2026', 'amount': 9}]}],
        })
        self.assertEqual(unknown['ym'], 'unknown')
        self.assertEqual(unknown['close_date'], 'not-a-date')
        self.assertNotIn('September', unknown['month_label'])
        self.assertEqual(app.KATIE_SCOPE_STMT, '2026-10-22')
        self.assertEqual(app.KATIE_SCOPE_EMP, 'katie-henderson')
        self.assertEqual(app.KATIE_SCOPE_START.isoformat(), '2026-09-23')
        self.assertEqual(app.KATIE_SCOPE_END.isoformat(), '2026-10-21')

    def test_completeness_follows_the_statement_not_a_fixed_roster(self):
        self.assertFalse(app.statement_all_submitted(self.doc))
        app.append({'type': 'submit', 'stmt': STMT, 'emp_id': 'ada-example', 'ts': '2026-04-24T08:00:00-05:00',
                    'flagged': [], 'approved': [1, 2]})
        self.assertFalse(app.statement_all_submitted(self.doc))
        app.append({'type': 'submit', 'stmt': STMT, 'emp_id': 'ben-example', 'ts': '2026-04-24T08:05:00-05:00',
                    'flagged': [], 'approved': [1]})
        self.assertTrue(app.statement_all_submitted(self.doc))
        self.assertFalse(app.statement_all_submitted({'stmt': STMT, 'employees': []}))

    def test_app_source_does_not_call_the_account_identity_endpoint(self):
        text = (ROOT / 'app.py').read_text()
        self.assertIn('https://content.dropboxapi.com/2/files/upload', text)
        self.assertNotIn('users/get_current_account', text)
        self.assertNotIn('account_info.read', text)


if __name__ == '__main__':
    unittest.main()
