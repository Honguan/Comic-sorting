"""Queue controls; background work lives in queue_worker and filesystem work in comic_core."""
from pathlib import Path
from datetime import datetime
import sqlite3
import uuid
import queue
import threading
import time
import subprocess
import os
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from queue_worker import BT_STAGES, Job, run_jobs, translator_command
from app_logging import logger, log_path
from ui_language import tr
from bt_settings import ConfigEditor
from comic_core import FOLDER_KINDS, folder_kind, image_files
from queue_history import HistoryWindow, elapsed_text, save_run, usage_text
from queue_errors import error_details, error_info
from ntfy_notifications import NtfyNotifications


ACTIONS = {"translate": "翻譯", "export": "匯出", "cleanup": "清理"}
STATUSES = {"pending": "等待", "running": "執行中", "done": "完成",
            "done_warning": "完成（有異常）",
            "failed": "失敗", "cancelled": "已停止", "blocked": "前置工作失敗"}


class TranslationQueue:
    def __init__(self, app, settings):
        self.app = app
        self.jobs = []
        self.active_jobs = ()
        self.events = queue.Queue()
        self.stop = threading.Event()
        self.pause_requested = threading.Event()
        self.paused_at = None
        self.running = False
        self.started_at = None
        self.history_run = None
        self.history_window = None
        self.history_save_failed = False
        self.notified_jobs = set()
        self.notified_run = None
        self.stage_times = {}
        self.editor = None
        self.config_editor = None
        self.controls = []
        default = settings.get("bt_path", "")
        self.installation = tk.StringVar(value=settings.get("bt_path", default))
        self.config = tk.StringVar(value=settings.get("bt_config", str(Path(default) / "config" / "config.json") if default else ""))
        self.python = tk.StringVar(value=settings.get("bt_python", ""))
        self.action = tk.StringVar(value=tr("翻譯"))
        self.export = tk.BooleanVar(value=settings.get("bt_export", False))
        self.cleanup = tk.BooleanVar(value=settings.get("bt_cleanup", False))
        self.open_after_completion = tk.BooleanVar(value=settings.get("bt_open_after_completion") is True)
        box = ttk.Frame(app.queue_tab, padding=8)
        box.pack(fill="both", expand=True)
        config_box = ttk.LabelFrame(app.settings_tab, text="BallonsTranslator", padding=8)
        config_box.pack(fill="x", padx=8, pady=8)
        for title, variable, directory in ((tr("安裝路徑"), self.installation, True),
                                          (tr("設定檔"), self.config, False),
                                          (tr("Python（空白使用內附環境）"), self.python, False)):
            row = ttk.Frame(config_box)
            row.pack(fill="x", pady=2)
            ttk.Label(row, text=title, width=29).pack(side="left")
            entry = ttk.Entry(row, textvariable=variable)
            entry.pack(side="left", fill="x", expand=True)
            entry.bind("<FocusOut>", lambda _event: self.app.save_settings())
            self.controls.append(entry)
            self.button(row, tr("瀏覽"), lambda v=variable, d=directory: self.browse(v, d))
        settings_actions = ttk.Frame(config_box)
        settings_actions.pack(fill="x", pady=(6, 0))
        self.button(settings_actions, tr("編輯設定檔"), self.edit_settings)
        self.button(settings_actions, tr("開啟原生設定介面"), self.open_settings)
        self.button(settings_actions, tr("開啟紀錄資料夾"), self.open_logs)
        self.notifications = NtfyNotifications(app, settings)
        row = ttk.Frame(box)
        row.pack(fill="x", pady=(0, 4))
        self.action_choice = ttk.Combobox(row, textvariable=self.action,
                                         values=tuple(tr(value) for value in ACTIONS.values()),
                                         state="readonly", width=10)
        self.action_choice.pack(side="left", padx=(0, 4))
        self.button(row, tr("加入上方選取項目"), self.add_selected)
        self.button(row, tr("加入指定路徑"), self.add_directory)
        self.button(row, tr("整合＋加入佇列"), self.integrate)
        self.range_button = self.button(row, tr("翻譯頁數"), self.edit_page_range)
        row = ttk.Frame(box)
        row.pack(fill="x")
        for text, variable in ((tr("翻譯成功後匯出 CBZ"), self.export),
                               (tr("成功後清理 mask / inpainted"), self.cleanup),
                               (tr("完成後開啟資料夾"), self.open_after_completion)):
            check = ttk.Checkbutton(row, text=text, variable=variable, command=app.save_settings)
            check.pack(side="left", padx=(0, 8))
            self.controls.append(check)
        footer = ttk.Frame(box)
        footer.pack(side="bottom", fill="x")
        tree_frame = ttk.Frame(box)
        tree_frame.pack(fill="both", expand=True, pady=4)
        self.tree = ttk.Treeview(tree_frame, columns=("kind", "action", "pages", "status", "error_code", "error_reason"), show="tree headings", height=3)
        for column, text in (("#0", "漫畫路徑"), ("kind", "資料夾類型"), ("action", "動作"), ("pages", "翻譯頁數"),
                             ("status", "狀態"), ("error_code", "錯誤碼"), ("error_reason", "錯誤原因")):
            self.tree.heading(column, text=tr(text))
        self.tree.column("#0", width=340, minwidth=180)
        self.tree.column("kind", width=110, stretch=False)
        self.tree.column("action", width=85, stretch=False)
        self.tree.column("pages", width=120, stretch=False)
        self.tree.column("status", width=110, stretch=False)
        self.tree.column("error_code", width=170, stretch=False)
        self.tree.column("error_reason", width=320, minwidth=180)
        vertical = ttk.Scrollbar(tree_frame, command=self.tree.yview)
        horizontal = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)
        self.tree.bind("<Double-1>", self.show_details)
        self.tree.bind("<<TreeviewSelect>>", lambda _event: self.update_controls())
        self.context_menu = tk.Menu(self.tree, tearoff=False)
        self.context_menu.add_command(label=tr("移除佇列項目"), command=lambda: self.remove(confirm=True))
        self.tree.bind("<Button-3>", self.show_context_menu)
        row = ttk.Frame(footer)
        row.pack(fill="x")
        for text, command in (("移除選取", self.remove), ("上移", lambda: self.move(-1)),
                              ("下移", lambda: self.move(1)), ("重試選取", self.retry),
                              ("清除已完成", self.clear_completed)):
            self.button(row, tr(text), command)
        self.start_button = self.button(row, tr("開始主佇列"), self.start)
        self.pause_button = ttk.Button(row, text=tr("佇列暫停"), command=self.request_pause)
        self.pause_button.pack(side="left", padx=2)
        self.stop_button = ttk.Button(row, text=tr("停止"), command=self.request_stop, width=0)
        self.stop_button.pack(side="left", padx=2)
        self.summary = tk.StringVar()
        ttk.Label(footer, textvariable=self.summary).pack(anchor="w")
        self.total = ttk.Progressbar(footer)
        self.total.pack(fill="x")
        self.label = tk.StringVar(value=tr("BallonsTranslator：待命"))
        self.stage = ttk.Progressbar(footer)
        self.bt_frame, progress_grid, self.bt_toggle = self.collapsible_frame(footer, tr("各階段進度"))
        ttk.Label(self.bt_toggle.master, textvariable=self.label).pack(side="left", fill="x", expand=True, padx=(8, 0))
        progress_grid.columnconfigure(1, weight=1)
        self.bt_bars = {}
        self.bt_labels = {}
        self.bt_progress = {}
        self.bt_elapsed = {}
        self.time_labels = {}
        for row, name in enumerate(BT_STAGES):
            ttk.Label(progress_grid, text=tr(name)).grid(row=row, column=0, sticky="w", padx=(0, 8))
            self.bt_bars[name] = ttk.Progressbar(progress_grid)
            self.bt_bars[name].grid(row=row, column=1, sticky="ew", padx=(0, 8), pady=1)
            self.bt_labels[name] = tk.StringVar()
            ttk.Label(progress_grid, textvariable=self.bt_labels[name]).grid(row=row, column=2, sticky="w")
            self.time_labels[name] = tk.StringVar(value=tr("累計耗時：{0}").format('—'))
            ttk.Label(progress_grid, textvariable=self.time_labels[name]).grid(row=row, column=3, sticky="w", padx=(8, 0))
        self.reset_bt_progress()
        self.usage_records = {}
        self.usage_labels = {}
        self.usage_frame, usage_grid, self.usage_toggle = self.collapsible_frame(
            footer, tr("本次佇列 LLM 消耗（非實際帳單）"))
        self.history_button = ttk.Button(self.usage_toggle.master, text=tr("歷史紀錄"), command=self.open_history, padding=0)
        self.history_button.pack(side='left', padx=(8, 0))
        self.elapsed_label = tk.StringVar(value=tr("總耗時：{0}").format('00:00:00'))
        ttk.Label(usage_grid, textvariable=self.elapsed_label).grid(row=0, column=2, rowspan=3, padx=(12, 0), sticky="ne")
        for row, (scope, title) in enumerate((("OCR", "OCR"), ("translation", "翻譯"), ("total", "合計"))):
            ttk.Label(usage_grid, text=tr(title), width=8).grid(row=row, column=0, sticky="w")
            self.usage_labels[scope] = tk.StringVar()
            ttk.Label(usage_grid, textvariable=self.usage_labels[scope]).grid(row=row, column=1, sticky="w")
        ttk.Label(usage_grid, text=tr("累計本次所有資料夾已回報用量；金額依翻譯器估算。")).grid(row=3, column=0, columnspan=2, sticky="w")
        self.show_usage()
        records = settings.get("bt_jobs", [])
        for record in records if isinstance(records, list) else []:
            if (not isinstance(record, dict) or record.get("action") not in ACTIONS
                    or not isinstance(record.get("path"), str) or not record["path"].strip()):
                continue
            status = record.get("status", "pending")
            error = str(record.get("error", ""))
            if status == "running":
                status, error = "cancelled", tr("上次執行中斷，請確認結果後重試")
            elif status not in STATUSES:
                status = "pending"
            self.jobs.append(Job(Path(record["path"]).resolve(), record["action"], status, error,
                                 record.get("start_page", 1), record.get("end_page"), record.get("range_export") is True))
        self.render()
        self.update_controls()

    def collapsible_frame(self, parent, title):
        frame = ttk.Frame(parent)
        frame.pack(fill="x", pady=(2, 0))
        content = ttk.Frame(frame, padding=(6, 0))
        borrowed_height = 0

        def toggle():
            nonlocal borrowed_height
            panes = self.app.panes
            layout_ready = panes.winfo_ismapped() and panes.sashpos(0) > 0
            if layout_ready:
                panes.update_idletasks()
            sash = panes.sashpos(0)
            old_height = frame.winfo_reqheight()
            collapsing = bool(content.winfo_manager())
            if collapsing:
                content.pack_forget()
                button.configure(text=f"▶ {title}")
            else:
                content.pack(fill="x")
                button.configure(text=f"▼ {title}")
            if not layout_ready:
                # Do not force an intermediate layout while the window is being built.
                borrowed_height = 0
                return
            panes.update_idletasks()
            row_height = int(ttk.Style(self.tree).lookup('Treeview', 'rowheight') or 20)
            if collapsing:
                # Return only space this section actually borrowed, including when
                # sections close out of order or the user has moved the divider.
                tabs = self.app.work_tabs
                minimum_queue = (self.app.queue_tab.winfo_reqheight()
                                 - max(0, int(self.tree['height']) - 1) * row_height
                                 + tabs.winfo_height() - self.app.queue_tab.winfo_height())
                panes.sashpos(0, sash + min(borrowed_height, max(0, tabs.winfo_height() - minimum_queue)))
                borrowed_height = 0
            else:
                manga = panes.nametowidget(panes.panes()[0])
                minimum_manga = manga.winfo_reqheight() - self.app.folder_tree.winfo_reqheight()
                growth = max(0, frame.winfo_reqheight() - old_height)
                target = sash - min(growth, max(0, sash - minimum_manga))
                borrowed_height = sash - panes.sashpos(0, target)

        header = ttk.Frame(frame)
        header.pack(fill="x")
        button = ttk.Button(header, text=f"▶ {title}", command=toggle, padding=0)
        button.pack(side="left")
        return frame, content, button

    def show_usage(self):
        for scope, label in self.usage_labels.items():
            records = [value for (_, kind), value in self.usage_records.items() if kind == scope]
            label.set(usage_text(records))

    def open_history(self):
        if self.history_window and self.history_window.winfo_exists():
            self.history_window.refresh()
            self.history_window.lift()
        else:
            self.history_window = HistoryWindow(self.app.root, self.app.history_path, ACTIONS, STATUSES)

    def save_history(self, refresh=True):
        if self.history_run is None:
            return True
        record = self.history_run
        record['saved_at'] = datetime.now().astimezone().isoformat(timespec='seconds')
        if refresh:
            for index, (job, saved) in enumerate(zip(self.active_jobs, record['jobs'])):
                saved.update(status=job.status, error=job.error,
                             stage_seconds={stage: value for (item, stage), value in self.stage_times.items() if item == index},
                             usage={scope: dict(value, cost=str(value['cost']) if value['cost'] is not None else None)
                                    for (item, scope), value in self.usage_records.items() if item == index})
        try:
            save_run(self.app.history_path, record)
        except (OSError, sqlite3.Error, ValueError) as error:
            logger.exception("queue_history_save_failed path=%s", self.app.history_path)
            self.stop.set()
            if not self.history_save_failed:
                messagebox.showerror(tr("歷史紀錄儲存失敗"),
                                     tr("已要求停止佇列；請檢查歷史檔寫入權限及磁碟空間後重試。\n{0}\n\n{1}").format(self.app.history_path, error))
            self.history_save_failed = True
            return False
        self.history_save_failed = False
        return True

    def button(self, parent, text, command):
        button = ttk.Button(parent, text=text, command=command)
        button.pack(side="left", padx=2)
        self.controls.append(button)
        return button

    def settings(self):
        return dict(bt_path=self.installation.get(), bt_config=self.config.get(),
                    bt_python=self.python.get(), bt_export=self.export.get(), bt_cleanup=self.cleanup.get(),
                    bt_open_after_completion=self.open_after_completion.get(),
                    bt_jobs=[dict(path=str(job.path), action=job.action, status=job.status, error=job.error,
                                  start_page=job.start_page, end_page=job.end_page, range_export=job.range_export)
                             for job in self.jobs], **self.notifications.settings())

    def notify_job(self, index):
        job = self.active_jobs[index]
        if index in self.notified_jobs or job.status not in ('done', 'done_warning', 'failed'):
            return
        self.notified_jobs.add(index)
        record = self.history_run['jobs'][index]
        lines = [f'{job.path.parent.name} / {job.path.name}',
                 f'{tr(ACTIONS[job.action])}: {tr(STATUSES[job.status])}',
                 tr('完成時間：{0}').format(record.get('finished_at') or '—'),
                 tr('總耗時：{0}').format(elapsed_text(record.get('elapsed_seconds')))]
        code, reason = error_info(job.status, job.error)
        if code:
            lines.append(f'{code}: {reason}')
        for scope, title in (('OCR', 'OCR'), ('translation', '翻譯'), ('total', '合計')):
            usage = self.usage_records.get((index, scope))
            lines.append(f'{tr(title)}: {usage_text([usage] if usage else [])}')
        lines.append(tr('預估金額非實際帳單；僅含已回報用量。'))
        failed = job.status != 'done'
        self.notifications.send('failed' if failed else 'success', tr('單項失敗／異常') if failed else tr('單項成功'),
                                '\n'.join(lines), 4 if failed else 3)

    def notify_run(self):
        record = self.history_run
        if record is None or record['id'] == self.notified_run:
            return
        self.notified_run = record['id']
        counts = {status: sum(job.status == status for job in self.active_jobs) for status in STATUSES}
        lines = [tr(STATUSES[record['status']]),
                 tr('完成 {0}｜異常 {1}｜失敗 {2}｜略過 {3}｜停止 {4}｜未執行 {5}').format(
                     counts['done'], counts['done_warning'], counts['failed'], counts['blocked'], counts['cancelled'], counts['pending']),
                 tr('開始時間：{0}').format(record['started_at']),
                 tr('完成時間：{0}').format(record.get('finished_at') or '—'),
                 tr('總耗時：{0}').format(elapsed_text(record.get('elapsed_seconds')))]
        for scope, title in (('OCR', 'OCR'), ('translation', '翻譯'), ('total', '合計')):
            lines.append(f'{tr(title)}: {usage_text([value for (_, kind), value in self.usage_records.items() if kind == scope])}')
        lines.append(tr('預估金額非實際帳單；僅含已回報用量。'))
        if self.history_save_failed:
            lines.append(tr('歷史紀錄儲存失敗'))
        self.notifications.send('done', tr('整批結束'), '\n'.join(lines), 3 if record['status'] == 'done' else 4)

    def reset_bt_progress(self, total=None, enabled=None):
        for name in BT_STAGES:
            active = enabled is None or enabled.get(name, True)
            self.bt_progress[name] = (0, 0 if total is not None else None, total, None, active)
            self.bt_elapsed[name] = None
            self.show_bt_progress(name)

    def show_bt_progress(self, name, terminal=None):
        percent, current, total, eta, active = self.bt_progress[name]
        self.bt_bars[name].configure(value=percent)
        if not active:
            text = tr("未啟用")
        else:
            remaining = eta if eta is not None else tr("估算中")
            if terminal and (current is None or total is None or current < total):
                remaining = tr(terminal)
            text = tr("{0}%｜{1}/{2} 頁｜剩餘 {3}").format(
                percent, current if current is not None else "—", total if total is not None else "—", remaining)
            seconds = self.bt_elapsed[name]
            average = '—'
            if current and seconds is not None:
                # tqdm reports whole seconds; zero elapsed is an upper bound, not zero processing time.
                average = '<1.00' if seconds == 0 else '<0.01' if seconds / current < .01 else f'{seconds / current:.2f}'
            text += '｜' + tr("平均：{0} 秒／頁").format(average)
        self.bt_labels[name].set(text)

    def update_controls(self):
        busy = self.running or self.app.manga_busy
        for control in self.controls:
            control.configure(state="disabled" if busy else "normal")
        self.action_choice.configure(state="disabled" if busy else "readonly")
        resumable = self.running and self.pause_requested.is_set() and not self.stop.is_set()
        self.start_button.configure(state="normal" if resumable or (not busy and any(j.status == "pending" for j in self.jobs)) else "disabled")
        self.pause_button.configure(
            state="normal" if self.running and not self.stop.is_set() and not self.pause_requested.is_set()
            and any(j.status == "pending" for j in self.active_jobs) else "disabled",
            text=tr("已暫停") if self.paused_at is not None else
            tr("等待暫停") if self.pause_requested.is_set() else tr("佇列暫停"))
        self.stop_button.configure(state="normal" if self.running else "disabled")
        selection = self.tree.selection()
        single_translation = len(selection) == 1 and any(str(id(j)) == selection[0] and j.action == "translate" for j in self.jobs)
        self.range_button.configure(state="normal" if not busy and single_translation else "disabled")

    def page_range_text(self, job):
        if job.action != "translate":
            return "—"
        if job.start_page == 1 and job.end_page is None:
            return tr("全部頁面")
        return tr("第 {0}–{1} 頁").format(job.start_page, job.end_page if job.end_page is not None else tr("最後"))

    def render(self):
        view = self.tree.yview()
        focus = self.tree.focus()
        selected = set(self.tree.selection())
        self.tree.delete(*self.tree.get_children())
        self.pending_file_counts = {}
        for job in self.jobs:
            item = str(id(job))
            if job.action == "translate" and job.status == "pending":
                try:
                    self.pending_file_counts[item] = len(job.select_pages(image_files(job.path)))
                except (OSError, ValueError):
                    self.pending_file_counts[item] = None
            self.tree.insert("", "end", iid=item, text=str(job.path),
                             values=(tr(FOLDER_KINDS[folder_kind(job.path.name)]), tr(ACTIONS[job.action]), self.page_range_text(job), tr(STATUSES[job.status]),
                                     *error_info(job.status, job.error)))
            if item in selected:
                self.tree.selection_add(item)
            if item == focus:
                self.tree.focus(item)
        if view:
            self.tree.yview_moveto(view[0])
        self.update_summary()

    def update_summary(self):
        counts = [self.pending_file_counts.get(str(id(job))) for job in self.jobs
                  if job.action == "translate" and job.status == "pending"]
        files = tr("｜待翻譯檔案 {0} 張").format(f"{sum(count for count in counts if count is not None):,}")
        unknown = counts.count(None)
        if unknown:
            files += tr("（{0} 項無法計數）").format(unknown)
        self.summary.set(tr("佇列 {0} 項｜等待 {1}｜完成 {2}｜需處理 {3}").format(
            len(self.jobs), sum(j.status == "pending" for j in self.jobs),
            sum(j.status in ("done", "done_warning") for j in self.jobs),
            sum(j.status in ("failed", "cancelled", "blocked") for j in self.jobs)) + files)

    def browse(self, variable, directory):
        selected = filedialog.askdirectory(initialdir=variable.get() or None) if directory else filedialog.askopenfilename()
        if selected:
            variable.set(selected)
            if variable is self.installation:
                self.config.set(str(Path(selected) / "config" / "config.json"))
                self.python.set("")
            self.app.save_settings()

    def command(self, chapter=None):
        return translator_command(self.installation.get(), self.config.get(), self.python.get(), chapter)

    def open_settings(self):
        if self.app.manga_busy or (self.editor and self.editor.poll() is None):
            return
        if self.config_editor and self.config_editor.winfo_exists():
            self.config_editor.lift()
            return
        try:
            command, root, env = self.command()
            self.editor = subprocess.Popen(command, cwd=root, env=env,
                                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.app.save_settings()
        except (OSError, ValueError) as error:
            logger.exception("translator_settings_failed")
            messagebox.showerror("BallonsTranslator", str(error))

    def edit_settings(self):
        if self.app.manga_busy:
            return
        if self.config_editor and self.config_editor.winfo_exists():
            self.config_editor.lift()
            return
        if self.editor and self.editor.poll() is None:
            messagebox.showinfo("BallonsTranslator", tr("請先在 BallonsTranslator 儲存設定並關閉原生介面"))
            return
        try:
            self.command()
            self.config_editor = ConfigEditor(self.app.root, self.settings())
        except (OSError, ValueError) as error:
            messagebox.showerror(tr("設定檔"), str(error))

    def open_logs(self):
        try:
            log_path().parent.mkdir(parents=True, exist_ok=True)
            os.startfile(log_path().parent)
        except OSError as error:
            messagebox.showerror(tr("開啟紀錄資料夾"), str(error))

    def add_paths(self, paths, action=None):
        if self.app.manga_busy:
            return
        action = action or next(code for code, label in ACTIONS.items() if tr(label) == self.action.get())
        if action not in ACTIONS:
            raise ValueError(action)
        for path in paths:
            path = Path(path).resolve()
            existing = next((job for job in self.jobs if job.path == path and job.action == action), None)
            if existing:
                existing.status, existing.error = "pending", ""
            else:
                self.jobs.append(Job(path, action))
        self.render()
        self.update_controls()
        self.app.work_tabs.select(self.app.queue_tab)
        self.app.save_settings()

    def add_selected(self):
        paths = self.app.selected_chapters()
        if not paths:
            messagebox.showinfo(tr("主佇列"), tr("請先選擇父系列或章節"))
            return
        self.add_paths(paths)

    def add_directory(self):
        path = filedialog.askdirectory()
        if path:
            self.add_paths([path])

    def show_context_menu(self, event):
        if self.tree.identify_region(event.x, event.y) not in ("tree", "cell"):
            return
        item = self.tree.identify_row(event.y)
        if not item:
            return
        if item not in self.tree.selection():
            self.tree.selection_set(item)
        self.tree.focus(item)
        self.context_menu.entryconfigure(0, state="disabled" if self.running or self.app.manga_busy else "normal")
        try:
            self.context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.context_menu.grab_release()
        return "break"

    def remove(self, confirm=False):
        if self.running or self.app.manga_busy:
            return
        selected = set(self.tree.selection())
        if not selected:
            return
        if confirm and not messagebox.askyesno(
                tr("移除佇列項目"),
                tr("確定移除選取的 {0} 個佇列項目？\n漫畫資料夾與檔案會保留。").format(len(selected)),
                parent=self.app.root, default=messagebox.NO):
            return
        self.jobs[:] = [job for job in self.jobs if str(id(job)) not in selected]
        self.changed()

    def clear_completed(self):
        if not self.app.manga_busy:
            self.jobs[:] = [job for job in self.jobs if job.status not in ("done", "done_warning")]
            self.changed()

    def retry(self):
        if not self.app.manga_busy:
            selected = set(self.tree.selection())
            for job in self.jobs:
                if str(id(job)) in selected and job.status in ("failed", "cancelled", "blocked", "done_warning"):
                    job.status, job.error = "pending", ""
            self.changed()

    def move(self, direction):
        if self.app.manga_busy:
            return
        selected = set(self.tree.selection())
        indices = range(len(self.jobs)) if direction < 0 else range(len(self.jobs) - 1, -1, -1)
        for index in indices:
            target = index + direction
            if (str(id(self.jobs[index])) in selected and 0 <= target < len(self.jobs)
                    and str(id(self.jobs[target])) not in selected):
                self.jobs[index], self.jobs[target] = self.jobs[target], self.jobs[index]
        self.changed()

    def changed(self):
        self.render()
        self.update_controls()
        self.app.save_settings()

    def show_details(self, _event=None):
        selected = set(self.tree.selection())
        for job in self.jobs:
            if str(id(job)) in selected:
                messagebox.showinfo(tr("工作詳情"), f"{job.path}\n{tr(ACTIONS[job.action])} / {tr(STATUSES[job.status])}\n{self.page_range_text(job)}\n\n{error_details(job.status, job.error)}")
                break

    def edit_page_range(self):
        selection = self.tree.selection()
        if self.running or self.app.manga_busy or len(selection) != 1:
            return
        job = next((j for j in self.jobs if str(id(j)) == selection[0] and j.action == "translate"), None)
        if job is None:
            return
        images = image_files(job.path)
        if not images:
            messagebox.showerror(tr("翻譯頁數"), tr("指定路徑沒有圖片，請選擇章節或整合輸出"))
            return
        dialog = tk.Toplevel(self.app.root)
        dialog.title(tr("翻譯頁數"))
        dialog.transient(self.app.root)
        dialog.resizable(False, False)
        box = ttk.Frame(dialog, padding=12)
        box.pack(fill="both", expand=True)
        ttk.Label(box, text=job.path.name).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(box, text=tr("共 {0} 頁，依檔名自然排序；包含起訖頁").format(len(images))).grid(row=1, column=0, columnspan=2, sticky="w", pady=6)
        start = tk.StringVar(dialog, value=str(job.start_page))
        end = tk.StringVar(dialog, value="" if job.end_page is None else str(job.end_page))
        for row, title, variable in ((2, "起始頁", start), (3, "結束頁（空白到最後）", end)):
            ttk.Label(box, text=tr(title)).grid(row=row, column=0, sticky="w", pady=3)
            ttk.Spinbox(box, from_=1, to=len(images), textvariable=variable, width=12).grid(row=row, column=1, padx=(12, 0))
        range_export = tk.BooleanVar(dialog, value=job.range_export)
        export_check = ttk.Checkbutton(box, text=tr("指定頁面完成後匯出 CBZ"), variable=range_export)
        export_check.grid(row=4, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Label(box, text=tr("指定頁數專用，預設不匯出；勾選後須全章結果齊全才匯出整章 CBZ。"),
                  wraplength=420).grid(row=5, column=0, columnspan=2, sticky="w")

        def update_export(*_args):
            export_check.configure(state="disabled" if start.get().strip() == "1" and not end.get().strip() else "normal")

        start.trace_add("write", update_export)
        end.trace_add("write", update_export)
        update_export()

        def save():
            try:
                first = int(start.get())
                last = int(end.get()) if end.get().strip() else None
                Job(job.path, "translate", start_page=first, end_page=last).select_pages(images)
            except ValueError:
                messagebox.showerror(tr("翻譯頁數"), tr("翻譯頁數必須介於 1 至 {0}，且起始頁不可大於結束頁").format(len(images)), parent=dialog)
                return
            if (job.start_page, job.end_page, job.range_export) != (first, last, range_export.get()):
                job.start_page, job.end_page, job.range_export = first, last, range_export.get()
                job.status, job.error = "pending", ""
                self.changed()
            dialog.destroy()

        buttons = ttk.Frame(box)
        buttons.grid(row=6, column=0, columnspan=2, pady=(12, 0))
        ttk.Button(buttons, text=tr("全部頁面"), command=lambda: (start.set("1"), end.set(""), range_export.set(False))).pack(side="left", padx=3)
        ttk.Button(buttons, text=tr("套用"), command=save).pack(side="left", padx=3)
        ttk.Button(buttons, text=tr("取消"), command=dialog.destroy).pack(side="left", padx=3)
        dialog.bind("<Return>", lambda _event: save())
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        dialog.grab_set()
        box.grid_slaves(row=2, column=1)[0].focus_set()
        return dialog

    def confirm_cleanup(self):
        needs_cleanup = self.cleanup.get() or any(j.action == "cleanup" and j.status == "pending" for j in self.jobs)
        return not needs_cleanup or messagebox.askyesno(
            tr("確認清理"), tr("佇列將清空指定路徑的 mask / inpainted 內容，無法復原。繼續？"))

    def integrate(self):
        if not self.app.manga_busy:
            self.app.confirm_aggregate(translate_after=True)

    def validate(self, translate=False):
        pending = [job for job in self.jobs if job.status == "pending"]
        error_tab = self.app.queue_tab
        try:
            translating = translate or any(job.action == "translate" for job in pending)
            if translating:
                error_tab = self.app.settings_tab
                if self.editor and self.editor.poll() is None:
                    raise ValueError(tr("請先在 BallonsTranslator 儲存設定並關閉原生介面"))
                if self.config_editor and self.config_editor.winfo_exists():
                    raise ValueError(tr("請先儲存並關閉設定編輯器"))
                self.command()
                error_tab = self.app.queue_tab
            exporting = (translate and self.export.get()) or any(job.should_export(self.export.get()) for job in pending)
            if exporting and not self.app.komga_path.get().strip():
                error_tab = self.app.export_tab
                raise ValueError(tr("請設定 Komga 輸出路徑"))
            previous_failures = set()
            for job in self.jobs:
                if job.status in ("failed", "cancelled", "blocked"):
                    previous_failures.add(job.path)
                if job.status != "pending":
                    continue
                if job.path in previous_failures:
                    raise ValueError(tr("請先重試此路徑的失敗工作：{0}").format(job.path))
                if not job.path.is_dir():
                    raise ValueError(f"{job.path}: {tr('漫畫路徑不存在')}")
                if job.action == "translate":
                    job.select_pages(image_files(job.path))
                if job.should_export(self.export.get()):
                    target = Path(self.app.komga_path.get()).resolve()
                    if target.is_relative_to(job.path) or job.path.is_relative_to(target):
                        raise ValueError(tr("匯出與漫畫路徑不可互相包含"))
            return True
        except (OSError, ValueError) as error:
            logger.exception("queue_validation_failed")
            messagebox.showerror(tr("佇列設定"), str(error))
            self.app.work_tabs.select(error_tab)
            return False

    def request_pause(self):
        if self.running and not self.stop.is_set():
            self.pause_requested.set()
            self.label.set(tr("目前項目完成後暫停後續佇列"))
            self.update_controls()

    def request_stop(self):
        self.stop.set()
        self.update_controls()

    def start(self, cleanup_confirmed=False):
        if self.running and self.pause_requested.is_set() and not self.stop.is_set():
            self.pause_requested.clear()
            self.label.set(tr("佇列繼續處理"))
            self.update_controls()
            return
        if self.running or self.app.manga_busy:
            return
        if self.history_save_failed and not self.save_history(refresh=False):
            return
        self.render()
        self.active_jobs = tuple(job for job in self.jobs if job.status == "pending")
        if not self.active_jobs or not self.validate():
            return
        if not cleanup_confirmed and not self.confirm_cleanup():
            return
        if not self.app.save_settings():
            return
        self.stage_times.clear()
        self.usage_records.clear()
        self.notified_jobs.clear()
        self.history_run = dict(id=uuid.uuid4().hex, started_at=datetime.now().astimezone().isoformat(timespec='seconds'),
                                finished_at=None, elapsed_seconds=None, status='running',
                                jobs=[dict(path=str(job.path), action=job.action, start_page=job.start_page,
                                           end_page=job.end_page, range_export=job.range_export)
                                      for job in self.active_jobs])
        if not self.save_history():
            self.history_run = None
            self.history_save_failed = False
            return
        self.running = True
        self.started_at = time.monotonic()
        self.elapsed_label.set(tr("總耗時：{0}").format('00:00:00'))
        for label in self.time_labels.values():
            label.set(tr("累計耗時：{0}").format('—'))
        self.show_usage()
        self.stop.clear()
        self.pause_requested.clear()
        self.paused_at = None
        self.app.set_manga_busy(True)
        self.app.work_tabs.select(self.app.queue_tab)
        self.total.configure(maximum=len(self.active_jobs), value=0)
        self.stage.configure(value=0)
        self.stage.pack_forget()
        self.reset_bt_progress()
        options = (self.settings(), self.app.komga_path.get(), self.app.skip_unchanged.get())
        threading.Thread(target=run_jobs, args=(self.active_jobs, *options, self.stop, self.events.put),
                         kwargs={'pause': self.pause_requested}, daemon=True).start()
        self.app.root.after(50, self.poll)

    def poll(self):
        if self.running and self.started_at is not None:
            now = time.monotonic() if self.paused_at is None else self.paused_at
            self.elapsed_label.set(tr("總耗時：{0}").format(elapsed_text(now - self.started_at)))
        changed = False
        for _ in range(100):
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            if event[0] == "status":
                job = self.active_jobs[event[1]]
                job.status, job.error = event[2], event[3]
                changed = True
                self.tree.set(str(id(job)), "status", tr(STATUSES[job.status]))
                code, reason = error_info(job.status, job.error)
                self.tree.set(str(id(job)), "error_code", code)
                self.tree.set(str(id(job)), "error_reason", reason)
                self.update_summary()
                self.update_controls()
                if job.status == "running":
                    self.tree.see(str(id(job)))
                    self.stage.configure(value=0)
                    self.stage.pack_forget()
                    self.reset_bt_progress()
                    self.label.set(tr("正在處理：{0}").format(job.path.name))
                elif job.status in ("failed", "cancelled"):
                    for name in BT_STAGES:
                        self.show_bt_progress(name, STATUSES[job.status])
            elif event[0] == "paused":
                self.paused_at = event[1]
                elapsed = self.paused_at - self.started_at
                self.elapsed_label.set(tr("總耗時：{0}").format(elapsed_text(elapsed)))
                self.label.set(tr("佇列已暫停，按「開始主佇列」繼續"))
                if self.history_run is not None:
                    self.history_run['elapsed_seconds'] = elapsed
                    self.save_history()
                self.update_controls()
            elif event[0] == "resumed":
                self.started_at += event[1]
                self.paused_at = None
                if not self.stop.is_set():
                    self.label.set(tr("佇列繼續處理"))
                self.update_controls()
            elif event[0] == "stage_time":
                _, index, name, seconds = event
                self.stage_times[index, name] = max(seconds, self.stage_times.get((index, name), 0))
                total = sum(value for (_, stage), value in self.stage_times.items() if stage == name)
                self.time_labels[name].set(tr("累計耗時：{0}").format(elapsed_text(total)))
                self.bt_elapsed[name] = seconds
                self.show_bt_progress(name)
            elif event[0] == "usage":
                self.usage_records[event[1], event[2]['scope']] = event[2]
                self.show_usage()
            elif event[0] == "job_time" and self.history_run is not None:
                self.history_run['jobs'][event[1]].update(started_at=event[2], finished_at=event[3], elapsed_seconds=event[4])
                self.notify_job(event[1])
            elif event[0] == "run_time":
                if self.history_run is not None:
                    self.history_run.update(started_at=event[1], finished_at=event[2], elapsed_seconds=event[3])
                self.started_at = None
                self.elapsed_label.set(tr("總耗時：{0}").format(elapsed_text(event[3])))
            elif event[0] == "bt_reset":
                self.reset_bt_progress(event[1], event[2])
            elif event[0] == "bt_progress":
                name, percent, current, total, eta = event[1:]
                self.bt_progress[name] = (percent, current, total, eta, True)
                self.bt_elapsed[name] = None  # Only pair page counts with timing from the same progress report.
                self.show_bt_progress(name)
            elif event[0] == "stage":
                self.label.set(f"{tr(event[1])}: {event[2]}%")
                self.stage.pack(fill="x", before=self.bt_frame)
                self.stage.configure(value=event[2])
            elif event[0] == "total":
                self.total.configure(value=event[1])
                if self.history_run is not None:
                    self.history_run['elapsed_seconds'] = time.monotonic() - self.started_at
                    self.save_history()
            elif event[0] == "result":
                if not self.open_after_completion.get():
                    continue
                path = event[1]
                try:
                    if path.is_dir():
                        os.startfile(path)
                    elif path.is_file():
                        subprocess.Popen(f'explorer.exe /select,"{path}"')
                    else:
                        raise FileNotFoundError(path)
                    logger.info("translation_result_opened path=%s", path)
                except OSError as error:
                    logger.exception("translation_result_open_failed path=%s", path)
                    messagebox.showwarning(tr("無法開啟結果位置"), f"{path}\n\n{error}")
            elif event[0] == "done":
                if self.history_run is not None:
                    self.history_run['status'] = ('cancelled' if self.stop.is_set() else 'done'
                                                  if all(job.status == 'done' for job in self.active_jobs) else 'done_warning')
                    self.save_history()
                    self.notify_run()
                self.running = False
                self.pause_requested.clear()
                self.paused_at = None
                self.app.set_manga_busy(False)
                self.label.set(tr("佇列已停止") if self.stop.is_set() else tr("佇列結束，請查看各項狀態"))
                self.app.save_settings()
                if self.app.base_path.get():
                    self.app.load_folders()
                return
        if changed and not self.app.save_settings():
            self.stop.set()
        self.app.root.after(50, self.poll)
