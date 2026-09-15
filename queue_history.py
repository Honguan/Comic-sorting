"""Local queue history and its read-only viewer."""
from contextlib import closing
from datetime import date
from decimal import Decimal, ROUND_CEILING
import json
from pathlib import Path
import sqlite3
import tkinter as tk
from tkinter import messagebox, ttk

from queue_worker import BT_STAGES, parse_bt_usage
from queue_errors import error_details, error_info
from ui_language import tr


def elapsed_text(seconds):
    if seconds is None:
        return "—"
    hours, seconds = divmod(max(0, int(seconds)), 3600)
    minutes, seconds = divmod(seconds, 60)
    return f'{hours:02}:{minutes:02}:{seconds:02}'


def usage_text(records, *, empty_text=None):
    if not records:
        return empty_text if empty_text is not None else tr("尚未回報")
    tokens = sum(value['total_tokens'] for value in records)
    token_text = str(tokens)
    for scale, unit in ((1_000_000_000, 'B'), (1_000_000, 'M'), (1_000, 'K')):
        if tokens >= scale:
            token_text = f'{tokens / scale:.2f}{unit}'
            break
    costs = [Decimal(value['cost']) for value in records if value['cost'] is not None]
    cost = (tr("預估 {0}").format(f"US${sum(costs).quantize(Decimal('0.01'), rounding=ROUND_CEILING):,.2f}")
            if costs else tr("無法預估"))
    text = tr("{0} tokens｜{1}｜{2} 次請求").format(token_text, cost, sum(value['requests'] for value in records))
    if costs and any(value['cost'] is None or value['unpriced_requests'] for value in records):
        text += tr("（僅含已知金額）")
    if any(value['missing_usage_requests'] for value in records):
        text += tr("（Token 回報不完整）")
    return text


def save_run(path, record):
    payload = json.dumps(record, ensure_ascii=False, allow_nan=False)
    paths = "\n".join(job['path'] for job in record['jobs'])
    with closing(sqlite3.connect(path, timeout=1)) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, started_at TEXT NOT NULL, paths TEXT NOT NULL, data TEXT NOT NULL)")
        db.execute("CREATE INDEX IF NOT EXISTS runs_started_at ON runs(started_at)")
        db.execute("INSERT INTO runs VALUES (?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET started_at=excluded.started_at, data=excluded.data",
                   (record['id'], record['started_at'], paths, payload))


def find_runs(path, keyword='', start='', end=''):
    # ponytail: show 200 matches at once; add pagination if browsing longer ranges is needed.
    return list(iter_runs(path, keyword, start, end, limit=200))


def iter_runs(path, keyword='', start='', end='', *, limit=-1):
    try:
        if start:
            date.fromisoformat(start)
        if end:
            date.fromisoformat(end)
    except ValueError as error:
        raise ValueError(tr("日期請使用 YYYY-MM-DD 格式")) from error
    if start and end and end < start:
        raise ValueError(tr("結束日期不可早於開始日期"))
    if not Path(path).exists():
        return
    clauses, values = [], []
    if keyword:
        clauses.append("instr(lower(paths), lower(?)) > 0")
        values.append(keyword)
    if start:
        clauses.append("substr(started_at, 1, 10) >= ?")
        values.append(start)
    if end:
        clauses.append("substr(started_at, 1, 10) <= ?")
        values.append(end)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=1)) as db:
        rows = db.execute("SELECT data FROM runs" + where + " ORDER BY started_at DESC, rowid DESC LIMIT ?", [*values, limit])
        for row in rows:
            record = json.loads(row[0])
            for job in record['jobs']:
                # Older versions omitted priced subtotals; recover only matching saved summaries.
                for line in job.get('error', '').splitlines():
                    parsed = parse_bt_usage(line)
                    saved = job.get('usage', {}).get(parsed['scope']) if parsed else None
                    if (saved and saved['cost'] is None and parsed['cost'] is not None
                            and all(saved[key] == parsed[key] for key in
                                    ('requests', 'total_tokens', 'missing_usage_requests', 'unpriced_requests'))):
                        saved['cost'] = str(parsed['cost'])
            yield record


