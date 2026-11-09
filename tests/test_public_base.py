#!/usr/bin/env python3
"""Link-host guard and small failure-path tests. Fictional charges only."""
import base64, hashlib, hmac, io, json, os, pathlib, shutil, smtplib, socket, subprocess, sys, tempfile, unittest
from email.message import EmailMessage
from unittest import mock
import urllib.error

ROOT = pathlib.Path(__file__).resolve().parents[1]
TMP = tempfile.mkdtemp(prefix='amex-public-base-')
os.environ['DATA_DIR'] = TMP
os.environ['TOKEN_SECRET'] = 'local-test-secret'
os.environ['ADMIN_PASSWORD'] = 'local-admin'
os.environ['EMAIL_MODE'] = 'dryrun'
os.environ['RECORD_TO'] = 'record@example.com'
os.environ['RECORD_CC'] = ''
os.environ['PUBLIC_BASE'] = 'http://127.0.0.1:10077'
os.environ['MAIL_FROM'] = 'Test <mailer@example.com>'
os.environ.pop('RENDER_EXTERNAL_URL', None)
for key in ('DROPBOX_APP_KEY', 'DROPBOX_APP_SECRET', 'DROPBOX_REFRESH_TOKEN', 'SMTP_PASSWORD', 'RESEND_API_KEY'):
    os.environ.pop(key, None)

sys.path.insert(0, str(ROOT))
import app

KNOWN = 'https://amex-submission.onrender.com'
STMT = '2099-01-24'
DOC = {
    'stmt': STMT,
    'label': 'Jan 24, 2099 statement',
    'due': 'Due by the first',
    'employees': [{
        'id': 'sample-employee',
        'name': 'Sample Employee',
        'first': 'Sample',
        'email': 'sample@example.com',
        'rows': [
            {'id': 1, 'date': '01/02/2099', 'category': 'Office supplies', 'company': 'EXAMPLE STORE', 'amount': 12.34},
        ],
    }],
}


def independent_token(stmt, emp_id):
    mac = hmac.new(b'local-test-secret', f'{stmt}|{emp_id}'.encode(), hashlib.sha256).digest()[:18]
    return base64.urlsafe_b64encode(mac).decode().rstrip('=')


def message():
    msg = EmailMessage()
    msg['Subject'] = 'Fictional record'
    msg['From'] = 'Test <mailer@example.com>'
    msg['To'] = 'record@example.com'
    msg.set_content('Fictional body')
    return msg


