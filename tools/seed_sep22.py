#!/usr/bin/env python3
"""Seed loader: builds statements/2026-09-22.json from Chris's approved v5 chart
(73 charges, $17,024.09). Run from render-app/:  python tools/seed_sep22.py"""
import re, html, json, os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.environ.get('V5_HTML', os.path.join(HERE, '..', '..', 'verify-chris-sep22-v5.html'))
s = open(SRC).read()
rows = []
for m in re.finditer(r'<tr style="background:#[0-9a-f]+">(.*?)</tr>', s, re.S):
    r = m.group(1)
    tds = re.findall(r'<td[^>]*>(.*?)</td>', r, re.S)
    href = re.search(r'href="(mailto:[^"]*)"', r).group(1)
    date = re.search(r'Cox%20(\d\d/\d\d/\d{4})', href).group(1)
    amt = float(html.unescape(re.sub('<.*?>', '', tds[3])).replace('$', '').replace(',', ''))
    rows.append({'date': date, 'category': html.unescape(tds[1]), 'company': html.unescape(tds[2]), 'amount': amt})
tot = round(sum(r['amount'] for r in rows), 2)
assert len(rows) == 73 and abs(tot - 17024.09) < 0.005, (len(rows), tot)
for i, r in enumerate(rows): r['id'] = i + 1
doc = {'stmt': '2026-09-22', 'label': 'Sep 22, 2026 statement', 'due': 'Due by the first',
       'employees': [{'id': 'chris-cox', 'name': 'Chris Cox', 'first': 'Chris',
                      'email': 'chris@coxresearch.com', 'rows': rows}]}
out = os.path.join(HERE, '..', 'statements', '2026-09-22.json')
json.dump(doc, open(out, 'w'), indent=1)
print(len(rows), tot, os.path.abspath(out))
