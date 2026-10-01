"""Local queue history and its read-only viewer."""
from contextlib import closing
from datetime import date
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP
import json
from pathlib import Path
import sqlite3
import tkinter as tk
from tkinter import font as tkfont, messagebox, ttk

from queue_worker import BT_STAGES, parse_bt_usage
from queue_errors import error_details, error_info
from queue_stats import used_models, model_usage, merge_snapshots
from ui_language import tr

JEV_FIELDS = {'requests': '請求數', 'input_tokens': '輸入 Token', 'missing_input_usage_requests': '未回報請求數',
              'cost_nano_usd': '已知金額（USD）', 'cost_source': '金額來源', 'unpriced_requests': '未計價請求數',
              'calls': '處理次數', 'changed_calls': '變更次數', 'cleared_blocks': '清空文字塊',
              'trimmed_blocks': '修剪文字塊', 'kept_blocks': '保留文字塊',
              'removed_characters': '移除字元', 'elapsed_seconds': 'Jev 耗時'}


def jev_totals(jobs):
    result = {}
    for job in jobs:
        for key, value in job.get('jev_stats', {}).items():
            if value is not None:
                result[key] = result.get(key, 0) + value
    return result


def jev_columns(stats):
    reported, estimated = stats.get('reported_cost_requests', 0), stats.get('estimated_cost_requests', 0)
    source = tr('回報及估算') if reported and estimated else tr('服務回報') if reported else tr('估算') if estimated else '—'
    return tuple(elapsed_text(stats.get(key)) if key == 'elapsed_seconds'
                 else source if key == 'cost_source'
                 else f"US${Decimal(stats[key]) / 1_000_000_000:.9f}" if key == 'cost_nano_usd' and key in stats
                 else format_tokens(stats[key]) if key == 'input_tokens' and key in stats
                 else f'{stats[key]:,}' if key in stats else '—' for key in JEV_FIELDS)


def elapsed_text(seconds):
    if seconds is None:
        return "—"
    hours, seconds = divmod(max(0, int(seconds)), 3600)
    minutes, seconds = divmod(seconds, 60)
    return f'{hours:02}:{minutes:02}:{seconds:02}'


def format_tokens(tokens):
    for scale, unit in ((1_000_000_000, 'B'), (1_000_000, 'M'), (1_000, 'K')):
        if tokens >= scale:
            return f'{tokens / scale:.2f}{unit}'
    return str(tokens)


def usage_text(records, *, empty_text=None):
    if not records:
        return empty_text if empty_text is not None else tr("尚未回報")
    tokens = sum(value['total_tokens'] for value in records)
    token_text = format_tokens(tokens)
    costs = [Decimal(value['cost']) for value in records if value['cost'] is not None]
    cost = (tr("預估 {0}").format(f"US${sum(costs).quantize(Decimal('0.01'), rounding=ROUND_CEILING):,.2f}")
            if costs else tr("無法預估"))
    text = tr("{0} tokens｜{1}｜{2} 次請求").format(token_text, cost, sum(value['requests'] for value in records))
    if costs and any(value['cost'] is None or value['unpriced_requests'] or value.get('cost_incomplete') for value in records):
        text += tr("（僅含已知金額）")
    if any(value['missing_usage_requests'] for value in records):
        text += tr("（Token 回報不完整）")
    models = sorted(set().union(*(used_models(record) for record in records)))
    if models:
        text += '｜' + tr('模型') + '：' + ', '.join(models)
    return text


def save_run(path, record):
    payload = json.dumps(record, ensure_ascii=False, allow_nan=False)
    paths = "\n".join(job['path'] for job in record['jobs'])
    with closing(sqlite3.connect(path, timeout=1)) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, started_at TEXT NOT NULL, paths TEXT NOT NULL, data TEXT NOT NULL)")
        db.execute("CREATE INDEX IF NOT EXISTS runs_started_at ON runs(started_at)")
        db.execute("INSERT INTO runs VALUES (?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET started_at=excluded.started_at, paths=excluded.paths, data=excluded.data",
                   (record['id'], record['started_at'], paths, payload))


