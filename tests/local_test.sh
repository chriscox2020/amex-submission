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
P=$(curl -fsS $B/v/$T); echo "page rows: $(grep -o "class='fl" <<<"$P" | wc -l), total shown: $(grep -o 'Total (73 charges)' <<<"$P" | head -1) $(grep -o '\$17,024.09' <<<"$P" | head -1)"
for r in 1 5 9; do curl -fsS -X POST -H 'Accept: application/json' $B/v/$T/flag/$r; echo; done
curl -fsS -X POST -H 'Accept: application/json' $B/v/$T/flag/9; echo " <- row 9 unflagged (toggle)"
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
echo "--- admin CSV:"; curl -fsS -u x:local-admin $B/admin/log.csv | head -3 | cut -c1-160
curl -fsS -u x:local-admin $B/admin | grep -o "Submitted [^<]*" | head -1
echo "server still running pid $(cat $DATA_DIR/g.pid) on $B (stop: kill \$(cat $DATA_DIR/g.pid))"