class PublicBaseTest(unittest.TestCase):
    def setUp(self):
        self._env = {k: os.environ.get(k) for k in (
            'PUBLIC_BASE', 'RENDER_EXTERNAL_URL', 'EMAIL_MODE', 'SMTP_USER', 'SMTP_PASSWORD', 'RESEND_API_KEY')}
        self._email_mode = app.EMAIL_MODE
        self._delays = (app.DROPBOX_RETRY_DELAYS, app.SMTP_RETRY_DELAYS, app.HTTP_RETRY_DELAYS, app.DNS_LOOKUP_TIMEOUT)
        app._PUBLIC_BASE_LOGGED.clear()
        if os.path.exists(app.LOG):
            os.remove(app.LOG)
        self.client = app.app.test_client()

    def tearDown(self):
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        app.EMAIL_MODE = self._email_mode
        app.DROPBOX_RETRY_DELAYS, app.SMTP_RETRY_DELAYS, app.HTTP_RETRY_DELAYS, app.DNS_LOOKUP_TIMEOUT = self._delays

    def write_doc(self, doc=None):
        folder = pathlib.Path(app.DATA_DIR, 'statements')
        folder.mkdir(parents=True, exist_ok=True)
        body = doc or DOC
        (folder / (body['stmt'] + '.json')).write_text(json.dumps(body))

    def test_ip_literal_is_kept(self):
        os.environ['PUBLIC_BASE'] = 'http://127.0.0.1:10077'
        self.assertEqual(app.public_base(), 'http://127.0.0.1:10077')

    def test_default_is_the_service_url(self):
        os.environ.pop('PUBLIC_BASE', None)
        os.environ.pop('RENDER_EXTERNAL_URL', None)
        with mock.patch('app._host_resolves', return_value=True):
            self.assertEqual(app.public_base(), KNOWN)

    def test_render_external_url_used_when_public_base_unset(self):
        os.environ.pop('PUBLIC_BASE', None)
        os.environ['RENDER_EXTERNAL_URL'] = 'https://amex-submission.onrender.com/extra'
        with mock.patch('app._host_resolves', return_value=True):
            self.assertEqual(app.public_base(), KNOWN)

    def test_render_external_url_other_host_is_kept_when_it_resolves(self):
        os.environ.pop('PUBLIC_BASE', None)
        os.environ['RENDER_EXTERNAL_URL'] = 'https://charges.example.com'
        with mock.patch('app._host_resolves', return_value=True):
            self.assertEqual(app.public_base(), 'https://charges.example.com')

    def test_dead_host_is_replaced_and_logged_once(self):
        os.environ['PUBLIC_BASE'] = 'https://amex.coxresearch.com'
        os.environ['RENDER_EXTERNAL_URL'] = 'https://amex.coxresearch.com'
        with self.assertLogs('app', level='ERROR') as captured, mock.patch('app._host_resolves', return_value=True):
            self.assertEqual(app.public_base(), KNOWN)
            self.assertEqual(app.public_base(), KNOWN)
        lines = [line for line in captured.output if 'PUBLIC_BASE rejected' in line]
        self.assertEqual(len(lines), 1)
        self.assertIn('amex.coxresearch.com', lines[0])
        self.assertIn(KNOWN, lines[0])

    def test_unresolvable_host_is_replaced(self):
        os.environ['PUBLIC_BASE'] = 'https://charges.example.invalid/path'
        os.environ.pop('RENDER_EXTERNAL_URL', None)

        def resolves(host):
            return host == 'amex-submission.onrender.com'

        with mock.patch('app._host_resolves', side_effect=resolves):
            self.assertEqual(app.public_base(), KNOWN)

    def test_resolving_host_is_kept_without_a_path(self):
        os.environ['PUBLIC_BASE'] = 'https://charges.example.com/ignored'
        with mock.patch('app._host_resolves', return_value=True):
            self.assertEqual(app.public_base(), 'https://charges.example.com')

    def test_unusable_public_base_falls_back(self):
        os.environ['PUBLIC_BASE'] = 'ftp://files.example.com/charges'
        os.environ.pop('RENDER_EXTERNAL_URL', None)
        with mock.patch('app._host_resolves', return_value=True):
            self.assertEqual(app.public_base(), KNOWN)

    def test_token_does_not_change_when_the_link_host_changes(self):
        first = app.make_token(STMT, 'sample-employee')
        os.environ['PUBLIC_BASE'] = 'https://amex.coxresearch.com'
        with mock.patch('app._host_resolves', return_value=True):
            self.assertEqual(app.public_base(), KNOWN)
        second = app.make_token(STMT, 'sample-employee')
        self.assertEqual(first, second)
        self.assertEqual(first, independent_token(STMT, 'sample-employee'))
        self.assertNotIn('http', first)

    def test_dns_failure_and_timeout_do_not_resolve(self):
        with mock.patch('app.socket.getaddrinfo', side_effect=socket.gaierror):
            self.assertFalse(app._host_resolves('missing.example'))
        app.DNS_LOOKUP_TIMEOUT = 0.05
        with mock.patch('app.socket.getaddrinfo', side_effect=lambda *a, **k: __import__('time').sleep(2)):
            self.assertFalse(app._host_resolves('slow.example'))
        self.assertTrue(app._host_resolves('127.0.0.1'))
        self.assertTrue(app._host_resolves('localhost'))

    def test_admin_links_and_submit_record_omit_the_dead_host(self):
        self.write_doc()
        os.environ['PUBLIC_BASE'] = 'https://User:secret@amex.coxresearch.com'
        with mock.patch('app._host_resolves', return_value=True):
            res = self.client.get('/admin/links.json', auth=('Chris', 'local-admin'))
            self.assertEqual(res.status_code, 200)
            url = res.get_json()[STMT]['sample-employee']['url']
            self.assertEqual(url, f'{KNOWN}/v/{independent_token(STMT, "sample-employee")}')
            self.assertNotIn('amex.coxresearch.com', url)
            posted = self.client.post('/v/' + independent_token(STMT, 'sample-employee') + '/submit')
        self.assertEqual(posted.status_code, 303)
        record = next(row for row in app.read_log() if row['type'] == 'email')
        raw = pathlib.Path(record['status'].split(':', 1)[1]).read_bytes()
        self.assertNotIn(b'amex.coxresearch.com', raw)
        self.assertIn(KNOWN.encode(), raw)
        self.assertTrue(any(row['type'] == 'submit' for row in app.read_log()))

    def test_make_email_writes_the_working_url(self):
        self.write_doc()
        out = ROOT / 'out' / STMT
        shutil.rmtree(out, ignore_errors=True)
        env = os.environ.copy()
        env['DATA_DIR'] = TMP
        env['TOKEN_SECRET'] = 'local-test-secret'
        env['PUBLIC_BASE'] = 'https://amex.coxresearch.com'
        env.pop('RENDER_EXTERNAL_URL', None)
        try:
            proc = subprocess.run(
                [sys.executable, 'tools/make_email.py', STMT, 'sample-employee'],
                cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn('PUBLIC_BASE rejected', proc.stderr)
            self.assertNotIn('amex.coxresearch.com/v/', proc.stdout)
            page = (out / 'sample-employee.html').read_text()
            self.assertNotIn('amex.coxresearch.com', page)
            self.assertIn(f'{KNOWN}/v/{independent_token(STMT, "sample-employee")}', page)
            self.assertEqual(page.count(f'{KNOWN}/v/'), 1)
            self.assertIn('Open my charges', page)
        finally:
            shutil.rmtree(out, ignore_errors=True)

    def test_upload_rejects_a_bad_body_and_accepts_fictional_json(self):
        bad = self.client.post('/admin/upload', data='not-json', content_type='application/json',
                               auth=('Chris', 'local-admin'))
        self.assertEqual(bad.status_code, 400)
        missing = self.client.post('/admin/upload', json={'employees': []}, auth=('Chris', 'local-admin'))
        self.assertEqual(missing.status_code, 400)
        escaped = self.client.post('/admin/upload', json={'stmt': '../outside', 'employees': []},
                                   auth=('Chris', 'local-admin'))
        self.assertEqual(escaped.status_code, 400)
        self.assertFalse((pathlib.Path(app.DATA_DIR) / 'outside.json').exists())
        ok = self.client.post('/admin/upload', json=DOC, auth=('Chris', 'local-admin'))
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.get_json()['stmt'], STMT)
        stored = json.loads((pathlib.Path(app.DATA_DIR) / 'statements' / (STMT + '.json')).read_text())
        self.assertEqual(stored['employees'][0]['name'], 'Sample Employee')
        self.assertGreaterEqual(stored['bundle_rev'], 1)

    def test_unreadable_statement_and_log_line_are_skipped(self):
        self.write_doc()
        folder = pathlib.Path(app.DATA_DIR, 'statements')
        (folder / 'broken.json').write_text('{')
        with self.assertLogs('app', level='ERROR') as captured:
            loaded = app.load_statements()
        self.assertTrue(STMT in loaded)
        self.assertTrue(any('unreadable statement' in line for line in captured.output))
        pathlib.Path(app.LOG).write_text('not-json\n')
        with self.assertLogs('app', level='WARNING') as captured:
            self.assertEqual(app.read_log(), [])
        self.assertTrue(any('unreadable log line' in line for line in captured.output))
        self.assertEqual(app.read_log(), [])

    def test_upload_replaces_an_unreadable_statement_file(self):
        folder = pathlib.Path(app.DATA_DIR, 'statements')
        folder.mkdir(parents=True, exist_ok=True)
        (folder / (STMT + '.json')).write_text('{')
        ok = self.client.post('/admin/upload', json=DOC, auth=('Chris', 'local-admin'))
        self.assertEqual(ok.status_code, 200)
        stored = json.loads((folder / (STMT + '.json')).read_text())
        self.assertEqual(stored['employees'][0]['rows'][0]['company'], 'EXAMPLE STORE')

    def test_dropbox_retries_transport_and_stops_on_client_error(self):
        app.DROPBOX_RETRY_DELAYS = (0, 0, 0)
        attempts = {'n': 0}

        class Resp:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self):
                return b'{"access_token":"short-lived"}'

        def flaky(req, timeout=None):
            attempts['n'] += 1
            if attempts['n'] < 3:
                raise urllib.error.URLError('reset')
            return Resp()

        with mock.patch('urllib.request.urlopen', flaky):
            body = app.dropbox_call('https://api.dropboxapi.com/oauth2/token', b'grant_type=refresh_token', {})
        self.assertEqual(body, b'{"access_token":"short-lived"}')
        self.assertEqual(attempts['n'], 3)

        attempts['n'] = 0

        def client_error(req, timeout=None):
            attempts['n'] += 1
            raise urllib.error.HTTPError('https://api.dropboxapi.com/oauth2/token', 400, 'bad', None, io.BytesIO(b'no'))

        with mock.patch('urllib.request.urlopen', client_error):
            with self.assertRaises(urllib.error.HTTPError):
                app.dropbox_call('https://api.dropboxapi.com/oauth2/token', b'a=1', {})
        self.assertEqual(attempts['n'], 1)

        attempts['n'] = 0

        def then_ok(req, timeout=None):
            attempts['n'] += 1
            if attempts['n'] == 1:
                raise urllib.error.HTTPError('https://content.dropboxapi.com/2/files/upload', 503, 'busy', None, io.BytesIO(b'later'))
            return Resp()

        with mock.patch('urllib.request.urlopen', then_ok):
            self.assertEqual(app.dropbox_call('https://content.dropboxapi.com/2/files/upload', b'pdf', {}).startswith(b'{'), True)
        self.assertEqual(attempts['n'], 2)

    def test_smtp_retries_connection_but_not_auth_or_send(self):
        app.SMTP_RETRY_DELAYS = (0, 0, 0)
        app.EMAIL_MODE = 'smtp'
        os.environ['SMTP_USER'] = 'mailer@example.com'
        os.environ['SMTP_PASSWORD'] = 'test-only-password'
        calls = {'n': 0}
        sent = []

        class FakeSMTP:
            def __init__(self, host, port, timeout=None):
                calls['n'] += 1
                if calls['n'] == 1:
                    raise OSError('temporary')
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def starttls(self, context=None):
                pass
            def login(self, user, password):
                self.user = user
            def send_message(self, msg):
                sent.append(msg)

        with mock.patch('app.smtplib.SMTP', FakeSMTP):
            self.assertEqual(app.send_prepared(message()), 'sent-smtp')
        self.assertEqual(calls['n'], 2)
        self.assertEqual(len(sent), 1)

        calls['n'] = 0

        class AuthSMTP(FakeSMTP):
            def __init__(self, host, port, timeout=None):
                calls['n'] += 1
            def login(self, user, password):
                raise smtplib.SMTPAuthenticationError(535, b'no')

        with mock.patch('app.smtplib.SMTP', AuthSMTP):
            with self.assertRaises(smtplib.SMTPAuthenticationError):
                app.send_prepared(message())
        self.assertEqual(calls['n'], 1)

        calls['n'] = 0

        class SendSMTP(FakeSMTP):
            def __init__(self, host, port, timeout=None):
                calls['n'] += 1
            def send_message(self, msg):
                raise smtplib.SMTPServerDisconnected('lost during data')

        with mock.patch('app.smtplib.SMTP', SendSMTP):
            with self.assertRaises(smtplib.SMTPServerDisconnected):
                app.send_prepared(message())
        self.assertEqual(calls['n'], 1)

    def test_resend_retries_a_transport_error_once(self):
        app.HTTP_RETRY_DELAYS = (0, 0, 0)
        app.EMAIL_MODE = 'resend'
        os.environ['RESEND_API_KEY'] = 'test-resend-key'
        attempts = {'n': 0}

        class Resp:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self):
                return b'{"id":"fictional"}'

        def flaky(req, timeout=None):
            attempts['n'] += 1
            if attempts['n'] == 1:
                raise urllib.error.URLError('reset')
            return Resp()

        with mock.patch('urllib.request.urlopen', flaky):
            self.assertEqual(app.send_prepared(message()), 'sent-resend')
        self.assertEqual(attempts['n'], 2)


if __name__ == '__main__':
    unittest.main()