def find_runs(path, keyword='', start='', end='', *, limit=200, offset=0):
    return list(iter_runs(path, keyword, start, end, limit=limit, offset=offset))


def iter_runs(path, keyword='', start='', end='', *, limit=-1, offset=0):
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
        rows = db.execute("SELECT data FROM runs" + where + " ORDER BY started_at DESC, rowid DESC LIMIT ? OFFSET ?", [*values, limit, offset])
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


def completed_jobs(record):
    return sum(job.get('status') in ('done', 'done_warning') for job in record['jobs'])


def history_totals(path):
    counts = dict(runs=0, jobs=0, elapsed_seconds=0)
    jev = {}
    totals = {scope: {} for scope in ('OCR', 'Jev', 'translation', 'total')}
    # ponytail: scan stored runs on refresh; persist aggregates if history scanning becomes slow.
    for record in iter_runs(path):
        counts['runs'] += 1
        counts['jobs'] += completed_jobs(record)
        counts['elapsed_seconds'] += record.get('elapsed_seconds') or 0
        for key, value in jev_totals(record['jobs']).items():
            jev[key] = jev.get(key, 0) + value
        for scope in totals:
            totals[scope] = merge_snapshots((totals[scope], *scope_records(record, scope)))
    usage = {scope: [value] if value else [] for scope, value in totals.items()}
    return dict(counts, usage={scope: usage_text(values) for scope, values in usage.items()}, usage_records=usage, jev_stats=jev)


def usage_columns(records):
    costs = [Decimal(value['cost']) for value in records if value['cost'] is not None]
    cost = f"US${sum(costs).quantize(Decimal('0.01'), rounding=ROUND_CEILING):,.2f}" if costs else tr("無法預估")
    tokens = format_tokens(sum(value['total_tokens'] for value in records)) if records else tr("尚未回報")
    requests = f"{sum(value['requests'] for value in records):,}" if records else '—'
    partial = any(value['cost'] is None or value['unpriced_requests'] or value.get('cost_incomplete') for value in records)
    pricing = tr("僅含已知金額") if costs and partial else tr("完整") if costs else tr("尚未回報")
    reporting = tr("回報不完整") if any(value['missing_usage_requests'] for value in records) else tr("完整") if records else tr("尚未回報")
    return tokens, cost, requests, pricing, reporting


MODEL_COLUMNS = dict(scope='項目', model='模型', total_tokens='Token 數', token_share='Token 佔比',
                     requests='請求數', request_share='請求佔比', cost='預估金額（USD）', cost_share='已知費用佔比',
                     avg_tokens='平均 Token', avg_cost='平均費用（USD）',
                     prompt='輸入 Token', completion='輸出 Token',
                     cache_hit='快取讀取', cache_write='快取寫入', unpriced_requests='未計價請求數',
                     missing_usage_requests='未回報請求數', pricing='金額完整性')


def create_model_table(parent):
    note = ttk.Label(parent, text=tr('佔比依 OCR／翻譯／合計分別計算；Token 僅含已回報用量，費用僅含已知金額。平均值以每次請求計算；平均 Token 排除未回報請求，金額不完整時不顯示平均費用。') + '\n' + tr('合計已包含 OCR 與翻譯；Jev 另列。快取屬於輸入 Token，分項只累加已回報資料；舊資料不推算模型分項。'), wraplength=950)
    note.grid(row=0, column=0, columnspan=2, sticky='ew', padx=4, pady=4)
    note.bind('<Configure>', lambda event: note.configure(wraplength=event.width) if event.width > 100 else None)
    table = ttk.Treeview(parent, columns=tuple(MODEL_COLUMNS), show='headings', height=8)
    widths = dict(scope=60, model=180, total_tokens=100, token_share=85, requests=70, request_share=85,
                  cost=135, cost_share=115, avg_tokens=110, avg_cost=145, pricing=145)
    for key, title in MODEL_COLUMNS.items():
        table.heading(key, text=tr(title))
        table.column(key, width=max(widths.get(key, 110), tkfont.nametofont('TkHeadingFont').measure(tr(title)) + 16),
                     minwidth=55, stretch=False, anchor='w' if key in ('scope', 'model', 'pricing') else 'e')
    vertical = ttk.Scrollbar(parent, command=table.yview)
    horizontal = ttk.Scrollbar(parent, orient='horizontal', command=table.xview)
    table.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
    table.grid(row=1, column=0, sticky='nsew')
    vertical.grid(row=1, column=1, sticky='ns')
    horizontal.grid(row=2, column=0, sticky='ew')
    parent.rowconfigure(1, weight=1)
    parent.columnconfigure(0, weight=1)
    return table