def scope_records(record, scope):
    return [job['usage'][scope] for job in record['jobs'] if scope in job.get('usage', {})]


def history_totals(path):
    counts = dict(runs=0, jobs=0, elapsed_seconds=0)
    usage = {scope: [] for scope in ('OCR', 'translation', 'total')}
    # ponytail: scan stored runs on refresh; persist aggregates if history scanning becomes slow.
    for record in iter_runs(path):
        counts['runs'] += 1
        counts['jobs'] += len(record['jobs'])
        counts['elapsed_seconds'] += record.get('elapsed_seconds') or 0
        for scope, values in usage.items():
            values.extend(scope_records(record, scope))
    return dict(counts, usage={scope: usage_text(values) for scope, values in usage.items()}, usage_records=usage)


class HistoryWindow(tk.Toplevel):
    def __init__(self, parent, path, actions, statuses):
        super().__init__(parent)
        self.title(tr("歷史紀錄"))
        self.geometry('1120x700')
        self.minsize(820, 480)
        self.path, self.actions, self.statuses = path, actions, statuses
        self.records = {}
        self.keyword, self.start, self.end = tk.StringVar(), tk.StringVar(), tk.StringVar()
        filters = ttk.Frame(self, padding=8)
        filters.pack(fill='x')
        search = ttk.Frame(self, padding=(8, 8, 8, 0))
        search.pack(fill='x', before=filters)
        ttk.Label(search, text=tr("漫畫名稱／路徑")).pack(side='left')
        entry = ttk.Entry(search, textvariable=self.keyword)
        entry.pack(side='left', fill='x', expand=True, padx=(8, 0))
        entry.bind('<Return>', lambda _event: self.refresh())
        for label, variable, width in ((tr("開始日期"), self.start, 12), (tr("結束日期"), self.end, 12)):
            ttk.Label(filters, text=label).pack(side='left')
            entry = ttk.Entry(filters, textvariable=variable, width=width)
            entry.pack(side='left', padx=(4, 8))
            entry.bind('<Return>', lambda _event: self.refresh())
        ttk.Button(filters, text=tr("查詢／重新整理"), command=self.refresh).pack(side='left')
        self.note = tk.StringVar()
        ttk.Label(self, textvariable=self.note, padding=(8, 0), wraplength=780).pack(anchor='w')
        totals = ttk.LabelFrame(self, text=tr("全部總累計（所有歷史）"), padding=8)
        totals.pack(side='bottom', fill='x', padx=8, pady=(0, 8))
        summary = ttk.Frame(totals)
        summary.pack(fill='x', pady=(0, 6))
        self.total_counts = {}
        for index, (key, label) in enumerate((('runs', '佇列批數'), ('jobs', '工作數'), ('elapsed', '累計耗時'))):
            ttk.Label(summary, text=tr(label)).grid(row=0, column=index * 2, padx=(0, 6))
            variable = tk.StringVar()
            self.total_counts[key] = variable
            ttk.Entry(summary, textvariable=variable, state='readonly', width=14).grid(row=0, column=index * 2 + 1, sticky='ew', padx=(0, 12))
            summary.columnconfigure(index * 2 + 1, weight=1)
        columns = ('scope', 'tokens', 'cost', 'requests', 'pricing', 'reporting')
        self.totals = ttk.Treeview(totals, columns=columns, show='headings', height=3, selectmode='browse')
        for key, title, width in zip(columns, ('項目', 'Token 數', '預估金額（USD）', '請求數', '金額完整性', 'Token 完整性'), (70, 130, 130, 80, 145, 145)):
            self.totals.heading(key, text=tr(title))
            self.totals.column(key, width=width, minwidth=40, anchor='w' if key in ('scope', 'pricing', 'reporting') else 'e')
        self.totals.pack(fill='x')
        note = ttk.Label(totals, text=tr("全部歷史，含失敗／停止已回報用量，不受查詢條件影響；耗時為各批次加總，可能重疊。"), wraplength=780)
        note.pack(fill='x', pady=(4, 0))
        note.bind('<Configure>', lambda event: note.configure(wraplength=max(100, event.width)))
        panes = ttk.Panedwindow(self, orient='vertical')
        panes.pack(fill='both', expand=True, padx=8, pady=8)
        listing, detail = ttk.Frame(panes), ttk.Frame(panes)
        panes.add(listing, weight=1)
        panes.add(detail, weight=1)
        columns = ('end', 'status', 'jobs', 'elapsed', 'usage', 'error_reason')
        self.tree = ttk.Treeview(listing, columns=columns, show='tree headings', selectmode='browse', height=8)
        self.tree.heading('#0', text=tr("開始時間／資料夾"))
        self.tree.column('#0', width=320, minwidth=160)
        for key, title, width in zip(columns, (tr("完成時間"), tr("狀態"), tr("工作數"), tr("耗時"),
                                               tr("合計用量／預估金額"), tr("錯誤原因")), (160, 100, 65, 90, 350, 300)):
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, minwidth=300 if key == 'usage' else 20, stretch=key == 'usage')
        scroll = ttk.Scrollbar(listing, command=self.tree.yview)
        horizontal = ttk.Scrollbar(listing, orient='horizontal', command=self.tree.xview)
        self.tree.configure(yscrollcommand=scroll.set, xscrollcommand=horizontal.set)
        self.tree.grid(row=0, column=0, sticky='nsew')
        scroll.grid(row=0, column=1, sticky='ns')
        horizontal.grid(row=1, column=0, sticky='ew')
        listing.rowconfigure(0, weight=1)
        listing.columnconfigure(0, weight=1)
        self.details = tk.Text(detail, wrap='word', state='disabled', height=14)
        detail_scroll = ttk.Scrollbar(detail, command=self.details.yview)
        self.details.configure(yscrollcommand=detail_scroll.set)
        detail_scroll.pack(side='right', fill='y')
        self.details.pack(fill='both', expand=True)
        self.tree.bind('<<TreeviewSelect>>', lambda _event: self.show_details())
        self.refresh()

    def refresh(self):
        try:
            rows = find_runs(self.path, self.keyword.get().strip(), self.start.get().strip(), self.end.get().strip())
            totals = history_totals(self.path)
        except (OSError, sqlite3.Error, ValueError) as error:
            messagebox.showerror(tr("歷史紀錄讀取失敗"), str(error), parent=self)
            return
        for key in ('runs', 'jobs'):
            self.total_counts[key].set(f"{totals[key]:,}")
        self.total_counts['elapsed'].set(elapsed_text(totals['elapsed_seconds']))
        self.totals.delete(*self.totals.get_children())
        for scope, label in (('OCR', 'OCR'), ('translation', tr("翻譯")), ('total', tr("合計"))):
            records = totals['usage_records'][scope]
            costs = [Decimal(value['cost']) for value in records if value['cost'] is not None]
            cost = f"US${sum(costs).quantize(Decimal('0.01'), rounding=ROUND_CEILING):,.2f}" if costs else tr("無法預估")
            tokens = f"{sum(value['total_tokens'] for value in records):,}" if records else tr("尚未回報")
            requests = f"{sum(value['requests'] for value in records):,}" if records else '—'
            partial = any(value['cost'] is None or value['unpriced_requests'] for value in records)
            pricing = tr("僅含已知金額") if costs and partial else tr("完整") if costs else tr("尚未回報")
            reporting = tr("回報不完整") if any(value['missing_usage_requests'] for value in records) else tr("完整") if records else tr("尚未回報")
            self.totals.insert('', 'end', iid=scope, values=(label, tokens, cost, requests, pricing, reporting))
        self.tree.delete(*self.tree.get_children())
        self.records = {}
        for record in rows:
            self.tree.insert('', 'end', iid=record['id'], open=False,
                             text=record['started_at'][:19].replace('T', ' '), values=(
                (record.get('finished_at') or '—')[:19].replace('T', ' '),
                tr("未結束") if record['status'] == 'running' else tr(self.statuses[record['status']]),
                len(record['jobs']), elapsed_text(record.get('elapsed_seconds')),
                usage_text(scope_records(record, 'total'), empty_text=tr("無法預估")), ''))
            self.records[record['id']] = record, None
            for index, job in enumerate(record['jobs'], 1):
                usage = job.get('usage', {})
                item = self.tree.insert(record['id'], 'end', open=False, text=f"{index}. {job['path']}", values=(
                    (job.get('finished_at') or '—')[:19].replace('T', ' '),
                    tr(self.statuses[job['status']]), '', elapsed_text(job.get('elapsed_seconds')),
                    usage_text([usage['total']] if 'total' in usage else [], empty_text=tr("無法預估")),
                    error_info(job['status'], job.get('error', ''))[1]))
                self.records[item] = record, job
                for scope, title, stage in (('OCR', 'OCR', 'OCR'), ('translation', tr("翻譯"), 'Translation'),
                                            ('total', tr("合計"), None)):
                    seconds = job.get('elapsed_seconds') if stage is None else job.get('stage_seconds', {}).get(stage)
                    child = self.tree.insert(item, 'end', text=title, values=(
                        '', '', '', elapsed_text(seconds),
                        usage_text([usage[scope]] if scope in usage else [], empty_text=tr("無法預估")), ''))
                    self.records[child] = record, job
        self.note.set(tr("顯示 {0} 筆（最多 200 筆）；日期格式 YYYY-MM-DD，留白不限。展開佇列及資料夾查看用量，選取後查看詳細資料。").format(len(rows)))
        if rows:
            self.tree.selection_set(rows[0]['id'])
        self.show_details()

    def show_details(self):
        selected = self.tree.selection()
        lines = []
        if selected:
            record, job = self.records[selected[0]]
            if job is None:
                lines = [tr("本次佇列累計"), tr("開始時間") + ': ' + record['started_at'],
                         tr("完成時間") + ': ' + (record.get('finished_at') or '—'),
                         tr("最後儲存時間") + ': ' + record['saved_at'],
                         tr("總耗時：{0}").format(elapsed_text(record.get('elapsed_seconds')))]
                for scope, label in (('OCR', 'OCR'), ('translation', tr("翻譯")), ('total', tr("合計"))):
                    lines.append(label + ': ' + usage_text(scope_records(record, scope), empty_text=tr("無法預估")))
                for stage in BT_STAGES:
                    values = [item['stage_seconds'][stage] for item in record['jobs'] if stage in item.get('stage_seconds', {})]
                    lines.append(tr(stage) + ': ' + elapsed_text(sum(values) if values else None))
            else:
                lines = [job['path'], tr(self.actions[job['action']]) + ' / ' + tr(self.statuses[job['status']]),
                         tr("開始時間") + ': ' + (job.get('started_at') or '—'),
                         tr("完成時間") + ': ' + (job.get('finished_at') or '—'),
                         tr("總耗時：{0}").format(elapsed_text(job.get('elapsed_seconds')))]
                if job['action'] == 'translate':
                    pages = tr("全部頁面") if job['start_page'] == 1 and job['end_page'] is None else f"{job['start_page']}—{job['end_page'] or '…'}"
                    lines.append(tr("翻譯頁數") + ': ' + pages)
                for scope, label in (('OCR', 'OCR'), ('translation', tr("翻譯")), ('total', tr("合計"))):
                    value = job.get('usage', {}).get(scope)
                    lines.append(label + ': ' + usage_text([value] if value else [], empty_text=tr("無法預估")))
                    if value and value.get('price_basis'):
                        lines.append(tr("估價依據：{0}（費率日期：{1}）").format(value['price_basis'], value.get('rates_date') or '—'))
                for stage in BT_STAGES:
                    lines.append(tr(stage) + ': ' + elapsed_text(job.get('stage_seconds', {}).get(stage)))
                diagnostic = error_details(job['status'], job.get('error', ''))
                if diagnostic:
                    lines.append(diagnostic)
            lines.append(tr("階段耗時可能重疊；金額為估算，僅包含已回報用量。"))
        self.details.configure(state='normal')
        self.details.delete('1.0', 'end')
        self.details.insert('1.0', '\n'.join(lines))
        self.details.configure(state='disabled')
