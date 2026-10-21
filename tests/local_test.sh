#!/usr/bin/env bash
# Local end-to-end test: real gunicorn, dry-run email, temp disk dir.
set -euo pipefail
cd "$(dirname "$0")/.."
export DATA_DIR=$PWD/local-data TOKEN_SECRET=local-test-secret ADMIN_PASSWORD=local-admin \
       EMAIL_MODE=dryrun RECORD_CC=chris@coxresearch.com PUBLIC_BASE=http://127.0.0.1:10077 PORT=10077
rm -rf "$DATA_DIR"; mkdir -p "$DATA_DIR"
.venv/bin/gunicorn app:app --workers 1 --threads 8 --bind 127.0.0.1:$PORT --daemon --pid "$DATA_DIR/g.pid" --error-logfile "$DATA_DIR/g.err"
sleep 1.5
B=http://127.0.0.1:$PORT
curl -fsS $B/healthz; echo " <- healthz"
LINK=$(curl -fsS -u x:local-admin $B/admin/links.json | .venv/bin/python -c "import sys,json;print(json.load(sys.stdin)['2026-09-22']['chris-cox']['url'])")
T=${LINK##*/}; echo "token link: $LINK"
echo "bad token -> $(curl -s -o /dev/null -w '%{http_code}' $B/v/AAAAAAAAAAAAAAAAAAAAAAAA)"
echo "admin no pw -> $(curl -s -o /dev/null -w '%{http_code}' $B/admin)"
curl -fsS $B/v/$T -o "$DATA_DIR/chris.html"
.venv/bin/python - << 'PY'
import os
from pathlib import Path
p = Path(os.environ['DATA_DIR'], 'chris.html').read_text()
low = p.lower()
assert "class='banner'>American Express Charges" in p, 'missing banner'
assert '>Chris Cox</h1>' in p
assert p.count("class='fb'") == 72, p.count("class='fb'")
assert '$17,048.40' in p and '&minus;$24.31' in p and '$17,024.09' in p
assert 'Total charges' in p and '>Credits</td>' in p and 'Net Total' in p
assert 'dry run' not in low and 'preview' not in low
assert 'button,.pop,.dd{display:none!important}' in p
assert '>Note/Reason</b>' in p and '>Print</button>' in p
print('chris page: banner, 72 flags, totals, credit, print css')
PY
ALICE=$(curl -fsS -u x:local-admin $B/admin/links.json | .venv/bin/python -c "import sys,json;d=json.load(sys.stdin)['2026-09-22']['alice-cox'];assert d['email']=='chris@coxresearch.com',d;print(d['url'])")
echo "alice link (send-to chris@coxresearch.com): $ALICE"
curl -fsS "$ALICE" -o "$DATA_DIR/alice.html"
.venv/bin/python - << 'PY'
import os
from pathlib import Path
p = Path(os.environ['DATA_DIR'], 'alice.html').read_text()
low = p.lower()
assert "class='banner'>American Express Charges" in p
assert '>Alice E Cox</h1>' in p
assert p.count("class='fb'") == 9, p.count("class='fb'")
assert '$480.62' in p
assert 'dry run' not in low and 'preview' not in low
print('alice page: banner, 9 charges, $480.62, no preview label')
PY
curl -fsS -X POST -H 'Accept: application/json' --data-urlencode 'note=Not a company charge' $B/v/$T/flag/1; echo
for r in 5 9; do curl -fsS -X POST -H 'Accept: application/json' $B/v/$T/flag/$r; echo; done
curl -fsS -X POST -H 'Accept: application/json' $B/v/$T/flag/9; echo " <- row 9 unflagged (toggle)"
curl -fsS $B/v/$T | grep -q 'Note: Not a company charge'
curl -fsS $B/v/$T | grep -q 'ref pending'
curl -fsS -X POST -H 'Accept: application/json' --data-urlencode 'comment=wrong category' $B/v/$T/comment; echo
curl -fsS -X POST -H 'Accept: application/json' --data-urlencode 'category=Travel' $B/v/$T/category/5; echo
echo "page shows the flag note and ref pending"
curl -s -o /dev/null -w 'submit -> %{http_code}\n' -X POST $B/v/$T/submit
curl -s -o /dev/null -w 'double submit -> %{http_code}\n' -X POST $B/v/$T/submit
echo "flag after submit -> $(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Accept: application/json' $B/v/$T/flag/2)"
curl -fsS $B/v/$T | grep -o "Submitted [^<]*</b>[^<]*" | head -1
echo "--- log ($DATA_DIR/log.jsonl): $(wc -l < $DATA_DIR/log.jsonl) lines, submits=$(grep -c '"type": "submit"' $DATA_DIR/log.jsonl)"
grep -E '"type": "(submit|email)"' $DATA_DIR/log.jsonl | .venv/bin/python -c "
import sys,json
for l in sys.stdin:
    r=json.loads(l); print({k:r[k] for k in r if k in ('type','ts','emp','flagged','flagged_total','approved_total','to','cc','status')} if r['type']=='email' else {k:(len(v) if k=='approved' else v) for k,v in r.items() if k in ('type','ts','emp','flagged','approved','flagged_total','approved_total')})"
echo "--- dry-run email:"; ls $DATA_DIR/outbox; .venv/bin/python -c "import email,glob;m=email.message_from_bytes(open(glob.glob('$DATA_DIR/outbox/*.eml')[0],'rb').read());[print(h+':',m[h]) for h in ('Subject','From','To','Cc')]"
.venv/bin/python - <<'PY'
import email, glob, os
p = glob.glob(os.environ['DATA_DIR'] + '/outbox/*.eml')[0]
m = email.message_from_bytes(open(p, 'rb').read())
text = html = ''
for part in m.walk():
    if part.get_content_type() == 'text/plain':
        text = part.get_payload(decode=True).decode()
    elif part.get_content_type() == 'text/html':
        html = part.get_payload(decode=True).decode()
print(text)
if 'Note: Not a company charge' not in text:
    raise SystemExit('missing note in text email')
if 'Note: Not a company charge' not in html:
    raise SystemExit('missing note in html email')
if 'ref pending' not in text:
    raise SystemExit('missing ref pending in text email')
if '\nFlagged charges: wrong category\n' not in text:
    raise SystemExit('missing overall comment in text email')
if text.index('Flagged charges: wrong category') > text.index('Approved'):
    raise SystemExit('overall comment is not after the flag lines')
if m['To'] != 'littledude@mail.grokbot.com':
    raise SystemExit('record email To is %r' % (m['To'],))
print('email shows the flag note, ref pending, and overall comment; To littledude@mail.grokbot.com')
PY
echo "--- admin CSV:"; curl -fsS -u x:local-admin $B/admin/log.csv | head -3 | cut -c1-160
curl -fsS -u x:local-admin $B/admin | grep -o "Submitted [^<]*" | head -1
echo "server still running pid $(cat $DATA_DIR/g.pid) on $B (stop: kill \$(cat $DATA_DIR/g.pid))"