def fill_model_usage(table, usage):
    rows = {}
    for scope, label in (('OCR', 'OCR'), ('translation', tr('翻譯')), ('total', tr('合計'))):
        models = {}
        for record in usage.get(scope, []):
            for model, value in model_usage(record).items():
                models[model] = merge_snapshots((models.get(model, {}), value))
        totals = merge_snapshots(models.values())
        for model, value in sorted(models.items()):
            value = dict(dict(total_tokens=0, requests=0, cost=None, unpriced_requests=0, missing_usage_requests=0), **value)
            cost = value['cost']
            reported_requests = value['requests'] - value['missing_usage_requests']
            fields = dict(scope=label, model=model or tr('模型未分項'), pricing=usage_columns([value])[3],
                          cost=f"US${Decimal(cost).quantize(Decimal('0.000001'), rounding=ROUND_CEILING):,.6f}" if cost is not None else tr('無法預估'))
            for column, key in (('token_share', 'total_tokens'), ('request_share', 'requests'), ('cost_share', 'cost')):
                amount, total = value.get(key), Decimal(totals.get(key) or 0)
                fields[column] = f"{(Decimal(amount) * 100 / total).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):.2f}%" if amount is not None and total > 0 else '—'
            fields['avg_tokens'] = f"{(Decimal(value['total_tokens']) / reported_requests).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):,.2f}" if reported_requests > 0 else '—'
            fields['avg_cost'] = f"US${(Decimal(cost) / value['requests']).quantize(Decimal('0.000001'), rounding=ROUND_CEILING):,.6f}" if cost is not None and value['requests'] > 0 and not value['unpriced_requests'] and not value.get('cost_incomplete') else '—'
            fields.update({key: f"{value[key]:,}" if key in value else '—'
                           for key in MODEL_COLUMNS if key not in fields})
            rows[json.dumps([scope, model])] = tuple(fields[key] for key in MODEL_COLUMNS)
    for item in set(table.get_children()) - rows.keys():
        table.delete(item)
    for item, values in rows.items():
        if not table.exists(item):
            table.insert('', 'end', iid=item, values=values)
        elif tuple(table.item(item, 'values')) != values:
            table.item(item, values=values)
    if table.get_children() != tuple(rows):
        for index, item in enumerate(rows):
            table.move(item, '', index)



