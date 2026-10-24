"""Queue controls; background work lives in queue_worker and filesystem work in comic_core."""
from pathlib import Path
from datetime import datetime
import sqlite3
import uuid
import queue
import threading
import time
import math
import subprocess
import os
import tkinter as tk
from tkinter import filedialog, messagebox, ttk, font as tkfont
from ui_interactions import bind_search_shortcuts, bind_table_shortcuts, copy_manga_names, details_text

from queue_worker import BT_STAGES, Job, run_jobs, translator_command
from app_logging import logger, log_path
from ui_language import tr
from queue_stats import SnapshotTotals
from model_pricing import open_pricing
from comic_core import FOLDER_KINDS, folder_kind, image_files, image_count
from queue_history import HistoryWindow, elapsed_text, save_run, usage_text, usage_columns, JEV_FIELDS, jev_totals, jev_columns, create_model_table, fill_model_usage
from queue_errors import error_details, error_info
from ntfy_notifications import NtfyNotifications
from queue_start_dialog import QueueStartDialog
from queue_schedule import QueueSchedule
from queue_phases import MODES, READY, MARKER, prepared


ACTIONS = {"translate": "翻譯", "export": "匯出", "cleanup": "清理"}
STATUSES = {"pending": "待執行", "running": "執行中", "done": "完成",
            "prepared": "待翻譯", "skipped": "已略過",
            "done_warning": "完成需檢查",
            "failed": "失敗", "cancelled": "已停止", "blocked": "未執行"}


