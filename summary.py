"""Monthly category summary for Chris and Meghan.

This module builds the summary, the PDF, and the spreadsheet. It does not
call QuickBooks, Dropbox, or any other outside service. Posting the bill is
Littledude's job after the summary email arrives.
"""
import csv, io, json, zipfile
from datetime import datetime
from xml.sax.saxutils import escape as xml_escape

AMEX_CATEGORIES = ('Meals and Entertainment', 'Gift', 'Utilities', 'Freight & Delivery', 'Office Expenses',
                   'Job Materials', 'Supplies', 'Repair & Maintenance', 'Travel Fuel', 'Auto Maintenance',
                   'Travel', 'Other Expense')
UNCATEGORIZED = 'Uncategorized'


def to_cents(amount):
    return int(round(float(amount) * 100))


def format_money(cents):
    sign = '-' if cents < 0 else ''
    cents = abs(int(cents))
    return f'{sign}${cents // 100:,}.{cents % 100:02d}'


def final_category(row, categories):
    """Employee edit wins. Otherwise the statement category, canonical when it matches."""
    if isinstance(categories, dict) and row.get('id') in categories:
        return categories[row['id']] or UNCATEGORIZED
    raw = str(row.get('category') or '').strip()
    key = raw.casefold()
    for name in AMEX_CATEGORIES:
        if name.casefold() == key:
            return name
    return raw or UNCATEGORIZED


def statement_period(doc):
    """Month folder name is 'American Express charges <Month> <YYYY>'."""
    close = str((doc or {}).get('close_date') or (doc or {}).get('stmt') or '')[:10]
    try:
        dt = datetime.strptime(close, '%Y-%m-%d')
    except ValueError:
        try:
            dt = datetime.strptime(close[:7], '%Y-%m')
            close = dt.strftime('%Y-%m-01')
        except ValueError:
            month = close or 'Statement'
            return {'ym': 'unknown', 'close_date': close, 'month_label': month,
                    'folder_label': f'American Express charges {month}'}
    month = dt.strftime('%B %Y')
    return {'ym': dt.strftime('%Y-%m'), 'close_date': dt.strftime('%Y-%m-%d'),
            'month_label': month, 'folder_label': f'American Express charges {month}'}


def build_summary(doc, state_fn):
    """Category totals of submitted charges only. Statement total includes everyone."""
    period = statement_period(doc)
    buckets = {name: 0 for name in AMEX_CATEGORIES}
    extras = {}
    people = []
    statement_cents = 0
    summary_cents = 0
    for emp in doc.get('employees') or []:
        _flags, submitted, comment, categories = state_fn(doc['stmt'], emp['id'])
        charges = []
        own = 0
        for row in emp.get('rows') or []:
            cents = to_cents(row.get('amount') or 0)
            statement_cents += cents
            own += cents
            final = final_category(row, categories)
            charges.append({
                'id': row.get('id'),
                'date': row.get('date') or '',
                'merchant': row.get('company') or '',
                'amount_cents': cents,
                'original_category': row.get('category') or '',
                'final_category': final,
                'flagged': bool(submitted) and row.get('id') in set(submitted.get('flagged') or []),
            })
            if submitted:
                summary_cents += cents
                if final in buckets:
                    buckets[final] += cents
                else:
                    extras[final] = extras.get(final, 0) + cents
        people.append({
            'id': emp['id'],
            'name': emp.get('name') or emp['id'],
            'submitted': submitted is not None,
            'ts': (submitted or {}).get('ts') or '',
            'comment': (submitted or {}).get('comment') or comment or '',
            'total_cents': own,
            'charges': charges,
            'flagged': list((submitted or {}).get('flagged') or []),
            'approved': list((submitted or {}).get('approved') or []),
        })
    category_rows = [(name, buckets[name]) for name in AMEX_CATEGORIES]
    for name in sorted(extras):
        category_rows.append((name, extras[name]))
    difference = statement_cents - summary_cents
    everyone = bool(people) and all(p['submitted'] for p in people)
    return {
        'stmt': doc['stmt'],
        'label': doc.get('label') or doc['stmt'],
        'period': period,
        'people': people,
        'categories': category_rows,
        'summary_cents': summary_cents,
        'statement_cents': statement_cents,
        'difference_cents': difference,
        'everyone_submitted': everyone,
        'ready': everyone and difference == 0,
    }


