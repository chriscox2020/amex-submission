#!/usr/bin/env python3
"""Generic monthly loader. Adds one employee's charges to a statement file.
CSV columns: date,category,company,amount   (date MM/DD/YYYY)
  python tools/load_csv.py --stmt 2026-10-22 --label "Oct 22, 2026 statement" \
     --emp-id nick-fluharty --name "Nicholas Fluharty" --first Nick \
     --email nick@coxresearch.com charges.csv
Then commit statements/<stmt>.json (or upload it: see README, /admin/upload)."""
import argparse, csv, json, os
p = argparse.ArgumentParser()
for a in ('--stmt', '--emp-id', '--name', '--first'): p.add_argument(a, required=True)
p.add_argument('--label', default=''); p.add_argument('--due', default='Due by the first')
p.add_argument('--email', default=''); p.add_argument('csv')
a = p.parse_args()
out = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'statements', a.stmt + '.json')
doc = json.load(open(out)) if os.path.exists(out) else {'stmt': a.stmt, 'label': a.label or a.stmt, 'due': a.due, 'employees': []}
rows = [{'id': i + 1, 'date': r['date'].strip(), 'category': r['category'].strip(), 'company': r['company'].strip(),
         'amount': round(float(r['amount'].replace('$', '').replace(',', '')), 2)}
        for i, r in enumerate(csv.DictReader(open(a.csv)))]
doc['employees'] = [e for e in doc['employees'] if e['id'] != a.emp_id] + [
    {'id': a.emp_id, 'name': a.name, 'first': a.first, 'email': a.email, 'rows': rows}]
json.dump(doc, open(out, 'w'), indent=1)
print(a.emp_id, len(rows), round(sum(r['amount'] for r in rows), 2), os.path.abspath(out))