class TranslationQueue:
    def __init__(self, app, settings):
        self.app = app
        self.jobs = []
        self.active_jobs = ()
        self.events = queue.Queue()
        self.count_cache = {}
        self.count_results = queue.Queue()
        self.counting = False
        self.count_generation = 0
        self.count_after = None
        app.root.bind("<Destroy>", self.close_counts, add="+")
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
        self.retry_count = 0
        self.phase_jobs = {}
        self.editor = None
        self.controls = []
        self.token_rates = dict(settings.get('bt_token_rates', {}))
        default = settings.get("bt_path", "")
        self.installation = tk.StringVar(value=settings.get("bt_path", default))
        self.config = tk.StringVar(value=settings.get("bt_config", str(Path(default) / "config" / "config.json") if default else ""))
        self.python = tk.StringVar(value=settings.get("bt_python", ""))
        self.action = tk.StringVar(value=tr("翻譯"))
        self.workflow = tk.StringVar(value=tr(MODES.get(settings.get("bt_workflow"), MODES["normal"])))
        self.export = tk.BooleanVar(value=settings.get("bt_export", False))
        self.cleanup = tk.BooleanVar(value=settings.get("bt_cleanup", False) and self.export.get())
        self.open_after_completion = tk.BooleanVar(value=settings.get("bt_open_after_completion") is True)
        self.build_settings_panel(settings)
        self.build_queue_panel()
        self.restore_jobs(settings.get("bt_jobs", []))
        self.render()
        self.update_controls()

    def build_settings_panel(self, settings):
        app = self.app
        config_box = ttk.LabelFrame(app.settings_tab, text="BallonsTranslator", padding=8)
        config_box.pack(fill="x", padx=8, pady=8)
        for title, variable, directory in ((tr("安裝路徑"), self.installation, True),
                                          (tr("設定檔"), self.config, False),
                                          (tr("Python（空白使用內附環境）"), self.python, False)):
            row = ttk.Frame(config_box)
            row.pack(fill="x", pady=2)
            ttk.Label(row, text=title).pack(anchor="w")
            field = ttk.Frame(row)
            field.pack(fill="x")
            entry = ttk.Entry(field, textvariable=variable, width=8)
            entry.bind("<FocusOut>", lambda _event: self.app.save_settings())
            self.controls.append(entry)
            self.button(field, tr("瀏覽"), lambda v=variable, d=directory: self.browse(v, d)).pack_configure(side="right")
            entry.pack(side="left", fill="x", expand=True)
        settings_actions = ttk.Frame(config_box)
        settings_actions.pack(fill="x", pady=(6, 0))
        self.button(settings_actions, tr("開啟原生設定介面"), self.open_settings)
        self.button(settings_actions, tr("開啟紀錄資料夾"), self.open_logs)
        self.button(settings_actions, tr('模型單價'), lambda: open_pricing(app.root, self.token_rates, app.save_settings))
        self.notifications = NtfyNotifications(app, settings)
        self.scheduler = QueueSchedule(self)

    def build_queue_panel(self):
        app = self.app
        box = ttk.Frame(app.queue_tab, padding=8)
        box.pack(fill="both", expand=True)
        app.build_enqueue_panel(box)
        row = app.enqueue_header
        self.action_choice = ttk.Combobox(app.enqueue_actions, textvariable=self.action,
            values=tuple(tr(value) for value in ACTIONS.values()), state="readonly", width=8)
        self.action_choice.pack(side="left", fill="x", expand=True, padx=(0, 8))
        # Menu-only commands; update_controls fills action_states and menus read it when opened.
        self.actions = {"翻譯頁數": self.edit_page_range}
        self.action_states = {}
        self.menu_states = []
        options = ttk.Menubutton(row, text=tr("選項"))
        options.pack(side="right")
        options_menu = tk.Menu(options, tearoff=False)
        self.action_entry(options_menu, "翻譯頁數")
        def sync_options():
            self.sync_menu(options_menu)
            options_menu.entryconfigure(2, state="normal" if self.export.get() else "disabled")
        options_menu.configure(postcommand=sync_options)
        for text, variable in (("翻譯成功後匯出 CBZ", self.export),
                               ("匯出驗證後清理快取", self.cleanup),
                               ("完成後開啟資料夾", self.open_after_completion)):
            options_menu.add_checkbutton(label=tr(text), variable=variable, command=self.output_options_changed)
        options_menu.add_separator()
        options_menu.add_command(label=tr("加入指定路徑"), command=self.add_directory)
        options_menu.add_command(label=tr("章節整合"), command=app.open_aggregate)
        options.configure(menu=options_menu)
        self.options_menu = options_menu
        self.controls.extend((options, app.add_range_button, app.add_selected_button, app.aggregate_translate_button))

        footer = ttk.Frame(box)
        footer.pack(side="bottom", fill="x")
        search_row = ttk.Frame(box)
        search_row.pack(fill="x", pady=(4, 0))
        ttk.Label(search_row, text=tr("搜尋")).pack(side="left", padx=(0, 6))
        self.search = tk.StringVar()
        self.search_after = None
        self.search_entry = ttk.Entry(search_row, textvariable=self.search)
        self.search_entry.pack(side="left", fill="x", expand=True)
        self.search_entry.configure(width=8)
        ttk.Button(search_row, text=tr("清除"), width=0, command=lambda: self.search.set("")).pack(side="left", padx=4)
        self.search_count = tk.StringVar()
        ttk.Label(search_row, textvariable=self.search_count).pack(side="left")
        self.queue_restore_ratio = None
        self.last_library_ratio = None
        self.sash_after = None
        self.sash_velocity = 0.0
        app.panes.bind("<ButtonPress-1>", self.cancel_sash_animation, add="+")
        app.panes.bind("<Configure>", lambda _event: app.panes.sashpos(0, 0)
                       if self.queue_restore_ratio is not None and self.sash_after is None else None, add="+")

        tree_frame = ttk.Frame(box)
        tree_frame.pack(fill="both", expand=True, pady=4)
        self.tree = ttk.Treeview(tree_frame, columns=("kind", "action", "pages", "status", "error_reason"), show="tree headings", height=3)
        for column, text in (("#0", "漫畫 / 章節"), ("kind", "資料夾類型"), ("action", "動作"), ("pages", "翻譯頁數"),
                             ("status", "狀態"), ("error_reason", "錯誤原因")):
            self.tree.heading(column, text=tr(text))
        self.tree.column("#0", width=340, minwidth=40, stretch=False)
        self.tree.column("kind", width=110, stretch=False)
        self.tree.column("action", width=85, stretch=False)
        self.tree.column("pages", width=120, stretch=False)
        self.tree.column("status", width=110, stretch=False)
        for status, color in (("failed", "#b42318"), ("blocked", "#b42318"),
                              ("done", "#166534"), ("done_warning", "#92400e")):
            self.tree.tag_configure(status, foreground=color)
        self.tree.column("error_reason", width=220, minwidth=40, stretch=False)
        vertical = ttk.Scrollbar(tree_frame, command=self.tree.yview)
        horizontal = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)
        self.selection_details = tk.StringVar()
        _details, self.selection_label = details_text(tree_frame, self.selection_details,
                                                      row=2, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        self.tree.bind("<Configure>", self.fit_columns)
        self.tree.bind("<Double-1>", self.show_details)
        self.tree.bind("<<TreeviewSelect>>", lambda _event: self.update_controls())
        self.search.trace_add("write", lambda *_args: self.filter_jobs())
        copy_paths = bind_table_shortcuts(self.tree, self.selected_paths)
        bind_search_shortcuts(self.search_entry, self.search, self.apply_queue_filter, self.tree)
        idle = lambda: "disabled" if self.running or self.app.manga_busy else "normal"
        self.context_menu = tk.Menu(self.tree, tearoff=False)
        self.menu_entry(self.context_menu, tr("移除佇列項目"), lambda: self.remove(confirm=True), idle)
        self.tree.bind("<Button-3>", self.show_context_menu)
        row = ttk.Frame(footer)
        row.pack(fill="x")
        details_row = ttk.Frame(footer)
        self.workflow_group = workflow_row = ttk.Frame(row)
        ttk.Label(workflow_row, text=tr("流程")).pack(side="left", padx=(0, 8))
        self.workflow_choice = ttk.Menubutton(workflow_row, textvariable=self.workflow)
        # Natural width keeps the arrow beside the label text so it reads as a menu.
        self.workflow_choice.pack(side="left", padx=(4, 0))
        self.actions.update({"移除選取": self.remove, "上移": lambda: self.move(-1),
                             "下移": lambda: self.move(1),
                             "移到最上": lambda: self.move(-1, to_edge=True),
                             "移到最下": lambda: self.move(1, to_edge=True), "重試": self.retry,
                             "清除已完成": self.clear_completed, "取消選取": self.clear_selection})
        self.operations_button = ttk.Menubutton(details_row, text=tr("操作"))
        self.operations_button.pack(side="left")
        self.operations_menu = tk.Menu(self.operations_button, tearoff=False)
        for label in ("移除選取", "上移", "下移", "移到最上", "移到最下", "重試", "清除已完成", "取消選取"):
            self.action_entry(self.operations_menu, label)
        self.menu_entry(self.operations_menu, tr("排程啟動"), self.scheduler.show, lambda: "normal")
        self.menu_entry(self.operations_menu, tr("調整範圍與順序…"), lambda: self.start(configure=True), idle)
        self.operations_menu.configure(postcommand=lambda: self.sync_menu(self.operations_menu))
        self.operations_button.configure(menu=self.operations_menu)

        for label in ("重試", "上移", "下移", "移到最上", "移到最下", "翻譯頁數"):
            self.action_entry(self.context_menu, label)
        self.context_menu.add_separator()
        self.context_menu.add_command(label=tr("複製完整路徑"), accelerator="Ctrl+C", command=copy_paths)
        self.start_button = self.button(row, tr("開始執行"), self.start)
        self.start_button.configure(style="Accent.TButton")

        self.workflow_menu = tk.Menu(self.workflow_choice, tearoff=False)
        for value in MODES.values():
            self.workflow_menu.add_radiobutton(label=tr(value), value=tr(value), variable=self.workflow,
                                               command=self.workflow_changed)
        self.workflow_choice.configure(menu=self.workflow_menu)

        self.menu_entry(self.context_menu, tr("執行選取項目"), self.start,
                        lambda: "normal" if idle() == "normal" and self.start_targets() else "disabled")
        self.run_menu_index = self.context_menu.index("end")
        self.context_menu.add_command(label=tr("取消選取"), command=self.clear_selection)
        self.context_menu.add_command(label=tr("複製漫畫名稱"), command=lambda: copy_manga_names(
            self.tree, (path.parent for path in self.selected_paths())))
        self.pause_button = ttk.Button(row, text=tr("暫停"), command=self.request_pause, width=0)
        self.pause_button.pack(side="left", padx=2)
        self.stop_button = ttk.Button(row, text=tr("停止"), command=self.request_stop, width=0)
        self.stop_button.pack(side="left", padx=2)
        self.action_entry(self.context_menu, "清除已完成")
        self.summary = tk.StringVar()
        self.summary_preview = tk.StringVar()
        summary_label = ttk.Label(footer, textvariable=self.summary_preview, wraplength=650, style="Muted.TLabel")
        summary_label.pack(fill="x", pady=(2, 0))
        summary_label.bind("<Configure>", lambda event: summary_label.configure(wraplength=event.width)
                           if event.width > 100 and self.sash_after is None else None)
        self.total = ttk.Progressbar(footer)
        self.total.pack(fill="x")
        self.label = tk.StringVar(value=tr("BallonsTranslator：待命"))
        self.stage = ttk.Progressbar(footer)
        elapsed_row = ttk.Frame(footer)
        elapsed_row.pack(fill="x", pady=(2, 0))
        details_row.pack(fill="x")
        progress_grid, self.toggle_progress = self.popup_panel(tr("各階段進度"))
        stage_label = ttk.Label(elapsed_row, textvariable=self.label, width=1, wraplength=300)
        stage_label.pack(side="left", fill="x", expand=True, padx=(0, 8))
        stage_label.bind("<Configure>", lambda event: stage_label.configure(wraplength=event.width)
                         if event.width > 100 and self.sash_after is None else None)
        self.resize_widgets = (app.folder_tree, self.tree, app.scan_status_label, summary_label, stage_label)
        progress_grid.columnconfigure(1, weight=1)
        self.bt_bars = {}
        self.bt_labels = {}
        self.bt_progress = {}
        self.bt_elapsed = {}
        self.bt_terminal = {}
        self.bt_job_indices = {}
        self.time_labels = {}
        for row, name in enumerate(BT_STAGES):
            line = row * 3
            ttk.Label(progress_grid, text=tr(name)).grid(row=line, column=0, sticky="w", padx=(0, 8))
            self.bt_bars[name] = ttk.Progressbar(progress_grid)
            self.bt_bars[name].grid(row=line + 1, column=0, columnspan=2, sticky="ew", pady=(2, 0))
            self.bt_labels[name] = tk.StringVar()
            detail = ttk.Label(progress_grid, textvariable=self.bt_labels[name], width=1, wraplength=700)
            detail.grid(row=line + 2, column=0, columnspan=2, sticky="ew", pady=(0, 8))
            detail.bind("<Configure>", lambda event, label=detail: label.configure(wraplength=event.width)
                        if event.width > 100 else None)
            self.time_labels[name] = tk.StringVar(value=tr("累計耗時：{0}").format('—'))
            ttk.Label(progress_grid, textvariable=self.time_labels[name], style="Muted.TLabel").grid(
                row=line, column=1, sticky="e", padx=(8, 0))
        self.reset_bt_progress()
        self.usage_records = SnapshotTotals(lambda key: key[1])
        self.jev_records = SnapshotTotals()
        self.jev_window = None
        self.model_window = None
        self.usage_labels = {}
        usage_grid, self.toggle_usage = self.popup_panel(tr("本次佇列 LLM 消耗（非實際帳單）"))
        self.elapsed_label = tk.StringVar(value='00:00:00')
        ttk.Label(elapsed_row, text=tr("本次耗時") + "：").pack(side="left")
        ttk.Label(elapsed_row, textvariable=self.elapsed_label).pack(side="left")
        self.jev_elapsed_label = tk.StringVar(value='—')
        self.history_button = ttk.Button(details_row, text=tr("歷史紀錄"), command=self.open_history, width=0)
        self.stats_button = ttk.Button(details_row, text=tr("模型統計"), command=self.open_model_stats, width=0)
        self.progress_button = ttk.Button(details_row, text=tr("進度"), command=self.toggle_progress, width=0)
        self.view_button = ttk.Menubutton(details_row, text=tr("檢視"))
        self.navigation_buttons = (self.operations_button, self.history_button, self.stats_button,
                                   self.progress_button, self.view_button)
        self.footer_buttons = (self.workflow_group, self.start_button, self.pause_button, self.stop_button)
        for button in (*self.footer_buttons, self.operations_button):
            button.pack_forget()
        self.view_menu = tk.Menu(self.view_button, tearoff=False)
        self.view_menu.add_command(label=tr("LLM 用量"), command=self.toggle_usage)
        self.view_menu.add_command(label=tr("Jev 統計"), command=self.open_jev_stats)
        self.view_menu.add_command(label=tr("佇列摘要"), command=lambda: messagebox.showinfo(
            tr("佇列摘要"), self.summary.get().replace("｜", "\n").replace(" | ", "\n")))
        self.view_menu.add_separator()
        self.view_menu.add_command(label=tr("放大佇列"), command=self.toggle_queue_size)
        self.view_menu.configure(postcommand=lambda: self.view_menu.entryconfigure(
            "end", label=tr("放大佇列") if self.queue_restore_ratio is None else tr("還原佇列大小")))
        self.view_button.configure(menu=self.view_menu)

        columns = ('scope', 'tokens', 'cost', 'requests', 'pricing', 'reporting')
        self.usage_table = ttk.Treeview(usage_grid, columns=columns, show='headings', height=3)
        for key, title, width in zip(columns, ('項目', 'Token 數', '預估金額（USD）', '請求數', '金額完整性', 'Token 完整性'), (70, 130, 130, 80, 145, 145)):
            self.usage_table.heading(key, text=tr(title))
            self.usage_table.column(key, width=width, minwidth=40, anchor='w' if key in ('scope', 'pricing', 'reporting') else 'e')
        self.usage_table.grid(row=1, column=0, columnspan=2, sticky='ew')
        usage_scroll = ttk.Scrollbar(usage_grid, command=self.usage_table.yview)
        self.usage_table.configure(yscrollcommand=usage_scroll.set)
        usage_scroll.grid(row=1, column=2, sticky='ns')
        usage_grid.columnconfigure(1, weight=1)
        for scope in ('OCR', 'Jev', 'translation', 'total'):
            self.usage_labels[scope] = tk.StringVar()
        self.show_usage()

    def restore_jobs(self, records):
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

    def toggle_queue_size(self):
        panes = self.app.panes
        if self.queue_restore_ratio is None:
            if self.sash_after is None:
                self.last_library_ratio = panes.sashpos(0) / max(1, panes.winfo_width())
            self.queue_restore_ratio = self.last_library_ratio or .30
            target = 0
        else:
            target = round(self.queue_restore_ratio * panes.winfo_width())
            self.queue_restore_ratio = None
        self.animate_sash(target)

    def finish_sash_layout(self):
        for widget in self.resize_widgets:
            widget.event_generate("<Configure>", width=widget.winfo_width())

    def cancel_sash_animation(self, _event=None):
        if self.sash_after is not None:
            self.app.root.after_cancel(self.sash_after)
            self.sash_after = None
        self.sash_velocity = 0.0
        self.queue_restore_ratio = None
        self.finish_sash_layout()

    def animate_sash(self, target):
        if self.sash_after is not None:
            self.app.root.after_cancel(self.sash_after)
        else:
            self.sash_velocity = 0.0
        panes = self.app.panes
        position, previous = float(panes.sashpos(0)), time.monotonic()
        ratio = target / max(1, panes.winfo_width())
        if not panes.winfo_ismapped():
            panes.sashpos(0, target)
            self.sash_after = None
            self.sash_velocity = 0.0
            return
        def step():
            nonlocal position, previous
            now = time.monotonic()
            delta, previous = max(0, now - previous), now
            goal = round(ratio * panes.winfo_width())
            # Critically damped motion preserves velocity when the target reverses.
            offset = position - goal
            impulse = self.sash_velocity + 34 * offset
            decay = math.exp(-34 * delta)
            position = goal + (offset + impulse * delta) * decay
            self.sash_velocity = (self.sash_velocity - 34 * impulse * delta) * decay
            bounded = min(panes.winfo_width(), max(0, position))
            if bounded != position:
                position, self.sash_velocity = bounded, 0.0
            if abs(position - goal) < .5 and abs(self.sash_velocity) < 12:
                panes.sashpos(0, goal)
                self.sash_after = None
                self.sash_velocity = 0.0
                self.finish_sash_layout()
                return
            if panes.sashpos(0) != round(position):
                panes.sashpos(0, round(position))
            delay = max(1, 16 - round((time.monotonic() - now) * 1000))
            self.sash_after = self.app.root.after(delay, step)
        step()

    def popup_panel(self, title):
        """Popup content and the function that shows or hides it."""
        popup = tk.Toplevel(self.app.root)
        popup.title(title)
        popup.geometry("1000x380")
        popup.minsize(760, 340)
        popup.withdraw()
        content = ttk.Frame(popup, padding=12)
        def toggle():
            if content.winfo_manager():
                content.pack_forget()
                popup.withdraw()
            else:
                content.pack(fill="both", expand=True)
                popup.deiconify()
                popup.lift()
        popup.protocol("WM_DELETE_WINDOW", toggle)
        return content, toggle



    def open_model_stats(self):
        if self.model_window is None or not self.model_window.winfo_exists():
            self.model_window = tk.Toplevel(self.app.root)
            self.model_window.title(tr('模型統計'))
            self.model_window.geometry('1200x480')
            frame = ttk.Frame(self.model_window, padding=8)
            frame.pack(fill='both', expand=True)
            self.model_usage_table = create_model_table(frame)
        self.show_usage()
        self.model_window.deiconify()
        self.model_window.lift()


    def open_jev_stats(self):
        if self.jev_window is None or not self.jev_window.winfo_exists():
            self.jev_window = tk.Toplevel(self.app.root)
            self.jev_window.title(tr('Jev 統計'))
            self.jev_window.geometry('560x480')
            self.jev_table = ttk.Treeview(self.jev_window, columns=('field', 'value'), show='headings', height=len(JEV_FIELDS))
            for key, title in (('field', '欄位'), ('value', '資料')):
                self.jev_table.heading(key, text=tr(title))
                self.jev_table.column(key, width=200, minwidth=80)
            self.jev_table.pack(fill='both', expand=True, padx=8, pady=8)
        self.show_usage()
        self.jev_window.deiconify()
        self.jev_window.lift()

    def show_usage(self):
        stats = self.jev_records.aggregate()
        self.jev_elapsed_label.set(elapsed_text(stats.get('elapsed_seconds')))
        if self.jev_window is not None and self.jev_window.winfo_exists():
            self.jev_table.delete(*self.jev_table.get_children())
            for label, value in zip(JEV_FIELDS.values(), jev_columns(stats)):
                self.jev_table.insert('', 'end', values=(tr(label), value))
        model_records = {}
        for scope, label in self.usage_labels.items():
            summary = self.usage_records.aggregate(scope)
            records = [summary] if summary else []
            model_records[scope] = records
            label.set(usage_text(records))
            if scope == 'Jev' and not records:
                if self.usage_table.exists(scope):
                    self.usage_table.delete(scope)
                continue
            title = {'OCR': 'OCR', 'Jev': 'Jev', 'translation': '翻譯', 'total': '合計'}[scope]
            values = (tr(title), *usage_columns(records))
            if self.usage_table.exists(scope):
                self.usage_table.item(scope, values=values)
            else:
                self.usage_table.insert('', 'end', iid=scope, values=values)
        if self.model_window is not None and self.model_window.winfo_exists():
            fill_model_usage(self.model_usage_table, model_records)

    def open_history(self):
        if self.history_window and self.history_window.winfo_exists():
            self.history_window.refresh()
            self.history_window.deiconify()
            self.history_window.lift()
        else:
            self.history_window = HistoryWindow(self.app.root, self.app.history_path, ACTIONS, STATUSES)

    def save_history(self, refresh=True):
        if self.history_run is None:
            return True
        record = self.history_run
        record['saved_at'] = datetime.now().astimezone().isoformat(timespec='seconds')
        if refresh:
            stages, usage = {}, {}
            for (index, stage), value in self.stage_times.items():
                stages.setdefault(index, {})[stage] = value
            for (index, scope), value in self.usage_records.items():
                usage.setdefault(index, {})[scope] = dict(
                    value, cost=str(value['cost']) if value['cost'] is not None else None)
            for index, (job, saved) in enumerate(zip(self.active_jobs, record['jobs'])):
                saved.update(status=job.status, error=job.error,
                             jev_stats=self.jev_records.get(index, {}),
                             stage_seconds=stages.get(index, {}), usage=usage.get(index, {}))
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

    def menu_entry(self, menu, label, command, state):
        """Add a command whose state is re-evaluated each time the menu opens."""
        menu.add_command(label=label, command=command)
        self.menu_states.append((menu, menu.index("end"), state))

    def action_entry(self, menu, label):
        self.menu_entry(menu, tr(label), lambda: self.run_action(label),
                        lambda: self.action_states.get(label, "disabled"))

    def run_action(self, label):
        """Run a menu action only while update_controls allows it."""
        self.update_controls()
        if self.action_states.get(label) == "normal":
            self.actions[label]()

    def sync_menu(self, menu):
        for owner, index, state in self.menu_states:
            if owner is menu:
                owner.entryconfigure(index, state=state())

    def button(self, parent, text, command):
        button = ttk.Button(parent, text=text, command=command, width=0)
        button.pack(side="left", padx=2)
        self.controls.append(button)
        return button

    def settings(self):
        return dict(bt_path=self.installation.get(), bt_config=self.config.get(),
                    bt_token_rates=dict(self.token_rates),
                    bt_workflow=self.workflow_mode(),
                    bt_python=self.python.get(), bt_export=self.export.get(), bt_cleanup=self.cleanup.get(),
                    bt_open_after_completion=self.open_after_completion.get(),
                    bt_jobs=[dict(path=str(job.path), action=job.action, status=job.status, error=job.error,
                                  start_page=job.start_page, end_page=job.end_page, range_export=job.range_export)
                             for job in self.jobs], **self.notifications.settings())

    def workflow_mode(self):
        return next((key for key, value in MODES.items() if tr(value) == self.workflow.get()), "normal")

    def output_options_changed(self):
        if not self.export.get():
            self.cleanup.set(False)
        self.app.save_settings()

    def workflow_changed(self):
        self.update_controls()
        self._queue_width = None
        self.app.root.after_idle(self.finish_sash_layout)
        self.app.save_settings()

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
            lines.append(reason)
        for scope, title in (('OCR', 'OCR'), ('Jev', 'Jev'), ('translation', '翻譯'), ('total', '合計')):
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
        for scope, title in (('OCR', 'OCR'), ('Jev', 'Jev'), ('translation', '翻譯'), ('total', '合計')):
            summary = self.usage_records.aggregate(scope)
            lines.append(f'{tr(title)}: {usage_text([summary] if summary else [])}')
        lines.append(tr('預估金額非實際帳單；僅含已回報用量。'))
        if self.history_save_failed:
            lines.append(tr('歷史紀錄儲存失敗'))
        self.notifications.send('done', tr('整批結束'), '\n'.join(lines), 3 if record['status'] == 'done' else 4)

    def reset_bt_progress(self, total=None, enabled=None, names=None):
        for name in BT_STAGES if names is None else names:
            active = enabled is None or enabled.get(name, True)
            self.bt_progress[name] = (0, 0 if total is not None else None, total, None, active)
            self.bt_elapsed[name] = None
            self.bt_terminal[name] = None
            self.show_bt_progress(name)

    def show_bt_progress(self, name, terminal=None):
        if terminal is not None:
            self.bt_terminal[name] = terminal
        terminal = self.bt_terminal.get(name)
        percent, current, total, eta, active = self.bt_progress[name]
        self.bt_bars[name].configure(value=percent)
        if not active:
            text = tr("未啟用")
        else:
            remaining = eta or tr("估算中")
            if eta and all(part.isdigit() for part in eta.split(':')):
                remaining = elapsed_text(sum(int(part) * 60 ** index
                                             for index, part in enumerate(reversed(eta.split(':')))))
            template = tr("{0}%｜{1}/{2} 頁｜剩餘 {3}")
            complete = percent >= 100 and (current is None or total is None or current >= total)
            waiting = not percent and not current
            state = "完成" if complete else terminal
            if waiting:
                state = "未執行" if terminal in ("已停止", "失敗") else "待執行"
            if state:
                remaining = tr(state)
                template = tr("{0}%｜{1}/{2} 頁｜狀態 {3}")
            text = template.format(percent, f"{current:,}" if current is not None else "—",
                                   f"{total:,}" if total is not None else "—", remaining)
            seconds = self.bt_elapsed[name]
            average = '—'
            if current and seconds is not None:
                # tqdm reports whole seconds; zero elapsed is an upper bound, not zero processing time.
                average = '<1.00' if seconds == 0 else '<0.01' if seconds / current < .01 else f'{seconds / current:.2f}'
            text += '｜' + tr("平均：{0} 秒／頁").format(average)
        self.bt_labels[name].set(text)

    def fit_columns(self, event):
        if self.sash_after is not None:
            return
        width = event.width - 4
        if width < 100 or getattr(self, "_queue_width", None) == width:
            return
        self._queue_width = width
        app = self.app
        wrapped = width < app.enqueue_range.winfo_reqwidth() + app.enqueue_actions.winfo_reqwidth() + 8
        app.enqueue_actions.grid_configure(row=1 if wrapped else 0, column=0 if wrapped else 1,
                                          columnspan=2 if wrapped else 1, pady=(4, 0) if wrapped else 0)
        wrapped = width < sum(button.winfo_reqwidth() for button in self.footer_buttons) + 24
        positions = ((0, 0), (1, 0), (1, 1), (1, 2)) if wrapped else tuple((0, i) for i in range(4))
        for button, (row, column) in zip(self.footer_buttons, positions):
            button.grid(row=row, column=column, sticky="w", padx=2, pady=2)
        self.workflow_group.grid_configure(columnspan=3 if wrapped else 1, sticky="ew")
        self.start_button.master.columnconfigure(0, weight=0 if wrapped else 1)
        self.start_button.master.columnconfigure(3, weight=1 if wrapped else 0)
        row = column = used = 0
        for button in self.navigation_buttons:
            size = button.winfo_reqwidth() + 4
            if used and used + size > width:
                row, column, used = row + 1, 0, 0
            button.grid(row=row, column=column, sticky="w", padx=2, pady=2)
            column, used = column + 1, used + size
        font = tkfont.nametofont("TkDefaultFont", root=self.app.root)
        name_min = min(180, width // 2)
        fixed = {"status": min(max(font.measure(tr(value)) for value in STATUSES.values()) + 20, width - name_min),
                 "pages": max(90, font.measure(tr("翻譯頁數")) + 16),
                 "action": max(font.measure(tr(value)) for value in ACTIONS.values()) + 20,
                 "error_reason": 180}
        visible = ["status"]
        for column in ("pages", "action", "error_reason"):
            if name_min + sum(fixed[key] for key in (*visible, column)) <= width:
                visible.append(column)
        self.tree.configure(displaycolumns=visible)
        for column, size in fixed.items():
            self.tree.column(column, width=size, minwidth=size, stretch=False)
        self.tree.column("#0", width=width - sum(fixed[key] for key in visible),
                         minwidth=name_min, stretch=False)


    def update_selection_details(self):
        selection = self.tree.selection()
        if not selection:
            self.selection_details.set("")
            return
        selected = self.selected_job_ids()
        jobs = [job for job in self.jobs if str(id(job)) in selected]
        paths = {job.path for job in jobs}
        merged = sum(folder_kind(path.name) == "merged" for path in paths)
        summary = " | ".join((
            tr("已選取 {0} 部漫畫").format(len({path.parent for path in paths})),
            tr("已選取 {0} 個佇列項目").format(len(jobs)),
            tr("{0} 單一｜{1} 整合").format(len(paths) - merged, merged),
            tr("翻譯頁數：{0}").format(self.selection_page_count(jobs)),
            tr("等待執行頁數：{0}").format(self.selection_page_count([j for j in jobs if j.status in READY]))))
        item = selection[0]
        job = next((job for job in jobs if len(selection) == 1 and str(id(job)) == item), None)
        if job:
            reason = error_info(job.status, job.error)[1]
            heading = f"{job.path.name} · {tr(STATUSES[job.status])}"
            metadata = " · ".join((tr(ACTIONS[job.action]), self.page_range_text(job)))
            self.selection_details.set("\n".join(filter(None, (heading, reason, metadata, str(job.path), summary))))
        else:
            heading = Path(item.split(":", 1)[1]).name if len(selection) == 1 else tr("已選取 {0} 個佇列項目").format(len(jobs))
            preview = tr("翻譯頁數：{0}").format(self.selection_page_count(jobs)) if any(j.action == "translate" for j in jobs) else " · ".join(sorted({tr(ACTIONS[j.action]) for j in jobs}))
            path = item.split(":", 1)[1] if len(selection) == 1 else ""
            self.selection_details.set("\n".join(filter(None, (heading, preview, path, summary))))

    def selection_page_count(self, jobs):
        keys = [(j.path, j.start_page, j.end_page) for j in jobs if j.action == "translate"]
        counts = [self.count_cache.get(key) for key in keys]
        text = tr("{0} 頁").format(f"{sum(count for count in counts if count is not None):,}")
        if counts and all(count is None for count in counts):
            text = tr("尚未確認")
        calculating = sum(key not in self.count_cache for key in keys)
        unknown = counts.count(None) - calculating
        if calculating:
            text += tr("（{0} 項計數中）").format(calculating)
        if unknown:
            text += tr("（{0} 項無法計數）").format(unknown)
        return text

    def update_controls(self):
        self.update_selection_details()
        busy = self.running or self.app.manga_busy
        for control in self.controls:
            control.configure(state="disabled" if busy else "normal")
        self.action_choice.configure(state="disabled" if busy else "readonly")
        self.app.manga_choice.configure(state="disabled" if busy else "readonly")
        selection = self.app.folder_tree.selection()
        for button in (self.app.add_range_button, self.app.add_selected_button,
                       self.app.aggregate_button, self.app.aggregate_translate_button):
            button.configure(state="normal" if not busy and selection else "disabled")
        self.workflow_choice.configure(state="disabled" if busy else "normal")
        for index in range(len(MODES)):
            self.workflow_menu.entryconfigure(index, state="disabled" if busy else "normal")
        resumable = self.running and self.pause_requested.is_set() and not self.stop.is_set()
        start_text = {"normal": "開始執行", "prepare": "開始前處理", "translate": "開始翻譯", "two_pass": "開始兩輪", "parallel": "開始並行"}
        self.start_button.configure(text=tr("繼續佇列") if resumable else
                                    tr(start_text[self.workflow_mode()]),
                                    state="normal" if resumable or (not busy and self.start_targets()) else "disabled")
        self.pause_button.configure(
            state="normal" if self.running and not self.stop.is_set() and not self.pause_requested.is_set()
            and any(j.status in READY for j in self.active_jobs) else "disabled",
            text=tr("已暫停") if self.paused_at is not None else
            tr("等待暫停") if self.pause_requested.is_set() else tr("暫停"))
        self.stop_button.configure(state="normal" if self.running else "disabled")
        selection = self.tree.selection()
        single_translation = len(selection) == 1 and any(str(id(j)) == selection[0] and j.action == "translate" for j in self.jobs)
        selected = self.selected_job_ids()
        enabled = {
            "翻譯頁數": single_translation,
            "移除選取": bool(selected),
            "重試": bool(self.retry_targets()),
            "清除已完成": any(j.status in ("done", "done_warning") for j in self.jobs),
        }
        for label, direction in (("上移", -1), ("下移", 1)):
            enabled[label] = any(str(id(job)) in selected and 0 <= index + direction < len(self.jobs)
                                 and str(id(self.jobs[index + direction])) not in selected
                                 for index, job in enumerate(self.jobs))
        enabled["移到最上"], enabled["移到最下"] = enabled["上移"], enabled["下移"]
        self.action_states = {label: "normal" if not busy and value else "disabled" for label, value in enabled.items()}
        # Deselecting is harmless while the queue runs.
        self.action_states["取消選取"] = "normal" if selection else "disabled"

    def page_range_text(self, job):
        if job.action != "translate":
            return "—"
        if job.start_page == 1 and job.end_page is None:
            return tr("全部頁面")
        return tr("第 {0}–{1} 頁").format(job.start_page, job.end_page if job.end_page is not None else tr("最後"))

    def filter_jobs(self):
        if self.search_after is not None:
            self.app.root.after_cancel(self.search_after)
        self.search_after = self.app.root.after(180, self.apply_queue_filter)

    def apply_queue_filter(self):
        self.render()
        self.update_controls()

    def render(self, refresh_counts=False):
        if self.search_after is not None:
            self.app.root.after_cancel(self.search_after)
            self.search_after = None
        if refresh_counts:
            self.count_cache.clear()
            self.count_generation += 1
        view = self.tree.yview()
        focus = self.tree.focus()
        selected = set(self.tree.selection())
        open_groups = {}
        for root in self.tree.get_children():
            for item in (root, *self.tree.get_children(root)):
                open_groups[item] = self.tree.item(item, "open")
        self.tree.delete(*self.tree.get_children())
        self.pending_file_counts = {}
        keys = {(j.path, j.start_page, j.end_page) for j in self.jobs}
        keys.update((j.path, 1, None) for j in self.jobs)
        self.count_cache = {key: value for key, value in self.count_cache.items() if key in keys}
        single_root = len({job.path.parent.parent for job in self.jobs}) == 1
        query = self.search.get().strip().replace("\\", "/").casefold()
        visible = 0
        for index, job in enumerate(self.jobs, 1):
            item = str(id(job))
            if job.action == "translate" and job.status in READY:
                key = (job.path, job.start_page, job.end_page)
                self.pending_file_counts[item] = self.count_cache.get(key)
            if query and query not in str(job.path).replace("\\", "/").casefold():
                continue
            visible += 1
            root = "path:" + str(job.path.parent.parent)
            series = "series:" + str(job.path.parent)
            if not self.tree.exists(root):
                path = job.path.parent.parent
                self.tree.insert("", "end", iid=root, text=(path.name or str(path)) if single_root else str(path),
                                 open=bool(query) or open_groups.get(root, single_root))
            if not self.tree.exists(series):
                self.tree.insert(root, "end", iid=series, text=job.path.parent.name, open=bool(query) or open_groups.get(series, False))
            self.tree.insert(series, "end", iid=item, text=f"{index}. {job.path.name}", tags=(job.status,),
                             values=(tr(FOLDER_KINDS[folder_kind(job.path.name)]), tr(ACTIONS[job.action]), self.page_range_text(job), tr(STATUSES[job.status]),
                                     error_info(job.status, job.error)[1]))
        # One call: each selection change queues a <<TreeviewSelect>> that reruns update_controls.
        restored = [item for item in selected if self.tree.exists(item)]
        if restored:
            self.tree.selection_set(restored)
        if focus and self.tree.exists(focus):
            self.tree.focus(focus)
        if view:
            self.tree.yview_moveto(view[0])
        self.search_count.set(tr("顯示 {0} / {1} 項").format(visible, len(self.jobs)))
        self.update_group_counts()
        self.update_summary()
        self.count_pages()

    def count_pages(self):
        if self.counting:
            return
        keys = {(j.path, j.start_page, j.end_page) for j in self.jobs
                if j.action == "translate"}
        missing = keys.difference(self.count_cache)
        if not missing:
            return
        self.counting = True
        generation = self.count_generation
        totals = dict(getattr(self.app, 'chapter_page_counts', {}))
        totals.update({path: count for (path, first, last), count in self.count_cache.items()
                       if first == 1 and last is None and count is not None})
        def work():
            results = {}
            for path, first, last in missing:
                try:
                    if path not in totals:
                        totals[path] = image_count(path)
                    pages = range(totals[path])
                    results[path, 1, None] = len(Job(path, "translate").select_pages(pages))
                    results[path, first, last] = len(Job(path, "translate", start_page=first, end_page=last).select_pages(pages))
                except (OSError, ValueError, TypeError):
                    results[path, first, last] = None
            self.count_results.put((generation, results))
        threading.Thread(target=work, daemon=True).start()
        self.count_after = self.app.root.after(50, self.poll_counts)

    def close_counts(self, event):
        if event.widget is self.app.root and self.search_after is not None:
            self.app.root.after_cancel(self.search_after)
            self.search_after = None
        if event.widget is self.app.root and self.count_after is not None:
            self.app.root.after_cancel(self.count_after)
            self.count_after = None

    def poll_counts(self):
        self.count_after = None
        try:
            generation, results = self.count_results.get_nowait()
        except queue.Empty:
            self.count_after = self.app.root.after(50, self.poll_counts)
            return
        self.counting = False
        if generation == self.count_generation:
            self.count_cache.update(results)
        self.pending_file_counts = {str(id(j)): self.count_cache.get((j.path, j.start_page, j.end_page))
                                    for j in self.jobs if j.action == "translate" and j.status in READY}
        self.update_group_counts()
        self.update_summary()
        self.update_selection_details()
        self.count_pages()

    def update_group_counts(self):
        groups = {}
        for job in self.jobs:
            item = str(id(job))
            if not self.tree.exists(item):
                continue
            key = (job.path, job.start_page, job.end_page)
            count = self.count_cache.get(key)
            if job.action == "translate":
                pages = tr("{0} 頁").format(f"{count:,}") if count is not None else tr("計數中") if key not in self.count_cache else tr("無法計數")
                self.tree.set(item, "pages", pages if job.start_page == 1 and job.end_page is None
                              else f"{pages}（{self.page_range_text(job)}）")
            for group in ("path:" + str(job.path.parent.parent), "series:" + str(job.path.parent)):
                paths, keys = groups.setdefault(group, (set(), set()))
                paths.add(job.path)
                if job.action == "translate":
                    keys.add(key)
        for group, (paths, keys) in groups.items():
            merged = sum(folder_kind(path.name) == "merged" for path in paths)
            self.tree.set(group, "kind", tr("{0} 單一｜{1} 整合").format(len(paths) - merged, merged))
            counts = [self.count_cache.get(key) for key in keys]
            text = tr("{0} 頁").format(f"{sum(count for count in counts if count is not None):,}") if keys else "—"
            calculating = sum(key not in self.count_cache for key in keys)
            unknown = counts.count(None) - calculating
            if calculating:
                text += tr("（{0} 項計數中）").format(calculating)
            if unknown:
                text += tr("（{0} 項無法計數）").format(unknown)
            self.tree.set(group, "pages", text)

    def update_summary(self):
        counts = [self.pending_file_counts.get(str(id(job))) for job in self.jobs
                  if job.action == "translate" and job.status in READY]
        total_pages = sum(count for count in counts if count is not None)
        files = tr("｜待翻譯 {0} 頁").format(f"{total_pages:,}")
        if counts and all(count is None for count in counts):
            files = "｜" + tr("頁數尚未確認")
        notes = ""
        unknown = counts.count(None)
        calculating = sum((j.path, j.start_page, j.end_page) not in self.count_cache for j in self.jobs
                          if j.action == "translate" and j.status in READY)
        if calculating:
            notes += tr("（{0} 項計數中）").format(calculating)
        if unknown > calculating:
            notes += tr("（{0} 項無法計數）").format(unknown - calculating)
        failures = sum(j.status == "failed" for j in self.jobs)
        retries = tr("｜失敗 {0}｜本次重試 {1}").format(failures, self.retry_count) if failures or self.retry_count else ""
        pending = sum(j.status in READY for j in self.jobs)
        done = sum(j.status in ("done", "done_warning") for j in self.jobs)
        attention = sum(j.status in ("failed", "cancelled", "blocked") for j in self.jobs)
        self.summary.set(tr("佇列 {0} 項｜待執行 {1}｜完成 {2}｜需處理 {3}").format(
            len(self.jobs), pending, done, attention) + retries + files + notes)
        preview = tr("待執行 {0} · 完成 {1}").format(pending, done)
        if attention:
            preview += tr(" · 需處理 {0}").format(attention)
        if counts:
            pages = files.lstrip("｜ |") if any(count is not None for count in counts) else tr("頁數尚未確認")
            preview += "\n" + pages + notes
        self.summary_preview.set(preview)

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
        if self.running or self.app.manga_busy:
            return
        action = action or next(code for code, label in ACTIONS.items() if tr(label) == self.action.get())
        if action not in ACTIONS:
            raise ValueError(action)
        affected = []
        for path in paths:
            path = Path(path).resolve()
            self.count_generation += 1
            self.count_cache = {key: value for key, value in self.count_cache.items() if key[0] != path}
            existing = next((job for job in self.jobs if job.path == path and job.action == action), None)
            if existing:
                self.app.chapter_page_counts.pop(path, None)
                existing.status, existing.error = "pending", ""
            else:
                existing = Job(path, action)
                self.jobs.append(existing)
            affected.append(str(id(existing)))
        self.render()
        self.app.work_tabs.select(self.app.queue_tab)
        visible = next((item for item in affected if self.tree.exists(item)), None)
        if visible:
            self.tree.see(visible)
        if affected:
            self.label.set(tr("已加入 {0} 項").format(len(set(affected))))
        self.update_controls()
        self.app.save_settings()

    def add_selected(self):
        if not self.app.source_is_current():
            return
        paths = self.app.selected_chapters()
        if not paths:
            messagebox.showinfo(tr("主佇列"), tr("請先選擇父系列或章節"))
            return
        self.add_paths(paths)

    def add_number_range(self):
        if self.running or self.app.manga_busy or not self.app.source_is_current():
            return
        selected = self.app.selected_tree_item()
        if not selected:
            messagebox.showinfo(tr("主佇列"), tr("請先選擇父系列或章節"))
            return
        series = {path if kind == "series" else path.parent
                  for kind, path in (self.app.tree_items[item] for item in self.app.folder_tree.selection())}
        if len(series) != 1 or self.app.has_explicit_chapter_selection():
            # A range only applies to one manga; otherwise the selection itself is the request.
            self.add_paths(self.app.selected_chapters())
            return
        chapters = selected[2]
        try:
            start = int(self.app.start_entry.get().strip())
            end = int(self.app.end_entry.get().strip() or len(chapters))
        except ValueError:
            messagebox.showwarning(tr("警告"), tr("請輸入有效的起始和結束編號"))
            return
        if not 1 <= start <= end <= len(chapters):
            messagebox.showwarning(tr("警告"), tr("請確保編號範圍有效"))
            return
        self.add_paths([item[0] for item in chapters[start - 1:end]])

    def add_directory(self):
        path = filedialog.askdirectory()
        if path:
            self.add_paths([path])

    def show_context_menu(self, event):
        if self.search_after is not None:
            self.apply_queue_filter()
        if self.tree.identify_region(event.x, event.y) not in ("tree", "cell"):
            return
        item = self.tree.identify_row(event.y)
        if not item:
            return
        if item not in self.tree.selection():
            self.tree.selection_set(item)
        self.tree.focus(item)
        self.update_controls()
        self.sync_menu(self.context_menu)
        try:
            self.context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.context_menu.grab_release()
        return "break"

    def selected_job_ids(self):
        if self.search_after is not None:
            self.apply_queue_filter()
        selected = set()
        def collect(item):
            children = self.tree.get_children(item)
            if children:
                for child in children:
                    collect(child)
            else:
                selected.add(item)
        for item in self.tree.selection():
            collect(item)
        return selected

    def selected_paths(self):
        selected = self.selected_job_ids()
        return [job.path for job in self.jobs if str(id(job)) in selected]

    def remove(self, confirm=False):
        if self.running or self.app.manga_busy:
            return
        selected = self.selected_job_ids()
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
        if not self.running and not self.app.manga_busy:
            self.jobs[:] = [job for job in self.jobs if job.status not in ("done", "done_warning")]
            self.changed()

    def retry_targets(self):
        selected = self.selected_job_ids()
        selection = self.tree.selection()
        return [job for job in self.jobs
                if (not selection and job.status in ("failed", "done_warning"))
                or (str(id(job)) in selected and
                    (job.status in ("failed", "done_warning", "cancelled", "blocked")
                     or (job.status == "done" and selection == (str(id(job)),))))]

    def retry(self):
        if self.running or self.app.manga_busy:
            return
        targets = self.retry_targets()
        if not targets:
            return
        items = [str(id(job)) for job in targets]
        if any(not self.tree.exists(item) for item in items):
            self.search.set("")
            self.apply_queue_filter()
        self.count_generation += 1
        paths = {job.path for job in targets}
        self.count_cache = {key: value for key, value in self.count_cache.items() if key[0] not in paths}
        for job in targets:
            job.status, job.error = "pending", ""
            if job.action == "translate" and (job.path / MARKER).is_file():
                try:
                    if prepared(job.path, job.select_pages(image_files(job.path))):
                        job.status = "prepared"
                except (OSError, ValueError):
                    pass  # Missing sources or an invalid range remain pending for normal validation.
            self.app.chapter_page_counts.pop(job.path, None)
        self.tree.selection_set(items)
        for item in items:
            parent = self.tree.parent(item)
            while parent:
                self.tree.item(parent, open=True)
                parent = self.tree.parent(parent)
        self.tree.focus(items[0])
        self.tree.see(items[0])
        self.changed()

    def move(self, direction, *, to_edge=False):
        if self.running or self.app.manga_busy:
            return
        selected = self.selected_job_ids()
        if to_edge:
            moving = [job for job in self.jobs if str(id(job)) in selected]
            remaining = [job for job in self.jobs if str(id(job)) not in selected]
            self.jobs[:] = moving + remaining if direction < 0 else remaining + moving
            self.changed()
            selection = self.tree.selection()
            if selection:
                self.tree.see(selection[0] if direction < 0 else selection[-1])
            return
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
                status = tr("前處理完成／待翻譯") if job.status == "prepared" else tr(STATUSES[job.status])
                messagebox.showinfo(tr("工作詳情"), f"{job.path}\n{tr(ACTIONS[job.action])} / {status}\n{self.page_range_text(job)}\n\n{error_details(job.status, job.error)}")
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
        ttk.Label(box, text=tr("共 {0} 頁；檔名自然排序，含起訖頁").format(len(images))).grid(row=1, column=0, columnspan=2, sticky="w", pady=6)
        start = tk.StringVar(dialog, value=str(job.start_page))
        end = tk.StringVar(dialog, value="" if job.end_page is None else str(job.end_page))
        for row, title, variable in ((2, "起始頁", start), (3, "結束頁（空白到最後）", end)):
            ttk.Label(box, text=tr(title)).grid(row=row, column=0, sticky="w", pady=3)
            ttk.Spinbox(box, from_=1, to=len(images), textvariable=variable, width=12).grid(row=row, column=1, padx=(12, 0))
        range_export = tk.BooleanVar(dialog, value=job.range_export)
        export_check = ttk.Checkbutton(box, text=tr("指定頁面完成後匯出 CBZ"), variable=range_export)
        export_check.grid(row=4, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Label(box, text=tr("勾選後才匯出整章 CBZ；須全章結果齊全。"),
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
        if self.workflow_mode() == "prepare":
            return True
        needs_cleanup = self.cleanup.get() or any(j.action == "cleanup" for j in self.active_jobs)
        return not needs_cleanup or messagebox.askyesno(
            tr("確認清理"), tr("只清理已完整匯出且 CBZ 驗證通過的 mask / inpainted。\n未匯出、檔案已變更或驗證失敗的章節會保留。\n清理無法復原，確定繼續？"))

    def integrate(self):
        if not self.app.manga_busy:
            self.app.confirm_aggregate(translate_after=True)

    def validate(self, translate=False, jobs=None):
        pending = [job for job in (self.jobs if jobs is None else jobs) if job.status in READY]
        error_tab = self.app.queue_tab
        try:
            translating = translate or any(job.action == "translate" for job in pending)
            if translating:
                error_tab = self.app.settings_tab
                if self.editor and self.editor.poll() is None:
                    raise ValueError(tr("請先在 BallonsTranslator 儲存設定並關閉原生介面"))
                self.command()
                error_tab = self.app.queue_tab
            exporting = (translate and self.export.get()) or any(job.should_export(self.export.get()) for job in pending)
            if self.workflow_mode() == "prepare":
                exporting = False
            cleaning = self.workflow_mode() != "prepare" and (
                (translating and self.cleanup.get()) or any(job.action == "cleanup" for job in pending))
            if cleaning and translating and self.cleanup.get() and not self.export.get():
                raise ValueError(tr("請先勾選匯出 CBZ，再啟用清理"))
            if (exporting or cleaning) and not self.app.komga_path.get().strip():
                error_tab = self.app.export_tab
                raise ValueError(tr("請設定 Komga 輸出路徑"))
            return True
        except (OSError, ValueError) as error:
            logger.exception("queue_validation_failed")
            messagebox.showerror(tr("佇列設定"), str(error))
            self.app.work_tabs.select(error_tab)
            return False

    def request_pause(self):
        if self.running and not self.stop.is_set():
            self.pause_requested.set()
            self.label.set(tr("完成目前項目後暫停"))
            self.update_controls()

    def request_stop(self):
        self.stop.set()
        if self.running:
            self.label.set(tr("佇列停止中"))
        self.update_controls()

    def clear_selection(self):
        self.tree.selection_remove(self.tree.selection())
        self.update_controls()

    def start_targets(self):
        selected = self.selected_job_ids()
        has_selection = bool(self.tree.selection())
        return tuple(job for job in self.jobs if job.status in READY
                     and (not has_selection or str(id(job)) in selected)
                     and (self.workflow_mode() == "normal" or job.action == "translate"))

    def configure_start(self, jobs):
        if not self.tree.selection():
            return jobs
        translations = [job for job in jobs if job.action == "translate"]
        series = list(dict.fromkeys(job.path.parent for job in translations))
        if len(series) > 1:
            order = QueueStartDialog(self.app.root, series, reorder=True).result
            if order is None:
                return ()
            if order != series:
                ranks = {path: index for index, path in enumerate(order)}
                return tuple(sorted(jobs, key=lambda job: ranks.get(job.path.parent, len(ranks))))
        elif len(series) == 1 and any(item.startswith(("series:", "path:")) for item in self.tree.selection()):
            singles = list(dict.fromkeys(job.path for job in translations if folder_kind(job.path.name) != "merged"))
            if len(singles) > 1:
                chosen = QueueStartDialog(self.app.root, singles).result
                if chosen is None:
                    return ()
                excluded = set(singles).difference(chosen)
                return tuple(job for job in jobs if job.path not in excluded)
        return jobs

    def start(self, cleanup_confirmed=False, *, scheduled_jobs=None, configure=False):
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
        self.active_jobs = self.start_targets() if scheduled_jobs is None else tuple(scheduled_jobs)
        if configure:
            self.active_jobs = self.configure_start(self.active_jobs)
        if not self.active_jobs or not self.validate(jobs=self.active_jobs):
            return
        if not cleanup_confirmed and not self.confirm_cleanup():
            return
        if not self.app.save_settings():
            return
        self.retry_count = 0
        self.phase_jobs.clear()
        self.bt_job_indices.clear()
        self.update_summary()
        self.stage_times.clear()
        self.usage_records.clear()
        self.jev_records.clear()
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
        self.elapsed_label.set('00:00:00')
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
        previous_failures, blocked_jobs = set(), set()
        indices = {id(job): index for index, job in enumerate(self.active_jobs)}
        for job in self.jobs:
            if job.status in ("failed", "cancelled", "blocked"):
                previous_failures.add(job.path)
            elif id(job) in indices and job.path in previous_failures:
                blocked_jobs.add(indices[id(job)])
        options = (self.settings(), self.app.komga_path.get(), self.app.skip_unchanged.get())
        threading.Thread(target=run_jobs, args=(self.active_jobs, *options, self.stop, self.events.put),
                         kwargs={'pause': self.pause_requested, 'checkpoint': self.checkpoint, 'blocked_jobs': blocked_jobs}, daemon=True).start()
        self.app.root.after(50, self.poll)

    def checkpoint(self):
        ready = threading.Event()
        self.events.put(("checkpoint", ready))
        while not ready.wait(.1):
            if self.stop.is_set():
                return False
        return not self.stop.is_set()

    def poll(self):
        if self.running and self.started_at is not None:
            now = time.monotonic() if self.paused_at is None else self.paused_at
            self.elapsed_label.set(elapsed_text(now - self.started_at))
        changed = False
        statistics_changed = False
        controls_changed = False
        summary_changed = False
        for _ in range(100):
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            phase = None
            names = tuple(BT_STAGES)
            if event[0] == "phase_event":
                _, phase, event = event
                names = tuple(name for name in BT_STAGES if (name == "Translation") == (phase == "translate"))
                if event[0] in ("bt_progress",) and event[1] not in names:
                    continue
                if event[0] == "stage_time" and event[2] not in names:
                    continue
            if event[0] == "checkpoint":
                try:
                    if not self.app.save_settings() or not self.save_history():
                        self.stop.set()
                    changed = False
                except Exception:
                    self.stop.set()
                    raise
                finally:
                    event[1].set()
            elif event[0] == "status":
                job = self.active_jobs[event[1]]
                job.status, job.error = event[2], event[3]
                changed = True
                visible = self.tree.exists(str(id(job)))
                if visible:
                    self.tree.set(str(id(job)), "status", tr(
                        ("前處理中" if phase == "prepare" else "翻譯中")
                        if phase and job.status == "running" else STATUSES[job.status]))
                code, reason = error_info(job.status, job.error)
                if code:
                    logger.warning("queue_status path=%s error_code=%s reason=%s", job.path, code, reason)
                if visible:
                    self.tree.set(str(id(job)), "error_reason", reason)
                summary_changed = controls_changed = True
                if job.status == "running":
                    if visible:
                        self.tree.see(str(id(job)))
                    self.stage.configure(value=0)
                    self.stage.pack_forget()
                    self.reset_bt_progress(names=names)
                    self.bt_job_indices.update(dict.fromkeys(names, event[1]))
                    if phase is None:
                        self.label.set(tr("佇列停止中") if self.stop.is_set() else tr("正在處理：{0}").format(job.path.name))
                elif job.status in ("failed", "cancelled"):
                    for name in names:
                        if self.bt_job_indices.get(name) == event[1]:
                            self.show_bt_progress(name, STATUSES[job.status])
                if phase and any(self.bt_job_indices.get(name) == event[1] for name in names):
                    self.phase_jobs[phase] = str(job.path.parent.name + '/' + job.path.name) if job.status == "running" else '—'
                    self.label.set(tr("佇列停止中") if self.stop.is_set() else tr("前處理：{0}｜翻譯：{1}").format(
                        self.phase_jobs.get('prepare', '—'), self.phase_jobs.get('translate', '—')))
            elif event[0] == "paused":
                self.paused_at = event[1]
                elapsed = self.paused_at - self.started_at
                self.elapsed_label.set(elapsed_text(elapsed))
                self.label.set(tr("佇列停止中") if self.stop.is_set() else tr("已暫停；按「繼續佇列」恢復"))
                for name in BT_STAGES:
                    if self.bt_terminal.get(name) is None:
                        self.show_bt_progress(name, "已暫停")
                if self.history_run is not None:
                    self.history_run['elapsed_seconds'] = elapsed
                    self.save_history()
                controls_changed = True
            elif event[0] == "resumed":
                self.started_at += event[1]
                self.paused_at = None
                for name in BT_STAGES:
                    if self.bt_terminal.get(name) == "已暫停":
                        self.bt_terminal[name] = None
                        self.show_bt_progress(name)
                if not self.stop.is_set():
                    self.label.set(tr("佇列繼續處理"))
                controls_changed = True
            elif event[0] == "stage_time":
                _, index, name, seconds = event
                self.stage_times[index, name] = max(seconds, self.stage_times.get((index, name), 0))
                total = sum(value for (_, stage), value in self.stage_times.items() if stage == name)
                self.time_labels[name].set(tr("累計耗時：{0}").format(elapsed_text(total)))
                self.bt_elapsed[name] = seconds
                self.show_bt_progress(name)
            elif event[0] == "usage":
                self.usage_records[event[1], event[2]['scope']] = event[2]
                statistics_changed = True
            elif event[0] == "retry":
                self.retry_count += 1
                summary_changed = True
            elif event[0] == "jev_stats":
                self.jev_records[event[1]] = event[2]
                statistics_changed = True
            elif event[0] == "job_time" and self.history_run is not None:
                self.history_run['jobs'][event[1]].update(started_at=event[2], finished_at=event[3], elapsed_seconds=event[4])
                self.notify_job(event[1])
            elif event[0] == "run_time":
                if self.history_run is not None:
                    self.history_run.update(started_at=event[1], finished_at=event[2], elapsed_seconds=event[3])
                self.started_at = None
                self.elapsed_label.set(elapsed_text(event[3]))
            elif event[0] == "bt_reset":
                self.reset_bt_progress(event[1], event[2], names=names)
            elif event[0] == "bt_progress":
                name, percent, current, total, eta = event[1:]
                self.bt_progress[name] = (percent, current, total, eta, True)
                self.bt_elapsed[name] = None  # Only pair page counts with timing from the same progress report.
                self.show_bt_progress(name)
            elif event[0] == "stage":
                if phase is None and not self.stop.is_set():
                    self.label.set(f"{tr(event[1])}: {event[2]}%")
                self.stage.pack(fill="x", after=self.total)
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
                if statistics_changed:
                    self.show_usage()
                if summary_changed:
                    self.update_summary()
                if self.history_run is not None:
                    self.history_run['status'] = ('cancelled' if self.stop.is_set() else 'done'
                                                  if all(job.status in ('done', 'prepared', 'skipped') for job in self.active_jobs) else 'done_warning')
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
        if statistics_changed:
            self.show_usage()
        if summary_changed:
            self.update_summary()
        if controls_changed:
            self.update_controls()
        if changed and not self.app.save_settings():
            self.stop.set()
        self.app.root.after(50, self.poll)
