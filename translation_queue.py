"""Queue controls; background work lives in queue_worker and filesystem work in comic_core."""
from pathlib import Path
import queue
import threading
import subprocess
import os
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from queue_worker import Job, run_jobs, translator_command
from app_logging import logger, log_path
from ui_language import tr


ACTIONS = {"translate": "翻譯", "export": "匯出", "cleanup": "清理"}
STATUSES = {"pending": "等待", "running": "執行中", "done": "完成",
            "failed": "失敗", "cancelled": "已停止", "blocked": "前置工作失敗"}


class TranslationQueue:
    def __init__(self, app, settings):
        self.app = app
        self.jobs = []
        self.active_jobs = ()
        self.events = queue.Queue()
        self.stop = threading.Event()
        self.running = False
        self.editor = None
        self.controls = []
        default = settings.get("bt_path", "")
        self.installation = tk.StringVar(value=settings.get("bt_path", default))
        self.config = tk.StringVar(value=settings.get("bt_config", str(Path(default) / "config" / "config.json") if default else ""))
        self.python = tk.StringVar(value=settings.get("bt_python", ""))
        self.action = tk.StringVar(value=tr("翻譯"))
        self.export = tk.BooleanVar(value=settings.get("bt_export", False))
        self.cleanup = tk.BooleanVar(value=settings.get("bt_cleanup", False))
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
        self.button(config_box, tr("開啟原生設定介面"), self.open_settings)
        self.button(config_box, tr("開啟紀錄資料夾"), self.open_logs)
        row = ttk.Frame(box)
        row.pack(fill="x", pady=(0, 4))
        self.action_choice = ttk.Combobox(row, textvariable=self.action,
                                         values=tuple(tr(value) for value in ACTIONS.values()),
                                         state="readonly", width=10)
        self.action_choice.pack(side="left", padx=(0, 4))
        self.button(row, tr("加入上方選取項目"), self.add_selected)
        self.button(row, tr("加入指定路徑"), self.add_directory)
        self.button(row, tr("一鍵整合＋主佇列"), self.integrate)
        row = ttk.Frame(box)
        row.pack(fill="x")
        for text, variable in ((tr("翻譯成功後匯出 CBZ"), self.export),
                               (tr("成功後清理 mask / inpainted"), self.cleanup)):
            check = ttk.Checkbutton(row, text=text, variable=variable, command=app.save_settings)
            check.pack(side="left", padx=(0, 8))
            self.controls.append(check)
        footer = ttk.Frame(box)
        footer.pack(side="bottom", fill="x")
        tree_frame = ttk.Frame(box)
        tree_frame.pack(fill="both", expand=True, pady=4)
        self.tree = ttk.Treeview(tree_frame, columns=("action", "status"), show="tree headings", height=5)
        for column, text in (("#0", "漫畫路徑"), ("action", "動作"), ("status", "狀態")):
            self.tree.heading(column, text=tr(text))
        self.tree.column("#0", width=480)
        self.tree.column("action", width=110, stretch=False)
        self.tree.column("status", width=140, stretch=False)
        vertical = ttk.Scrollbar(tree_frame, command=self.tree.yview)
        horizontal = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)
        self.tree.bind("<Double-1>", self.show_details)
        row = ttk.Frame(footer)
        row.pack(fill="x")
        for text, command in (("移除選取", self.remove), ("上移", lambda: self.move(-1)),
                              ("下移", lambda: self.move(1)), ("重試選取", self.retry),
                              ("清除已完成", self.clear_completed)):
            self.button(row, tr(text), command)
        self.start_button = self.button(row, tr("開始主佇列"), self.start)
        self.stop_button = ttk.Button(row, text=tr("停止"), command=self.stop.set)
        self.stop_button.pack(side="left", padx=2)
        self.summary = tk.StringVar()
        ttk.Label(footer, textvariable=self.summary).pack(anchor="w")
        self.total = ttk.Progressbar(footer)
        self.total.pack(fill="x")
        self.label = tk.StringVar(value=tr("BallonsTranslator：待命"))
        ttk.Label(footer, textvariable=self.label).pack(anchor="w")
        self.stage = ttk.Progressbar(footer)
        self.stage.pack(fill="x")
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
            self.jobs.append(Job(Path(record["path"]).resolve(), record["action"], status, error))
        self.render()
        self.update_controls()

    def button(self, parent, text, command):
        button = ttk.Button(parent, text=text, command=command)
        button.pack(side="left", padx=2)
        self.controls.append(button)
        return button

    def settings(self):
        return dict(bt_path=self.installation.get(), bt_config=self.config.get(),
                    bt_python=self.python.get(), bt_export=self.export.get(), bt_cleanup=self.cleanup.get(),
                    bt_jobs=[dict(path=str(job.path), action=job.action, status=job.status, error=job.error)
                             for job in self.jobs])

    def update_controls(self):
        busy = self.running or self.app.manga_busy
        for control in self.controls:
            control.configure(state="disabled" if busy else "normal")
        self.action_choice.configure(state="disabled" if busy else "readonly")
        self.start_button.configure(state="normal" if not busy and any(j.status == "pending" for j in self.jobs) else "disabled")
        self.stop_button.configure(state="normal" if self.running else "disabled")

    def render(self):
        view = self.tree.yview()
        focus = self.tree.focus()
        selected = set(self.tree.selection())
        self.tree.delete(*self.tree.get_children())
        for job in self.jobs:
            item = str(id(job))
            self.tree.insert("", "end", iid=item, text=str(job.path),
                             values=(tr(ACTIONS[job.action]), tr(STATUSES[job.status])))
            if item in selected:
                self.tree.selection_add(item)
            if item == focus:
                self.tree.focus(item)
        if view:
            self.tree.yview_moveto(view[0])
        self.update_summary()

    def update_summary(self):
        self.summary.set(tr("佇列 {0} 項｜等待 {1}｜完成 {2}｜需處理 {3}").format(
            len(self.jobs), sum(j.status == "pending" for j in self.jobs),
            sum(j.status == "done" for j in self.jobs),
            sum(j.status in ("failed", "cancelled", "blocked") for j in self.jobs)))

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
        try:
            command, root, env = self.command()
            self.editor = subprocess.Popen(command, cwd=root, env=env,
                                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.app.save_settings()
        except (OSError, ValueError) as error:
            logger.exception("translator_settings_failed")
            messagebox.showerror("BallonsTranslator", str(error))

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

    def remove(self):
        if not self.app.manga_busy:
            selected = set(self.tree.selection())
            self.jobs[:] = [job for job in self.jobs if str(id(job)) not in selected]
            self.changed()

    def clear_completed(self):
        if not self.app.manga_busy:
            self.jobs[:] = [job for job in self.jobs if job.status != "done"]
            self.changed()

    def retry(self):
        if not self.app.manga_busy:
            selected = set(self.tree.selection())
            for job in self.jobs:
                if str(id(job)) in selected and job.status in ("failed", "cancelled", "blocked"):
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
                messagebox.showinfo(tr("工作詳情"), f"{job.path}\n{tr(ACTIONS[job.action])} / {tr(STATUSES[job.status])}\n\n{job.error}")
                break

    def confirm_cleanup(self):
        needs_cleanup = self.cleanup.get() or any(j.action == "cleanup" and j.status == "pending" for j in self.jobs)
        return not needs_cleanup or messagebox.askyesno(
            tr("確認清理"), tr("佇列將清空指定路徑的 mask / inpainted 內容，無法復原。繼續？"))

    def integrate(self):
        if not self.app.manga_busy and self.validate(True) and self.confirm_cleanup():
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
                self.command()
                error_tab = self.app.queue_tab
            exporting = (translating and self.export.get()) or any(job.action == "export" for job in pending)
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
                if job.action == "export" or (job.action == "translate" and self.export.get()):
                    target = Path(self.app.komga_path.get()).resolve()
                    if target.is_relative_to(job.path) or job.path.is_relative_to(target):
                        raise ValueError(tr("匯出與漫畫路徑不可互相包含"))
            return True
        except (OSError, ValueError) as error:
            logger.exception("queue_validation_failed")
            messagebox.showerror(tr("佇列設定"), str(error))
            self.app.work_tabs.select(error_tab)
            return False

    def start(self, cleanup_confirmed=False):
        if self.running or self.app.manga_busy:
            return
        self.active_jobs = tuple(job for job in self.jobs if job.status == "pending")
        if not self.active_jobs or not self.validate():
            return
        if not cleanup_confirmed and not self.confirm_cleanup():
            return
        if not self.app.save_settings():
            return
        self.running = True
        self.stop.clear()
        self.app.set_manga_busy(True)
        self.app.work_tabs.select(self.app.queue_tab)
        self.total.configure(maximum=len(self.active_jobs), value=0)
        self.stage.configure(value=0)
        options = (self.settings(), self.app.komga_path.get(), self.app.skip_unchanged.get())
        threading.Thread(target=run_jobs, args=(self.active_jobs, *options, self.stop, self.events.put), daemon=True).start()
        self.app.root.after(50, self.poll)

    def poll(self):
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
                self.update_summary()
                if job.status == "running":
                    self.tree.see(str(id(job)))
                    self.stage.configure(value=0)
                    self.label.set(tr("正在處理：{0}").format(job.path.name))
            elif event[0] == "stage":
                self.label.set(f"{tr(event[1])}: {event[2]}%")
                self.stage.configure(value=event[2])
            elif event[0] == "total":
                self.total.configure(value=event[1])
            elif event[0] == "done":
                self.running = False
                self.app.set_manga_busy(False)
                self.label.set(tr("佇列已停止") if self.stop.is_set() else tr("佇列結束，請查看各項狀態"))
                self.app.save_settings()
                if self.app.base_path.get():
                    self.app.load_folders()
                return
        if changed and not self.app.save_settings():
            self.stop.set()
        self.app.root.after(50, self.poll)