def category_payload(summary):
    return {
        'statement': summary['stmt'],
        'close_date': summary['period']['close_date'],
        'month': summary['period']['month_label'],
        'folder': summary['period']['folder_label'],
        'categories': {name: round(cents / 100, 2) for name, cents in summary['categories']},
        'grand_total': round(summary['summary_cents'] / 100, 2),
        'statement_total': round(summary['statement_cents'] / 100, 2),
    }


def submission_record(summary, person):
    return {
        'statement': summary['stmt'],
        'close_date': summary['period']['close_date'],
        'emp_id': person['id'],
        'name': person['name'],
        'submitted_at': person['ts'],
        'comment': person['comment'],
        'total': round(person['total_cents'] / 100, 2),
        'flagged': person['flagged'],
        'approved': person['approved'],
        'charges': [{
            'date': c['date'],
            'merchant': c['merchant'],
            'amount': round(c['amount_cents'] / 100, 2),
            'original_category': c['original_category'],
            'final_category': c['final_category'],
            'flagged': c['flagged'],
        } for c in person['charges']],
    }


def summary_table_rows(summary):
    """Rows for the spreadsheet: categories, totals, then who submitted."""
    rows = [['Category', 'Amount']]
    for name, cents in summary['categories']:
        rows.append([name, f'{cents / 100:.2f}'])
    rows.append(['Grand total', f'{summary["summary_cents"] / 100:.2f}'])
    rows.append(['Statement total', f'{summary["statement_cents"] / 100:.2f}'])
    rows.append(['Difference', f'{summary["difference_cents"] / 100:.2f}'])
    rows.append([])
    rows.append(['Employee', 'Submitted', 'When', 'Total'])
    for person in summary['people']:
        rows.append([
            person['name'],
            'yes' if person['submitted'] else 'no',
            person['ts'],
            f'{person["total_cents"] / 100:.2f}',
        ])
    rows.append([])
    rows.append(['Close date', summary['period']['close_date']])
    rows.append(['Month', summary['period']['month_label']])
    rows.append(['Folder', summary['period']['folder_label']])
    return rows


def build_csv(summary):
    buf = io.StringIO()
    writer = csv.writer(buf)
    for row in summary_table_rows(summary):
        safe = []
        for cell in row:
            text = '' if cell is None else str(cell)
            if text[:1] in '=+-@\t\r':
                text = "'" + text
            safe.append(text)
        writer.writerow(safe)
    return buf.getvalue().encode('utf-8')


def _xlsx_sheet(rows):
    body = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>']
    for r, row in enumerate(rows, start=1):
        body.append(f'<row r="{r}">')
        for c, cell in enumerate(row):
            col = chr(ord('A') + c)
            text = xml_escape('' if cell is None else str(cell))
            body.append(f'<c r="{col}{r}" t="inlineStr"><is><t>{text}</t></is></c>')
        body.append('</row>')
    body.append('</sheetData></worksheet>')
    return ''.join(body).encode('utf-8')


def build_xlsx(summary):
    sheet = _xlsx_sheet(summary_table_rows(summary))
    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>"""
    rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""
    workbook = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets><sheet name="Summary" sheetId="1" r:id="rId1"/></sheets></workbook>"""
    workbook_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('[Content_Types].xml', content_types)
        zf.writestr('_rels/.rels', rels)
        zf.writestr('xl/workbook.xml', workbook)
        zf.writestr('xl/_rels/workbook.xml.rels', workbook_rels)
        zf.writestr('xl/worksheets/sheet1.xml', sheet)
    return buf.getvalue()


def _pdf_escape(text):
    raw = str(text).encode('latin-1', 'replace').decode('latin-1')
    return raw.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')