class HistoryWindow(tk.Toplevel):
    def __init__(self, parent, path, actions, statuses):
        super().__init__(parent)
        self.title(tr("歷史紀錄"))
        self.geometry('1200x700')
        self.minsize(820, 480)
        self.path, self.actions, self.statuses = path, actions, statuses
        self.records = {}
        self.pending_children = set()
        self.offset = 0
        self.query = None
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
        self.previous = ttk.Button(filters, text=tr('上一頁'), command=lambda: self.change_page(-1))
        self.previous.pack(side='left', padx=(8, 2))
        self.next = ttk.Button(filters, text=tr('下一頁'), command=lambda: self.change_page(1))
        self.next.pack(side='left')
        self.note = tk.StringVar()
        ttk.Label(self, textvariable=self.note, padding=(8, 0), wraplength=780).pack(anchor='w')
        totals = ttk.LabelFrame(self, text=tr('全部歷史總累計'), padding=8)
        totals.pack(side='bottom', fill='x', padx=8, pady=(0, 8))
        summary = ttk.Frame(totals)
        summary.pack(fill='x', pady=(0, 6))
        self.total_counts = {}
        for index, (key, label) in enumerate((('runs', '佇列批數'), ('jobs', '完成數'), ('elapsed', '累計耗時'))):
            ttk.Label(summary, text=tr(label)).grid(row=0, column=index * 2, padx=(0, 6))
            variable = tk.StringVar()
            self.total_counts[key] = variable
            ttk.Entry(summary, textvariable=variable, state='readonly', width=14).grid(row=0, column=index * 2 + 1, sticky='ew', padx=(0, 12))
            summary.columnconfigure(index * 2 + 1, weight=1)
        self.total_usage = tk.StringVar()
        ttk.Label(summary, textvariable=self.total_usage, wraplength=950).grid(row=1, column=0, columnspan=7, sticky='w', pady=(4, 0))
        self.totals_toggle = ttk.Button(summary, text=tr('詳細統計') + ' ▾', command=self.toggle_totals)
        self.totals_toggle.grid(row=0, column=6, sticky='e')
        self.total_details = ttk.Frame(totals)
        columns = ('scope', 'tokens', 'cost', 'requests', 'pricing', 'reporting')
        self.totals = ttk.Treeview(self.total_details, columns=columns, show='headings', height=4, selectmode='browse')
        for key, title, width in zip(columns, ('項目', 'Token 數', '預估金額（USD）', '請求數', '金額完整性', 'Token 完整性'), (70, 130, 130, 80, 145, 145)):
            self.totals.heading(key, text=tr(title))
            self.totals.column(key, width=width, minwidth=40, anchor='w' if key in ('scope', 'pricing', 'reporting') else 'e')
        self.totals.pack(fill='x')
        self.jev_totals_table = ttk.Treeview(self.total_details, columns=tuple(JEV_FIELDS),
                                           displaycolumns=('requests', 'input_tokens', 'cost_nano_usd', 'cost_source', 'unpriced_requests', 'elapsed_seconds'),
                                           show='headings', height=1)
        for key, title in JEV_FIELDS.items():
            self.jev_totals_table.heading(key, text=tr(title))
            self.jev_totals_table.column(key, width=110, minwidth=40, anchor='e')
        self.jev_totals_table.pack(fill='x', pady=(4, 0))
        note = ttk.Label(self.total_details, text=tr("全部歷史，含失敗／停止已回報用量，不受查詢條件影響；耗時為各批次加總，可能重疊。"), wraplength=780)
        note.pack(fill='x', pady=(4, 0))
        note.bind('<Configure>', lambda event: note.configure(wraplength=max(100, event.width)))
        panes = ttk.Panedwindow(self, orient='vertical')
        panes.pack(fill='both', expand=True, padx=8, pady=8)
        listing = ttk.LabelFrame(panes, text=tr('執行紀錄'), padding=4)
        detail = ttk.LabelFrame(panes, text=tr('選取項目明細'), padding=4)
        panes.add(listing, weight=1)
        panes.add(detail, weight=1)
        columns = ('end', 'status', 'jobs', 'elapsed', 'tokens', 'cost', 'requests', 'pricing', 'reporting', 'error_reason')
        self.tree = ttk.Treeview(listing, columns=columns, show='tree headings', selectmode='browse', height=8)
        self.tree.heading('#0', text=tr("開始時間／資料夾"))
        self.tree.column('#0', width=320, minwidth=160)
        for key, title, width in zip(columns, (tr("完成時間"), tr("狀態"), tr("完成數"), tr("耗時"),
                                               tr("Token 數"), tr("預估金額（USD）"), tr("請求數"), tr("金額完整性"), tr("Token 完整性"), tr("錯誤原因")), (160, 100, 65, 90, 130, 130, 80, 130, 130, 220)):
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, minwidth=40, stretch=False)
        scroll = ttk.Scrollbar(listing, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind('<Configure>', self.fit_columns)
        self.tree.grid(row=0, column=0, sticky='nsew')
        scroll.grid(row=0, column=1, sticky='ns')
        listing.rowconfigure(0, weight=1)
        listing.columnconfigure(0, weight=1)
        self.details = ttk.Notebook(detail)
        self.details.pack(fill='both', expand=True)
        self.detail_tables = {}
        for key, title in (('basic', '基本資料'), ('usage', '用量與金額'), ('models', '模型統計'), ('stages', '各階段耗時'), ('jev', 'Jev 統計')):
            frame = ttk.Frame(self.details)
            self.details.add(frame, text=tr(title))
            if key == 'models':
                self.detail_tables[key] = create_model_table(frame)
                continue
            columns = self.totals['columns'] if key == 'usage' else ('field', 'value')
            table = ttk.Treeview(frame, columns=columns, show='headings', height=5)
            for column in columns:
                title = self.totals.heading(column, 'text') if key == 'usage' else tr('欄位') if column == 'field' else tr('資料')
                table.heading(column, text=title)
                table.column(column, width=130 if key == 'usage' or column == 'field' else 550, minwidth=40, stretch=True)
            vertical = ttk.Scrollbar(frame, command=table.yview)
            horizontal = ttk.Scrollbar(frame, orient='horizontal', command=table.xview)
            table.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
            table.grid(row=0, column=0, sticky='nsew')
            vertical.grid(row=0, column=1, sticky='ns')
            horizontal.grid(row=1, column=0, sticky='ew')
            frame.rowconfigure(0, weight=1)
            frame.columnconfigure(0, weight=1)
            self.detail_tables[key] = table
        self.tree.bind('<<TreeviewSelect>>', lambda _event: self.show_details())
        self.tree.bind('<<TreeviewOpen>>', lambda _event: self.load_children(self.tree.focus()))
        self.refresh()

    def toggle_totals(self):
        if self.total_details.winfo_manager():
            self.total_details.pack_forget()
            self.totals_toggle.configure(text=tr('詳細統計') + ' ▾')
        else:
            self.total_details.pack(fill='x', pady=(4, 0))
            self.totals_toggle.configure(text=tr('詳細統計') + ' ▴')


    def fit_columns(self, event=None):
        width = max(1, self.tree.winfo_width() - 4)
        font = tkfont.Font(root=self, font=ttk.Style(self.tree).lookup("Treeview", "font") or "TkDefaultFont")
        samples = {'#0': '2026-09-14 20:04:28', 'end': '2026-09-14 21:05:08',
                   'status': tr("完成（有異常）"), 'jobs': '999', 'elapsed': '999:59:59',
                   'tokens': '999,999,999', 'cost': 'US$9999.99', 'requests': '999,999'}
        sizes = {key: max(font.measure(value), font.measure(self.tree.heading(key, 'text'))) + 20
                 for key, value in samples.items()}
        sizes['#0'] += 20  # Reserve the tree expander.
        columns = ['end', 'status', 'jobs', 'elapsed', 'tokens', 'cost', 'requests']
        if sum(sizes.values()) > width:
            columns.remove('end')  # Full timestamps remain in the selected record's details.
        visible = ['#0', *columns]
        self.tree.configure(displaycolumns=columns)
        total = sum(sizes[key] for key in visible)
        scale = min(1, width / total)
        remaining = width
        for key in columns:
            size = max(1, int(sizes[key] * scale))
            self.tree.column(key, width=size, minwidth=1, stretch=False,
                             anchor='e' if key in ('jobs', 'tokens', 'cost', 'requests') else 'w')
            remaining -= size
        self.tree.column('#0', width=max(1, remaining), minwidth=1, stretch=False)
        self.tree.xview_moveto(0)

    def refresh(self):
        query = (self.keyword.get().strip(), self.start.get().strip(), self.end.get().strip())
        offset = self.offset if query == self.query else 0
        try:
            rows = find_runs(self.path, *query, limit=201, offset=offset)
            totals = history_totals(self.path)
        except (OSError, sqlite3.Error, ValueError) as error:
            messagebox.showerror(tr("歷史紀錄讀取失敗"), str(error), parent=self)
            return False
        self.query = query
        self.offset = offset
        self.previous.configure(state='normal' if self.offset else 'disabled')
        self.next.configure(state='normal' if len(rows) > 200 else 'disabled')
        rows = rows[:200]
        for key in ('runs', 'jobs'):
            self.total_counts[key].set(f"{totals[key]:,}")
        self.total_counts['elapsed'].set(elapsed_text(totals['elapsed_seconds']))
        self.total_usage.set('LLM ' + usage_text([dict(value, model=None, models=[])
                                                for value in totals['usage_records']['total']]))
        self.fill_usage(self.totals, totals['usage_records'])
        self.jev_totals_table.delete(*self.jev_totals_table.get_children())
        self.jev_totals_table.insert('', 'end', values=jev_columns(totals['jev_stats']))
        selected = self.tree.selection()
        expanded = {item for item in self.records if self.tree.exists(item) and self.tree.item(item, 'open')}
        self.tree.delete(*self.tree.get_children())
        self.records = {}
        self.pending_children.clear()
        for record in rows:
            self.tree.insert('', 'end', iid=record['id'], open=False,
                             text=record['started_at'][:19].replace('T', ' '), values=(
                (record.get('finished_at') or '—')[:19].replace('T', ' '),
                tr("未結束") if record['status'] == 'running' else tr(self.statuses[record['status']]),
                completed_jobs(record), elapsed_text(record.get('elapsed_seconds')),
                *usage_columns(scope_records(record, 'total')), ''))
            self.records[record['id']] = record, None
            if record['jobs']:
                self.pending_children.add(record['id'])
                self.tree.insert(record['id'], 'end')
            if record['id'] in expanded:
                self.load_children(record['id'])
                self.tree.item(record['id'], open=True)
                for item in self.tree.get_children(record['id']):
                    if item in expanded:
                        self.load_children(item)
                        self.tree.item(item, open=True)
        self.note.set(tr("第 {0} 頁｜顯示 {1} 筆（每頁 200 筆）；日期 YYYY-MM-DD，留白不限。展開批次與章節載入用量，選取後查看明細。").format(self.offset // 200 + 1, len(rows)))
        restored = [item for item in selected if self.tree.exists(item)]
        if restored:
            self.tree.selection_set(restored)
        elif rows:
            self.tree.selection_set(rows[0]['id'])
        self.show_details()
        self.fit_columns()
        return True

    def change_page(self, direction):
        previous = self.offset
        self.offset = max(0, self.offset + direction * 200)
        if not self.refresh():
            self.offset = previous

    def load_children(self, item):
        if item not in self.pending_children:
            return
        self.pending_children.remove(item)
        self.tree.delete(*self.tree.get_children(item))
        record, job = self.records[item]
        if job is None:
            for index, job in enumerate(record['jobs'], 1):
                usage = job.get('usage', {})
                child = self.tree.insert(item, 'end', iid=f'job:{record["id"]}:{index}', open=False, text=f"{index}. {job['path']}", values=(
                    (job.get('finished_at') or '—')[:19].replace('T', ' '),
                    tr(self.statuses[job['status']]), '', elapsed_text(job.get('elapsed_seconds')),
                    *usage_columns([usage['total']] if 'total' in usage else []),
                    error_info(job['status'], job.get('error', ''))[1]))
                self.records[child] = record, job
                self.pending_children.add(child)
                self.tree.insert(child, 'end')
        else:
            usage = job.get('usage', {})
            for scope, title, stage in (('OCR', 'OCR', 'OCR'), ('Jev', 'Jev', 'Jev'), ('translation', tr("翻譯"), 'Translation'),
                                        ('total', tr("合計"), None)):
                if scope == 'Jev' and scope not in usage:
                    continue
                seconds = job.get('elapsed_seconds') if stage is None else job.get('stage_seconds', {}).get(stage)
                if stage == 'Jev':
                    seconds = job.get('jev_stats', {}).get('elapsed_seconds')
                child = self.tree.insert(item, 'end', iid=f'{item}:scope:{scope}', text=title, values=(
                    '', '', '', elapsed_text(seconds),
                    *usage_columns([usage[scope]] if scope in usage else []), ''))
                self.records[child] = record, job

    def fill_usage(self, table, usage):
        table.delete(*table.get_children())
        for scope, label in (('OCR', 'OCR'), ('Jev', 'Jev'), ('translation', tr("翻譯")), ('total', tr("合計"))):
            if scope == 'Jev' and not usage[scope]:
                continue
            table.insert('', 'end', iid=scope, values=(label, *usage_columns(usage[scope])))

    def show_details(self):
        for table in self.detail_tables.values():
            table.delete(*table.get_children())
        selected = self.tree.selection()
        if not selected or selected[0] not in self.records:
            return
        record, job = self.records[selected[0]]
        source = job if job is not None else record
        fields = [(tr("項目"), tr("本次佇列累計") if job is None else job['path']),
                  (tr("狀態"), tr(self.statuses[source['status']])),
                  (tr("開始時間"), source.get('started_at') or '—'),
                  (tr("完成時間"), source.get('finished_at') or '—'),
                  (tr("最後儲存時間"), record.get('saved_at') or '—'),
                  (tr("累計耗時"), elapsed_text(source.get('elapsed_seconds')))]
        jobs = record['jobs'] if job is None else [job]
        if job is None:
            fields.append((tr("完成數"), completed_jobs(record)))
        else:
            fields.append((tr("動作"), tr(self.actions[job['action']])))
            if job['action'] == 'translate':
                pages = tr("全部頁面") if job.get('start_page', 1) == 1 and job.get('end_page') is None else f"{job.get('start_page', 1)}—{job.get('end_page') or '…'}"
                fields.append((tr("翻譯頁數"), pages))
            diagnostic = error_details(job['status'], job.get('error', ''))
            if diagnostic:
                fields.append((tr("錯誤原因"), diagnostic))
            for scope, label in (('OCR', 'OCR'), ('Jev', 'Jev'), ('translation', tr("翻譯")), ('total', tr("合計"))):
                value = job.get('usage', {}).get(scope, {})
                if value.get('price_basis'):
                    fields.append((label + ' / ' + tr("估價依據"), value['price_basis']))
                    fields.append((label + ' / ' + tr("費率日期"), value.get('rates_date') or '—'))
        for scope, label in (('OCR', 'OCR'), ('translation', tr('翻譯')), ('total', tr('合計'))):
            models = sorted(set().union(*(used_models(item.get('usage', {}).get(scope, {})) for item in jobs)))
            if models:
                fields.append((label + ' / ' + tr('模型'), ', '.join(models)))
        for label, value in fields:
            self.detail_tables['basic'].insert('', 'end', values=(label, value))
        self.fill_usage(self.detail_tables['usage'], {
            scope: [item['usage'][scope] for item in jobs if scope in item.get('usage', {})]
            for scope in ('OCR', 'Jev', 'translation', 'total')})
        fill_model_usage(self.detail_tables['models'], {
            scope: [item['usage'][scope] for item in jobs if scope in item.get('usage', {})]
            for scope in ('OCR', 'translation', 'total')})
        for stage in BT_STAGES:
            values = [item['stage_seconds'][stage] for item in jobs if stage in item.get('stage_seconds', {})]
            self.detail_tables['stages'].insert('', 'end', values=(tr(stage), elapsed_text(sum(values) if values else None)))
        stats = jev_totals(jobs)
        for label, value in zip(JEV_FIELDS.values(), jev_columns(stats)):
            self.detail_tables['jev'].insert('', 'end', values=(tr(label), value))
        self.detail_tables['stages'].insert('', 'end', values=(tr('Jev 耗時'), elapsed_text(stats.get('elapsed_seconds'))))
