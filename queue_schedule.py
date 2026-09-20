"""One-shot local schedules; closing the application discards them."""
from datetime import datetime, timedelta
import math
import time
import tkinter as tk
from tkinter import ttk, messagebox
from ui_language import tr


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
                q.cleanup.get(), q.app.komga_path.get(), q.app.skip_unchanged.get())

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
            due = datetime.now() + timedelta(minutes=minutes)
            deadline = time.monotonic() + minutes * 60
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
                jobs = tuple(job for job, signature in entry['jobs']
                             if any(job is current for current in q.jobs) and job.status == 'pending'
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
        window.geometry('760x400')
        ttk.Label(window, text=tr('一次性排程；需保持程式開啟。忙碌時等待，關閉程式即取消。'), wraplength=720).pack(pady=8)
        row = ttk.Frame(window)
        row.pack(fill='x', padx=8)
        mode = tk.StringVar(window, tr('倒數分鐘'))
        ttk.Combobox(row, textvariable=mode, values=[tr('倒數分鐘'), tr('指定日期時間')], state='readonly', width=18).pack(side='left')
        value = tk.StringVar(window, '60')
        ttk.Entry(row, textvariable=value, width=24).pack(side='left', padx=6)
        ttk.Label(window, text=tr('固定時間格式：YYYY-MM-DD HH:MM；使用本機時間。')).pack(anchor='w', padx=8)
        def add():
            try:
                self.add(mode.get() == tr('倒數分鐘'), value.get())
            except (ValueError, OverflowError):
                messagebox.showerror(tr('排程啟動'), tr('請檢查時間格式、範圍，並在佇列閒置時新增。'), parent=window)
        ttk.Button(row, text=tr('新增排程'), command=add).pack(side='left')
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

    def refresh(self):
        if self.window is None or not self.window.winfo_exists():
            return
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
