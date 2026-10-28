#!/usr/bin/env python3
"""Dropbox filing is mocked. No network call is made."""
import email, json, os, pathlib, tempfile, unittest, zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
TMP = tempfile.mkdtemp(prefix='amex-dropbox-')
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

STMT = '2026-10-22'
FROZEN = '2026-10-21T09:30:00-05:00'
DOC = {
    'stmt': STMT,
    'label': 'Oct 22, 2026 statement',
    'employees': [{
        'id': 'sample-employee',
        'name': 'Sample Employee',
        'first': 'Sample',
        'email': 'sample@coxresearch.com',
        'rows': [
            {'id': 1, 'date': '09/15/2026', 'category': 'Office supplies', 'company': 'AMAZON MARKETPLACE', 'amount': 10.0},
            {'id': 2, 'date': '09/16/2026', 'category': 'Travel', 'company': 'DELTA AIR LINES', 'amount': 5.0},
        ],
    }],
}


class DropboxFilingTest(unittest.TestCase):
    def setUp(self):
        self._now = app.now
        app.now = lambda: FROZEN
        self._call = app.dropbox_call
        self._send = app.send_prepared
        d = pathlib.Path(app.DATA_DIR, 'statements')
        d.mkdir(parents=True, exist_ok=True)
        (d / (STMT + '.json')).write_text(json.dumps(DOC))
        if os.path.exists(app.LOG):
            os.remove(app.LOG)
        self.client = app.app.test_client()
        self.token = app.make_token(STMT, 'sample-employee')
        self.calls = []

    def tearDown(self):
        app.now = self._now
        app.dropbox_call = self._call
        app.send_prepared = self._send
        for key in ('DROPBOX_APP_KEY', 'DROPBOX_APP_SECRET', 'DROPBOX_REFRESH_TOKEN'):
            os.environ.pop(key, None)

    def enable_dropbox(self):
        os.environ['DROPBOX_APP_KEY'] = 'app-key'
        os.environ['DROPBOX_APP_SECRET'] = 'app-secret'
        os.environ['DROPBOX_REFRESH_TOKEN'] = 'refresh-token'

        def fake(url, data, headers):
            self.calls.append((url, data, headers))
            if url.endswith('/oauth2/token'):
                return b'{"access_token":"short-lived","expires_in":14400}'
            return b'{}'

        app.dropbox_call = fake

    def submit(self):
        self.client.post(f'/v/{self.token}/flag/1', data={'note': 'not my charge'},
                         headers={'Accept': 'application/json'})
        return self.client.post(f'/v/{self.token}/submit')

    def upload_args(self):
        found = []
        for url, data, headers in self.calls:
            if url.endswith('/files/upload'):
                found.append((json.loads(headers['Dropbox-API-Arg']), data))
        return found

    def test_missing_env_logs_failure_and_submit_still_succeeds(self):
        res = self.submit()
        self.assertEqual(res.status_code, 303)
        self.assertEqual(self.calls, [])
        record = next(r for r in app.read_log() if r['type'] == 'email')
        self.assertTrue(str(record['status']).startswith('dryrun:'))
        filed = next(r for r in app.read_log() if r['type'] == 'dropbox_file')
        self.assertIn('Dropbox filing failed', filed['status'])
        msg = email.message_from_bytes(open(record['status'].split(':', 1)[1], 'rb').read())
        text = ''
        for part in msg.walk():
            if part.get_content_type() == 'text/plain':
                text = part.get_payload(decode=True).decode()
        self.assertIn('Dropbox filing failed', text)
        self.assertIn('Note: not my charge', text)
        confirm = next(r for r in app.read_log() if r['type'] == 'submit_confirm_email')
        self.assertTrue(str(confirm['status']).startswith('dryrun:'))

    def test_upload_uses_add_and_places_chart_at_the_charges_root(self):
        self.enable_dropbox()
        res = self.submit()
        self.assertEqual(res.status_code, 303)
        urls = [url for url, _data, _headers in self.calls]
        self.assertIn('https://api.dropboxapi.com/oauth2/token', urls)
        self.assertNotIn('https://api.dropboxapi.com/2/files/create_folder_v2', urls)
        for url in urls:
            self.assertNotIn('delete', url)
            self.assertNotIn('sharing', url)
        uploads = self.upload_args()
        paths = [arg['path'] for arg, _data in uploads]
        base = 'Sample Employee - Amex October 2026 - submitted 2026-10-21 0930 CT'
        employee = "/Chris's AI/American Express charges/October 22/Sample Employee/"
        self.assertIn(employee + base + '.pdf', paths)
        self.assertIn(employee + base + '.xlsx', paths)
        self.assertEqual(len(uploads), 2)
        self.assertFalse(any('Combined' in path or 'all submitted' in path for path in paths))
        for path in paths:
            if path.endswith(base + '.pdf') or path.endswith(base + '.xlsx'):
                self.assertTrue(path.split('/')[-1].startswith('Sample Employee - '))
                self.assertNotIn('/October 22/Combined chart', path)
        for arg, content in uploads:
            self.assertEqual(arg['mode'], {'.tag': 'add'})
            self.assertTrue(arg['autorename'])
            self.assertNotIn('overwrite', json.dumps(arg))
            if arg['path'].endswith(base + '.pdf'):
                self.assertTrue(content.startswith(b'%PDF'))
                self.assertIn(b'AMAZON MARKETPLACE', content)
                self.assertIn(b'Office supplies', content)
                self.assertIn(b'flagged', content)
                self.assertIn(b'not my charge', content)
            if arg['path'].endswith(base + '.xlsx'):
                with zipfile.ZipFile(__import__('io').BytesIO(content)) as zf:
                    sheet = zf.read('xl/worksheets/sheet1.xml').decode()
                self.assertIn('AMAZON MARKETPLACE', sheet)
                self.assertIn('Office supplies', sheet)
                self.assertIn('flagged', sheet)
                self.assertIn('not my charge', sheet)
        record = next(r for r in app.read_log() if r['type'] == 'email')
        msg = email.message_from_bytes(open(record['status'].split(':', 1)[1], 'rb').read())
        text = ''
        for part in msg.walk():
            if part.get_content_type() == 'text/plain':
                text += part.get_payload(decode=True).decode()
        self.assertNotIn('Dropbox filing failed', text)
        filed = next(r for r in app.read_log() if r['type'] == 'dropbox_file')
        self.assertEqual(filed['status'], 'ok')

    def test_upload_failure_does_not_block_submit_or_confirmation(self):
        self.enable_dropbox()
        sent = []
        real_send = app.send_prepared

        def send(msg):
            sent.append(''.join(str(msg['Subject']).splitlines()))
            return 'mocked'

        def boom(url, data, headers):
            self.calls.append((url, data, headers))
            if url.endswith('/files/upload'):
                raise OSError('upload down')
            return b'{"access_token":"short-lived"}'

        app.dropbox_call = boom
        app.send_prepared = send
        try:
            res = self.submit()
        finally:
            app.send_prepared = real_send
        self.assertEqual(res.status_code, 303)
        self.assertEqual(sent[0].startswith('Amex verification SUBMITTED:'), True)
        self.assertIn('Your American Express charges were submitted', sent)
        record = next(r for r in app.read_log() if r['type'] == 'email')
        self.assertEqual(record['status'], 'mocked')
        confirm = next(r for r in app.read_log() if r['type'] == 'submit_confirm_email')
        self.assertEqual(confirm['status'], 'mocked')
        filed = next(r for r in app.read_log() if r['type'] == 'dropbox_file')
        self.assertTrue(filed['status'].startswith('Dropbox filing failed'))

    def test_admin_refile_is_admin_only(self):
        self.enable_dropbox()
        self.assertEqual(self.client.post(f'/admin/dropbox/{STMT}/sample-employee').status_code, 401)
        self.assertEqual(self.client.post(f'/admin/dropbox/{STMT}/sample-employee',
                                          auth=('Pat', 'local-admin')).status_code, 403)
        early = self.client.post(f'/admin/dropbox/{STMT}/sample-employee', auth=('Chris', 'local-admin'))
        self.assertEqual(early.status_code, 409)
        self.submit()
        self.calls.clear()
        again = self.client.post(f'/admin/dropbox/{STMT}/sample-employee', auth=('Chris', 'local-admin'))
        self.assertEqual(again.status_code, 200)
        paths = [arg['path'] for arg, _data in self.upload_args()]
        self.assertTrue(any(path.startswith("/Chris's AI/American Express charges/October 22/Sample Employee/")
                            for path in paths))
        self.assertEqual(len(paths), 2)
        self.assertFalse(any('Combined' in path or 'all submitted' in path for path in paths))

    def test_submit_uploads_exactly_two_files_and_never_a_combined_chart(self):
        self.enable_dropbox()
        sent = []
        real_send = app.send_prepared

        def send(msg):
            sent.append(msg)
            return 'mocked'

        app.send_prepared = send
        two = {
            'stmt': STMT,
            'label': 'Oct 22, 2026 statement',
            'employees': [
                {'id': 'ada', 'name': 'Ada One', 'email': 'ada@example.com', 'rows': [
                    {'id': 1, 'date': '09/01/2026', 'category': 'Supplies', 'company': 'STORE', 'amount': 10.0},
                ]},
                {'id': 'ben', 'name': 'Ben Two', 'email': 'ben@example.com', 'rows': [
                    {'id': 1, 'date': '09/03/2026', 'category': 'Travel', 'company': 'AIR', 'amount': 4.0},
                ]},
            ],
        }
        pathlib.Path(app.DATA_DIR, 'statements', STMT + '.json').write_text(json.dumps(two))
        ada = app.make_token(STMT, 'ada')
        ben = app.make_token(STMT, 'ben')
        try:
            self.assertEqual(self.client.post(f'/v/{ada}/submit').status_code, 303)
            self.assertEqual(len(self.upload_args()), 2)
            self.assertFalse(any('Combined' in arg['path'] or 'all submitted' in arg['path']
                                 for arg, _data in self.upload_args()))
            self.assertFalse(any('Combined Amex chart is ready' in str(m['Subject']) for m in sent))
            self.calls.clear()
            self.assertEqual(self.client.post(f'/v/{ben}/submit').status_code, 303)
            self.assertEqual(len(self.upload_args()), 2)
            self.assertFalse(any('all submitted' in arg['path'] or 'Combined' in arg['path']
                                 for arg, _data in self.upload_args()))
        finally:
            app.send_prepared = real_send
        before = len(self.calls)
        app.maybe_file_final_chart(app.load_statements()[STMT], FROZEN)
        self.assertEqual(len(self.calls), before)
        self.assertEqual([r for r in app.read_log() if r['type'] == 'combined_chart'], [])
        disabled = self.client.post(f'/admin/combined/{STMT}', auth=('Chris', 'local-admin'))
        self.assertEqual(disabled.status_code, 403)
        self.assertEqual(disabled.get_data(as_text=True),
                         'Combined chart disabled; built manually with Chris')
        self.assertEqual(self.client.post(f'/admin/combined/{STMT}').status_code, 401)


if __name__ == '__main__':
    unittest.main()
