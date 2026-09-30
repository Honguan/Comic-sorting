"""One-shot local schedules; closing the application discards them."""
from datetime import datetime, timedelta
import math
import time
import tkinter as tk
from tkinter import ttk, messagebox
from ui_language import tr
from queue_phases import READY


class QueueSchedule:
    def __init__(self, queue):
        self.queue = queue
        self.entries = []
        self.window = None
        self.after = queue.app.root.after(1000, self.tick)
        queue.app.root.bind('<Destroy>', self.close, add='+')

    def close(self, event):
        if event.widget is self.queue.app.root and self.after is not None:
            self.queue.app.root.after_cancel(self.after)
            self.after = None

    def options(self):
        q = self.queue
        return (q.installation.get(), q.config.get(), q.python.get(), q.export.get(),
                q.cleanup.get(), q.app.komga_path.get(), q.app.skip_unchanged.get(), q.workflow_mode())

    @staticmethod
    def signature(job):
        return job.path, job.action, job.start_page, job.end_page, job.range_export

    def add(self, countdown, value):
        q = self.queue
        if q.running or q.app.manga_busy:
            raise ValueError(tr('請在佇列閒置時新增排程。'))
        if countdown:
            minutes = float(value)
            if not math.isfinite(minutes) or minutes <= 0 or minutes > 525600:
                raise ValueError(tr('倒數分鐘須大於 0 且不超過 525600。'))
        else:
            due = datetime.strptime(value.strip(), '%Y-%m-%d %H:%M')
            if due <= datetime.now():
                raise ValueError(tr('請設定未來的日期時間。'))
            deadline = None
        jobs = q.configure_start(q.start_targets())
        if not jobs or not q.validate(jobs=jobs):
            return
        previous = q.active_jobs
        try:
            q.active_jobs = jobs
            if not q.confirm_cleanup():
                return
        finally:
            q.active_jobs = previous
        if countdown:
            due = datetime.now() + timedelta(minutes=minutes)
            deadline = time.monotonic() + minutes * 60
        elif due <= datetime.now():
            raise ValueError(tr('請設定未來的日期時間。'))
        self.entries.append(dict(due=due, deadline=deadline, jobs=[(job, self.signature(job)) for job in jobs],
                                 options=self.options(), status='等待'))
        self.refresh()

    def tick(self):
        self.after = None
        try:
            q = self.queue
            for entry in sorted(self.entries, key=lambda item: item['due']):
                if entry['status'] != '等待':
                    continue
                ready = (time.monotonic() >= entry['deadline'] if entry['deadline'] is not None
                         else datetime.now() >= entry['due'])
                if not ready or q.running or q.app.manga_busy or q.app.root.grab_current() is not None:
                    continue
                entry['status'] = '已略過'
                if entry['options'] != self.options():
                    entry['status'] = '設定已變更，請重新排程'
                    continue
                current_jobs = {id(job) for job in q.jobs}
                jobs = tuple(job for job, signature in entry['jobs']
                             if id(job) in current_jobs and job.status in READY
                             and self.signature(job) == signature)
                if jobs:
                    entry['status'] = '啟動失敗'
                    q.start(cleanup_confirmed=True, scheduled_jobs=jobs)
                    if q.running:
                        entry['status'] = '已啟動'
                    break
            self.refresh()
        finally:
            self.after = self.queue.app.root.after(1000, self.tick)

    def show(self):
        if self.window is not None and self.window.winfo_exists():
            self.window.lift()
            return
        self.window = window = tk.Toplevel(self.queue.app.root)
        window.title(tr('排程啟動'))
        window.geometry('820x520')
        window.minsize(720, 440)
        ttk.Label(window, text=tr('一次性排程；需保持程式開啟。忙碌時等待，關閉程式即取消。'), wraplength=720).pack(pady=8)
        self.tabs = ttk.Notebook(window)
        self.tabs.pack(fill='x', padx=8)
        countdown = ttk.Frame(self.tabs, padding=10)
        fixed = ttk.Frame(self.tabs, padding=10)
        self.tabs.add(countdown, text=tr('倒數啟動'))
        self.tabs.add(fixed, text=tr('指定日期時間'))
        future = datetime.now() + timedelta(hours=1)
        self.hours = tk.StringVar(window, '1')
        self.minutes = tk.StringVar(window, '0')
        self.date = tk.StringVar(window, future.strftime('%Y-%m-%d'))
        self.hour = tk.StringVar(window, future.strftime('%H'))
        self.minute = tk.StringVar(window, future.strftime('%M'))
        for variable, maximum, label in ((self.hours, 8760, '小時'), (self.minutes, 59, '分鐘')):
            ttk.Spinbox(countdown, from_=0, to=maximum, textvariable=variable, width=6).pack(side='left')
            ttk.Label(countdown, text=tr(label)).pack(side='left', padx=(4, 12))
        def preset(minutes):
            self.hours.set(str(minutes // 60))
            self.minutes.set(str(minutes % 60))
        for minutes in (15, 30, 60, 120):
            ttk.Button(countdown, text=f'{minutes} ' + tr('分鐘'), width=0,
                       command=lambda value=minutes: preset(value)).pack(side='left', padx=2)
        ttk.Label(fixed, text=tr('日期')).pack(side='left', padx=(0, 4))
        ttk.Entry(fixed, textvariable=self.date, width=12).pack(side='left')
        ttk.Label(fixed, text=' YYYY-MM-DD ').pack(side='left')
        for variable, maximum, label in ((self.hour, 23, '時'), (self.minute, 59, '分')):
            ttk.Spinbox(fixed, from_=0, to=maximum, textvariable=variable, width=4, wrap=True).pack(side='left')
            ttk.Label(fixed, text=tr(label)).pack(side='left', padx=(4, 8))
        for days, label in ((0, '今天'), (1, '明天')):
            ttk.Button(fixed, text=tr(label), width=0, command=lambda days=days:
                       self.date.set((datetime.now() + timedelta(days=days)).strftime('%Y-%m-%d'))).pack(side='left', padx=2)
        row = ttk.Frame(window, padding=(8, 8))
        row.pack(fill='x')
        self.preview = ttk.Label(row, wraplength=500)
        self.preview.pack(side='left', fill='x', expand=True)
        def add():
            try:
                is_countdown, value, _ = self.form_value()
                self.add(is_countdown, value)
            except (ValueError, OverflowError) as error:
                messagebox.showerror(tr('排程啟動'), str(error), parent=window)
        self.add_button = ttk.Button(row, text=tr('新增排程'), command=add)
        self.add_button.pack(side='right')
        for variable in (self.hours, self.minutes, self.date, self.hour, self.minute):
            variable.trace_add('write', lambda *_: self.update_preview())
        self.tabs.bind('<<NotebookTabChanged>>', lambda _: self.update_preview())
        self.table = ttk.Treeview(window, columns=('time', 'count', 'status'), show='headings', selectmode='extended')
        for key, title in [('time', '啟動時間'), ('count', '工作數'), ('status', '狀態')]:
            self.table.heading(key, text=tr(title))
        self.table.pack(fill='both', expand=True, padx=8, pady=8)
        def remove():
            selected = set(self.table.selection())
            self.entries[:] = [entry for entry in self.entries if str(id(entry)) not in selected]
            self.refresh()
        ttk.Button(window, text=tr('刪除選取排程'), command=remove).pack(pady=8)
        self.refresh()

    def form_value(self):
        countdown = self.tabs.index(self.tabs.select()) == 0
        try:
            if countdown:
                hours, minutes = int(self.hours.get()), int(self.minutes.get())
                value = hours * 60 + minutes
                if hours < 0 or not 0 <= minutes <= 59 or not 0 < value <= 525600:
                    raise ValueError
                due = datetime.now() + timedelta(minutes=value)
            else:
                hour, minute = int(self.hour.get()), int(self.minute.get())
                if not 0 <= hour <= 23 or not 0 <= minute <= 59:
                    raise ValueError
                value = f'{self.date.get().strip()} {hour:02}:{minute:02}'
                due = datetime.strptime(value, '%Y-%m-%d %H:%M')
        except (ValueError, OverflowError):
            raise ValueError(tr('請輸入有效的日期與時間，倒數須大於 0 且不超過一年。')) from None
        if due <= datetime.now():
            raise ValueError(tr('請設定未來的日期時間。'))
        return countdown, value, due

    def update_preview(self):
        try:
            _, _, due = self.form_value()
            text = tr('預計啟動（本機時間）：{0}').format(due.strftime('%Y-%m-%d %H:%M'))
            if self.queue.running or self.queue.app.manga_busy:
                raise ValueError(tr('請在佇列閒置時新增排程。'))
            enabled = True
        except ValueError as error:
            text, enabled = str(error), False
        self.preview.configure(text=text)
        self.add_button.configure(state='normal' if enabled else 'disabled')

    def refresh(self):
        if self.window is None or not self.window.winfo_exists():
            return
        self.update_preview()
        for entry in self.entries:
            key = str(id(entry))
            values = (entry['due'].strftime('%Y-%m-%d %H:%M:%S'), len(entry['jobs']), tr(entry['status']))
            if self.table.exists(key):
                self.table.item(key, values=values)
            else:
                self.table.insert('', 'end', iid=key, values=values)
        remaining = {str(id(entry)) for entry in self.entries}
        for key in self.table.get_children():
            if key not in remaining:
                self.table.delete(key)