def build_pdf(summary):
    """A simple Helvetica PDF of the category summary and who submitted."""
    lines = [
        summary['period']['folder_label'],
        f"Statement close date: {summary['period']['close_date']}",
        '',
        'Category totals (submitted charges, edited categories)',
    ]
    for name, cents in summary['categories']:
        lines.append(f"{name}: {format_money(cents)}")
    lines += [
        '',
        f"Grand total: {format_money(summary['summary_cents'])}",
        f"Statement total: {format_money(summary['statement_cents'])}",
        f"Difference: {format_money(summary['difference_cents'])}",
        '',
        'Cardholders',
    ]
    for person in summary['people']:
        status = 'submitted ' + person['ts'] if person['submitted'] else 'not submitted'
        lines.append(f"{person['name']}: {status} ({format_money(person['total_cents'])})")
    pages = [lines[i:i + 46] for i in range(0, len(lines), 46)] or [[]]
    objects = []

    def add(body):
        objects.append(body)
        return len(objects)

    font_id = add('<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>')
    page_ids = []
    content_ids = []
    for chunk in pages:
        commands = ['BT', '/F1 11 Tf', '50 760 Td', '14 TL']
        for line in chunk:
            commands.append(f'({_pdf_escape(line)}) Tj')
            commands.append('T*')
        commands.append('ET')
        stream = '\n'.join(commands).encode('latin-1', 'replace') + b'\n'
        header = f'<< /Length {len(stream)} >>\nstream\n'.encode('latin-1')
        content_ids.append(add(header + stream + b'endstream'))
        page_ids.append(None)
    # Page objects point at the pages tree, which is added immediately after them.
    pages_id = len(objects) + len(content_ids) + 1
    for index, content_id in enumerate(content_ids):
        page_body = (f'<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 612 792] '
                     f'/Contents {content_id} 0 R /Resources << /Font << /F1 {font_id} 0 R >> >> >>')
        page_ids[index] = add(page_body)
    kids_ref = ' '.join(f'{pid} 0 R' for pid in page_ids)
    real_pages = add(f'<< /Type /Pages /Kids [{kids_ref}] /Count {len(page_ids)} >>')
    if real_pages != pages_id:
        raise RuntimeError('pdf page tree id mismatch')
    catalog = add(f'<< /Type /Catalog /Pages {real_pages} 0 R >>')
    out = bytearray(b'%PDF-1.4\n')
    offsets = [0]
    for num, body in enumerate(objects, start=1):
        offsets.append(len(out))
        if isinstance(body, str):
            body = body.encode('latin-1')
        out += f'{num} 0 obj\n'.encode('latin-1') + body + b'\nendobj\n'
    xref = len(out)
    out += f'xref\n0 {len(objects) + 1}\n'.encode('latin-1')
    out += b'0000000000 65535 f \n'
    for offset in offsets[1:]:
        out += f'{offset:010d} 00000 n \n'.encode('latin-1')
    out += (f'trailer\n<< /Size {len(objects) + 1} /Root {catalog} 0 R >>\n'
            f'startxref\n{xref}\n%%EOF\n').encode('latin-1')
    return bytes(out)


def summary_filename(summary, ext):
    """Saved and attached as 'American Express charges October 2026 - Summary.pdf'."""
    return f"{summary['period']['folder_label']} - Summary.{ext}"


def qb_subject(summary):
    return f"QB SUBMIT: {summary['period']['folder_label']}"


def qb_bodies(summary, actor):
    payload = category_payload(summary)
    block = json.dumps(payload, indent=2)
    folder = summary['period']['folder_label']
    text = (
        f"{actor} sent {folder} for posting.\n"
        f"Statement close date: {summary['period']['close_date']}\n"
        f"Folder: {folder}\n\n"
        f"{block}\n\n"
        f"Create the American Express bill from this summary and file the attachments in {folder}. "
        "This message was only emailed. The verification server did not contact QuickBooks or Dropbox.\n"
    )
    html_block = (f"<pre style='font-family:Consolas,monospace;font-size:13px'>{xml_escape(block)}</pre>")
    body = (
        "<div style='font-family:Arial,Helvetica,sans-serif'>"
        f"<p><b>{xml_escape(actor)}</b> sent {xml_escape(folder)} for posting.</p>"
        f"<p>Statement close date: {xml_escape(summary['period']['close_date'])}</p>"
        f"<p>Folder: {xml_escape(folder)}</p>"
        f"{html_block}"
        f"<p>Create the American Express bill from this summary and file the attachments in {xml_escape(folder)}. "
        "This message was only emailed. The verification server did not contact QuickBooks or Dropbox.</p>"
        "</div>"
    )
    return text, body
