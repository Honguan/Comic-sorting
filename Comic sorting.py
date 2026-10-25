import ctypes
import os
import queue
import shutil
import sys
import threading
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk, font as tkfont
from translation_queue import TranslationQueue
from app_logging import configure_logging, logger, log_path, redact
from ui_language import LANGUAGES, set_language, tr
import ui_theme
from ui_theme import apply_theme, px
from ui_interactions import LibraryTree, bind_search_shortcuts, bind_table_shortcuts, copy_manga_names, details_text
from comic_core import (
    IMAGE_EXTENSIONS, FOLDER_KINDS, chapter_number, folder_kind, image_files, translation_status,
    natural_sort_key, updated_at, folder_size, format_size, summarize_names,
    clear_work_folders, remove_aggregated_folders, delete_manga_folder, source_fingerprint,
    output_path_for, validate_cbz, create_cbz, load_json, save_json, export_chapter,
    aggregate_output, is_link_or_junction,
    get_folders_with_numbers, scan_folder_data, aggregate_folders,
)


SETTINGS_FILENAME = "comic-sorting.settings.json"
SERIES_SORT_FIELDS = {"updated": "更新時間", "name": "系列名稱", "size": "資料夾大小", "chapters": "章節數"}


def resource_path(relative_path):
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    return base / relative_path


def settings_path():
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
    return base / SETTINGS_FILENAME


class FileAggregatorApp:
    def __init__(self, root):
        self.root = root
        self.history_path = settings_path().with_name("comic-sorting.history.sqlite3")
        self.root.report_callback_exception = self.report_callback_exception
        settings = load_json(settings_path(), {}, strict=True)
        apply_theme(root)
        language = settings.get("ui_language", "zh-TW")
        set_language(language)
        self.ui_language = tk.StringVar(value=LANGUAGES.get(language, LANGUAGES["zh-TW"]))
        icon = resource_path("assets/comic-sorting.ico")
        if icon.is_file():
            self.root.iconbitmap(default=icon)
        self.root.title(tr("漫畫工作台"))
        self.root.minsize(px(820), px(640))
        size = settings.get("window_size")
        self.window_size = None
        self.window_maximized = settings.get("window_maximized") is True
        if isinstance(size, list) and len(size) == 2 and all(type(value) is int and value > 0 for value in size):
            width = max(px(820), min(px(size[0]), self.root.winfo_screenwidth()))
            height = max(px(640), min(px(size[1]), self.root.winfo_screenheight()))
            self.window_size = [round(width / ui_theme.SCALE), round(height / ui_theme.SCALE)]
            self.root.geometry(f"{width}x{height}")
        self.folders = []
        self.series_groups = {}
        self.tree_items = {}
        self.chapter_updates = {}
        self.chapter_sizes = {}
        self.chapter_page_counts = {}
        self.ready_chapters = set()
        self.events = queue.Queue()
        self.scan_events = queue.Queue()
        self.scan_count = 0
        self.scan_started = time.monotonic()
        self.aggregate_events = queue.Queue()
        self.manga_busy = False
        self.base_path = tk.StringVar(value=settings.get("manga_path", ""))
        self.komga_path = tk.StringVar(value=settings.get("komga_path", ""))
        self.skip_unchanged = tk.BooleanVar(value=settings.get("skip_unchanged", True) is True)
        self.open_after_export = tk.BooleanVar(value=settings.get("open_after_export", False) is True)
        self.remove_sources_after_aggregate = tk.BooleanVar(
            value=settings.get("remove_sources_after_aggregate") is True)
        self.keep_last_source = tk.BooleanVar(value=settings.get("keep_last_source") is not False)
        self.status_text = tk.StringVar(value=tr("就緒"))
        self.search_text = tk.StringVar()
        self.scan_data = None
        self.search_after = None
        self.applied_query = ""
        self.expanded_series = set()
        self.series_sort = settings.get("series_sort", "updated")
        if self.series_sort not in tuple(SERIES_SORT_FIELDS):
            self.series_sort = "updated"
        self.series_sort_descending = settings.get("series_sort_descending", True) is True

        self.build_library_panel()
        self.build_work_tabs()

        self.translation_queue = TranslationQueue(self, settings)
        self.window_grip = ttk.Sizegrip(root)
        self.window_grip.place(relx=1, rely=1, anchor="se")
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.bind("<Configure>", self.remember_window_size, add="+")
        if self.window_maximized and os.name == "nt":
            self.root.state("zoomed")
        if self.base_path.get() and Path(self.base_path.get()).is_dir():
            self.load_folders()

    def build_library_panel(self):
        self.pending_chapters = {}
        self.current_series = tk.StringVar(value=tr("請先選擇漫畫"))
        self.series_choices = {}
        self.range_start, self.range_end = tk.StringVar(), tk.StringVar()
        self.panes = ttk.Panedwindow(self.root, orient="horizontal")
        self.panes.pack(fill="both", expand=True, padx=px(10), pady=px(8))
        self.panes.bind("<Map>", self.initialize_panes)
        manga = ttk.Frame(self.panes, padding=px(8))
        header = ttk.Frame(manga)
        header.pack(fill="x", pady=px(0, 6))
        ttk.Label(header, text=tr("漫畫庫"), style="TLabelframe.Label").pack(side="left")
        self.panes.add(manga, weight=0)
        path_row = ttk.Frame(manga)
        path_row.pack(fill="x")
        self.path_entry = ttk.Entry(path_row, textvariable=self.base_path)
        self.path_entry.pack(fill="x")
        path_buttons = ttk.Frame(manga)
        path_buttons.pack(fill="x", pady=px(4, 0))
        self.browse_button = ttk.Button(path_buttons, text=tr("瀏覽"), command=self.browse)
        self.browse_button.pack(side="left", fill="x", expand=True)
        self.rescan_button = ttk.Button(path_buttons, text=tr("重新掃描"), command=self.load_folders)
        self.rescan_button.pack(side="left", fill="x", expand=True, padx=px(4, 0))
        self.path_entry.bind("<Return>", lambda _event: self.load_folders())

        search_row = ttk.Frame(manga)
        search_row.pack(fill="x", pady=px(8, 0))
        ttk.Label(search_row, text=tr("搜尋")).pack(side="left", padx=px(0, 4))
        self.search_entry = ttk.Entry(search_row, textvariable=self.search_text)
        self.search_entry.pack(side="left", fill="x", expand=True)
        self.search_entry.configure(width=8)
        ttk.Button(search_row, text=tr("清除"), width=0, command=lambda: self.search_text.set("")).pack(side="left", padx=px(4))
        self.library_menu = library_menu = ttk.Menubutton(header, text=tr("工具"))
        menu = tk.Menu(library_menu, tearoff=False)
        menu.add_command(label=tr("章節整合"), command=self.open_aggregate)
        menu.add_command(label=tr("匯出漫畫名稱"), command=self.export_manga_names)
        sort_menu = tk.Menu(menu, tearoff=False)
        for field, title in SERIES_SORT_FIELDS.items():
            sort_menu.add_command(label=tr(title), command=lambda key=field: self.sort_by_column(key))
        menu.add_cascade(label=tr("排序"), menu=sort_menu)
        library_menu.configure(menu=menu)
        library_menu.pack(side="right")
        self.selection_text = tk.StringVar(value=tr("已選取 {0} 個資料夾").format(0))
        self.search_text.trace_add("write", self.filter_folders)

        manga_footer = ttk.Frame(manga)
        manga_footer.pack(side="bottom", fill="x")
        list_frame = ttk.Frame(manga)
        list_frame.pack(fill="both", expand=True, pady=px(8))
        self.folder_tree = LibraryTree(
            list_frame, self.load_series_children,
            columns=("kind", "status", "size", "updated", "pages"), displaycolumns=("pages",),
            show="tree headings", selectmode="extended", height=6)
        self.update_sort_headings()
        self.folder_tree.column("#0", width=px(230), minwidth=px(160))
        for column, title, width in (("kind", "資料夾類型", 110), ("status", "狀態", 190),
                                     ("size", "資料夾大小", 110), ("updated", "更新時間", 180), ("pages", "頁數", 80)):
            self.folder_tree.heading(column, text=tr(title))
            self.folder_tree.column(column, width=px(width), stretch=False)
        vertical = ttk.Scrollbar(list_frame, command=self.folder_tree.yview)
        horizontal = ttk.Scrollbar(list_frame, orient="horizontal", command=self.folder_tree.xview)
        self.folder_tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.folder_tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        list_frame.rowconfigure(0, weight=1)
        list_frame.columnconfigure(0, weight=1)
        self.folder_details = tk.StringVar()
        _details, self.folder_details_label = details_text(list_frame, self.folder_details,
                                                           row=2, column=0, columnspan=2, sticky="ew", pady=px(6, 0))
        self.folder_tree.bind("<Configure>", self.fit_folder_columns)
        self.folder_tree.bind("<<TreeviewSelect>>", self.on_tree_select)
        self.folder_tree.bind("<Double-1>", self.add_chapter_to_queue)
        copy_paths = bind_table_shortcuts(self.folder_tree, self.selected_chapters)
        self.root.bind("<Control-f>", bind_search_shortcuts(
            self.search_entry, self.search_text, self.apply_filter, self.folder_tree))
        self.folder_context_menu = tk.Menu(self.folder_tree, tearoff=False)
        self.folder_context_menu.add_command(label=tr("刪除章節資料夾…"))
        for label in ("全選所有資料夾", "全選單一章節", "全選整合資料夾"):
            self.folder_context_menu.add_command(label=tr(label))
        self.folder_context_menu.add_separator()
        self.folder_context_menu.add_command(label=tr("複製完整路徑"), accelerator="Ctrl+C", command=copy_paths)
        self.folder_context_menu.add_command(label=tr("複製漫畫名稱"), command=lambda: copy_manga_names(
            self.folder_tree, (path if kind == "series" else path.parent
                               for kind, path in (self.tree_items[item] for item in self.folder_tree.selection()))))
        self.folder_tree.bind("<Button-3>", self.show_folder_context_menu)

        scan_row = ttk.Frame(manga_footer)
        scan_row.pack(fill="x")
        self.scan_progress = ttk.Progressbar(scan_row, mode="indeterminate")
        self.scan_status_text = tk.StringVar(value=tr("尚未掃描"))
        self.scan_status_label = ttk.Label(scan_row, textvariable=self.scan_status_text, wraplength=px(300), style="Muted.TLabel")
        self.scan_status_label.pack(side="left", fill="x", expand=True)
        self.scan_status_label.bind("<Configure>", lambda event: self.scan_status_label.configure(wraplength=event.width)
                                    if event.width > 100 and self.translation_queue.sash_after is None else None)
        self.add_selected_button = ttk.Button(manga_footer, text=tr("加入選取"),
                                             command=lambda: self.translation_queue.add_selected())
        self.add_selected_button.pack(fill="x", pady=px(4, 0))
        ttk.Label(manga_footer, textvariable=self.selection_text, style="Muted.TLabel").pack(anchor="w", pady=px(4, 0))


    def build_enqueue_panel(self, parent):
        panel = ttk.Frame(parent)
        panel.pack(fill="x", pady=px(0, 8))
        self.enqueue_header = ttk.Frame(panel)
        self.enqueue_header.pack(fill="x", pady=px(0, 4))
        ttk.Label(self.enqueue_header, text=tr("加入漫畫與章節"), style="TLabelframe.Label").pack(side="left")
        manga_row = ttk.Frame(panel)
        manga_row.pack(fill="x", pady=px(0, 4))
        ttk.Label(manga_row, text=tr("漫畫")).pack(side="left", padx=px(0, 8))
        self.manga_choice = ttk.Combobox(manga_row, textvariable=self.current_series, state="readonly", width=8)
        self.manga_choice.pack(side="left", fill="x", expand=True)
        self.manga_choice.bind("<<ComboboxSelected>>", self.choose_series)
        range_row = ttk.Frame(panel)
        range_row.pack(fill="x")
        range_row.columnconfigure(1, weight=1)
        self.enqueue_range = ttk.Frame(range_row)
        self.enqueue_range.grid(row=0, column=0, sticky="w", padx=px(0, 8))
        ttk.Label(self.enqueue_range, text=tr("章節序號")).pack(side="left", padx=px(0, 8))
        self.start_entry = ttk.Entry(self.enqueue_range, textvariable=self.range_start, width=5)
        self.start_entry.pack(side="left")
        ttk.Label(self.enqueue_range, text="–").pack(side="left", padx=px(4))
        self.end_entry = ttk.Entry(self.enqueue_range, textvariable=self.range_end, width=5)
        self.end_entry.pack(side="left")
        self.enqueue_actions = ttk.Frame(range_row)
        self.enqueue_actions.grid(row=0, column=1, sticky="ew")
        self.add_range_button = ttk.Button(self.enqueue_actions, text=tr("加入佇列"), style="Accent.TButton",
                                          command=lambda: self.translation_queue.add_number_range())
        self.add_range_button.pack(side="right")
        def add_range(_event=None):
            self.add_range_button.invoke()
            return "break"
        for entry in (self.start_entry, self.end_entry):
            entry.bind("<Return>", add_range)
        ttk.Label(panel, text=tr("從 1 起算；結束留白到最後"), style="Muted.TLabel").pack(anchor="w", pady=px(2, 0))


    def series_name(self, series):
        base = self.scan_data[0] if self.scan_data else None
        return str(series.relative_to(base)) if base and series != base else series.name

    def choose_series(self, _event=None):
        series = self.series_choices.get(self.current_series.get())
        if series is None or self.manga_busy or self.translation_queue.running:
            return
        if self.search_text.get() or self.search_after is not None:
            self.search_text.set("")
            self.apply_filter()
        parent = next(item for item in self.folder_tree.get_children()
                      if self.tree_items[item] == ("series", series))
        self.folder_tree.selection_set(parent)
        self.folder_tree.see(parent)
        self.on_tree_select()
        self.work_tabs.select(self.queue_tab)

    def open_aggregate(self):
        self.aggregate_window.deiconify()
        self.aggregate_window.lift()


    def initialize_panes(self, _event):
        self.panes.unbind("<Map>")
        self.panes.sashpos(0, min(px(360), max(px(260), int(self.panes.winfo_width() * .30))))

    def build_work_tabs(self):
        self.work_tabs = ttk.Notebook(self.panes)
        self.panes.add(self.work_tabs, weight=1)
        self.queue_tab = ttk.Frame(self.work_tabs)
        self.aggregate_window = tk.Toplevel(self.root)
        self.aggregate_window.title(tr("章節整合"))
        self.aggregate_window.geometry(f"{px(600)}x{px(350)}")
        self.aggregate_window.minsize(px(480), px(320))
        self.aggregate_window.protocol("WM_DELETE_WINDOW", self.aggregate_window.withdraw)
        self.aggregate_window.withdraw()
        self.aggregate_tab = ttk.Frame(self.aggregate_window)
        self.aggregate_tab.pack(fill="both", expand=True)
        self.export_tab = ttk.Frame(self.work_tabs)
        self.settings_tab = ttk.Frame(self.work_tabs)
        self.work_tabs.add(self.queue_tab, text=tr("主佇列"))
        self.work_tabs.add(self.export_tab, text=tr("Komga 匯出"))
        self.work_tabs.add(self.settings_tab, text=tr("設定"))

        integration = ttk.LabelFrame(self.aggregate_tab, text=tr("章節整合"), padding=px(12))
        integration.pack(fill="x", padx=px(8), pady=px(8))
        ttk.Label(integration, textvariable=self.current_series).pack(anchor="w", pady=px(0, 8))
        range_row = ttk.Frame(integration)
        range_row.pack(fill="x")
        ttk.Label(range_row, text=tr("章節序號")).pack(side="left")
        self.aggregate_start_entry = ttk.Entry(range_row, textvariable=self.range_start, width=6)
        self.aggregate_start_entry.pack(side="left", padx=px(6))
        ttk.Label(range_row, text="–").pack(side="left")
        self.aggregate_end_entry = ttk.Entry(range_row, textvariable=self.range_end, width=6)
        self.aggregate_end_entry.pack(side="left", padx=px(6))
        self.remove_sources_checkbox = ttk.Checkbutton(integration, text=tr("整合後清除來源"),
            variable=self.remove_sources_after_aggregate, command=self.save_settings)
        self.remove_sources_checkbox.pack(anchor="w", pady=px(12, 4))
        self.keep_last_source_checkbox = ttk.Checkbutton(integration, text=tr("保留最後一個"),
            variable=self.keep_last_source, command=self.save_settings)
        self.keep_last_source_checkbox.pack(anchor="w")
        buttons = ttk.Frame(integration)
        buttons.pack(fill="x", pady=px(12, 0))
        self.aggregate_button = ttk.Button(buttons, text=tr("確認整合"), command=self.confirm_aggregate)
        self.aggregate_button.pack(side="left")
        self.aggregate_translate_button = ttk.Button(buttons, text=tr("整合＋加入佇列"),
            command=lambda: self.confirm_aggregate(translate_after=True))
        self.aggregate_translate_button.pack(side="left", padx=px(6))

        language_row = ttk.LabelFrame(self.settings_tab, text=tr("介面設定"), padding=px(8))
        language_row.pack(fill="x", padx=px(8), pady=px(8))
        ttk.Label(language_row, text="Language / 表示言語").pack(side="left")
        language_choice = ttk.Combobox(language_row, textvariable=self.ui_language,
            values=tuple(LANGUAGES.values()), state="readonly", width=12)
        language_choice.pack(side="left", padx=px(8))
        language_choice.bind("<<ComboboxSelected>>", lambda _event: self.save_settings())
        ttk.Label(language_row, text=tr("重啟後套用")).pack(anchor="w")

        export = ttk.LabelFrame(self.export_tab, text=tr("Komga 匯出"), padding=px(8))
        export.pack(fill="x", padx=px(10), pady=px(6))
        komga_row = ttk.Frame(export)
        komga_row.pack(fill="x")
        ttk.Label(komga_row, text=tr("Komga 輸出路徑：")).pack(side="left")
        self.komga_entry = ttk.Entry(komga_row, textvariable=self.komga_path)
        self.komga_entry.pack(side="left", fill="x", expand=True)
        self.browse_komga_button = ttk.Button(
            komga_row, text=tr("瀏覽"), command=self.browse_komga)
        self.browse_komga_button.pack(side="left", padx=px(6, 0))
        options = ttk.Frame(export)
        options.pack(fill="x", pady=px(6))
        self.skip_checkbox = ttk.Checkbutton(
            options, text=tr("已存在且來源未變更時跳過"), variable=self.skip_unchanged, command=self.save_settings)
        self.skip_checkbox.pack(side="left")
        self.open_checkbox = ttk.Checkbutton(
            options, text=tr("匯出完成後開啟輸出資料夾"), variable=self.open_after_export, command=self.save_settings)
        self.open_checkbox.pack(side="left", padx=px(12))
        buttons = ttk.Frame(export)
        buttons.pack(fill="x")
        self.export_selected_button = ttk.Button(buttons, text=tr("匯出選取項目"), style="Accent.TButton",
                                                 command=self.export_selected)
        self.export_selected_button.pack(side="left")
        self.export_all_button = ttk.Button(buttons, text=tr("匯出所有已翻譯項目"), command=self.export_all)
        self.export_all_button.pack(side="left", padx=px(6))
        # Cleanup deletes cache folders, so it sits on its own row apart from the export actions.
        self.cleanup_button = ttk.Button(
            export, text=tr("清理已匯出快取"), command=self.confirm_cleanup)
        self.cleanup_button.pack(anchor="w", pady=px(6, 0))
        self.progress = ttk.Progressbar(export, mode="determinate")
        self.progress.pack(fill="x", pady=px(8, 2))
        self.status_label = ttk.Label(export, textvariable=self.status_text)
        self.status_label.pack(anchor="w", pady=px(6, 0))
        self.progress.pack_forget()

    def remember_window_size(self, event):
        if event.widget != self.root:
            return
        state = self.root.state()
        if state in ("normal", "zoomed"):
            self.window_maximized = state == "zoomed"
            if state == "normal" and event.width >= px(820) and event.height >= px(640):
                # Saved in 96-DPI pixels so the size survives a display scaling change.
                self.window_size = [round(event.width / ui_theme.SCALE), round(event.height / ui_theme.SCALE)]

    def save_settings(self):
        try:
            save_json(settings_path(), {
                "manga_path": self.base_path.get(),
                "komga_path": self.komga_path.get(),
                "remove_sources_after_aggregate": self.remove_sources_after_aggregate.get(),
                "keep_last_source": self.keep_last_source.get(),
                "skip_unchanged": self.skip_unchanged.get(),
                "open_after_export": self.open_after_export.get(),
                "series_sort": self.series_sort,
                "series_sort_descending": self.series_sort_descending,
                "window_size": self.window_size,
                "window_maximized": self.window_maximized,
                **({"ui_language": next(code for code, label in LANGUAGES.items()
                                        if label == self.ui_language.get())}
                   if hasattr(self, "ui_language") else {}),
                **(self.translation_queue.settings() if hasattr(self, "translation_queue") else {}),
            })
            return True
        except OSError as error:
            logger.exception("settings_save_failed path=%s", settings_path())
            messagebox.showerror(
                tr("設定保存失敗"),
                tr("無法寫入 EXE 同目錄的設定檔：\n{0}\n\n{1}").format(settings_path(), error))
            return False

    def report_callback_exception(self, exception_type, error, traceback):
        logger.error("ui_callback_failed", exc_info=(exception_type, error, traceback))
        messagebox.showerror(tr("Comic sorting 錯誤"), redact(str(error)) + tr("；紀錄：{0}").format(log_path()))

    def export_manga_names(self):
        if not self.scan_data or not self.series_groups:
            messagebox.showinfo(tr("匯出漫畫名稱"), tr("目前沒有可匯出的漫畫，請先掃描漫畫路徑。"), parent=self.root)
            return
        base = self.scan_data[0]
        names = [series.name if series == base else str(series.relative_to(base))
                 for series in self.sorted_series(base)]
        target = filedialog.asksaveasfilename(
            parent=self.root, title=tr("匯出漫畫名稱"), defaultextension=".txt",
            initialfile="manga-names.txt", filetypes=[("TXT", "*.txt")])
        if not target:
            return
        try:
            Path(target).write_text("\n".join(names) + "\n", encoding="utf-8-sig")
        except OSError as error:
            messagebox.showerror(tr("匯出漫畫名稱"), tr("匯出失敗：{0}").format(error), parent=self.root)
            return
        messagebox.showinfo(tr("匯出漫畫名稱"), tr("已匯出 {0} 個漫畫名稱：\n{1}").format(len(names), target), parent=self.root)

    def update_sort_headings(self):
        for column, field, title in (("#0", "name", tr("漫畫 / 章節")),
                                     ("status", "chapters", tr("狀態")),
                                     ("size", "size", tr("資料夾大小")),
                                     ("updated", "updated", tr("更新時間"))):
            if field == self.series_sort:
                title += " ↓" if self.series_sort_descending else " ↑"
            self.folder_tree.heading(column, text=title, command=lambda key=field: self.sort_by_column(key))

    def sort_by_column(self, field):
        previous = self.series_sort, self.series_sort_descending
        self.series_sort_descending = not self.series_sort_descending if field == self.series_sort else False
        self.series_sort = field
        if not self.save_settings():
            self.series_sort, self.series_sort_descending = previous
            return
        self.update_sort_headings()
        if self.scan_data:
            items = {self.tree_items[item][1]: item for item in self.folder_tree.get_children()}
            for index, series in enumerate(self.sorted_series(self.scan_data[0]), 1):
                if series in items:
                    item = items[series]
                    name = self.folder_tree.item(item, "text").partition(". ")[2]
                    self.folder_tree.item(item, text=f"{index}. {name}")
                    self.folder_tree.move(item, "", "end")

    def close(self):
        if self.manga_busy:
            messagebox.showwarning(tr("工作執行中"), tr("請等待目前工作完成，或先停止翻譯佇列。"))
            return
        if self.save_settings():
            logger.info("application_close")
            self.translation_queue.cancel_sash_animation()
            self.root.destroy()

    def browse(self):
        selected = filedialog.askdirectory(initialdir=self.base_path.get() or None)
        if selected:
            self.base_path.set(selected)
            self.save_settings()
            self.load_folders()

    def browse_komga(self):
        selected = filedialog.askdirectory(initialdir=self.komga_path.get() or None)
        if selected:
            self.komga_path.set(selected)
            self.save_settings()

    def load_folders(self):
        value = self.base_path.get().strip()
        base = Path(value).resolve()
        if not value or not base.is_dir():
            messagebox.showwarning(tr("警告"), tr("請選擇有效的漫畫路徑"))
            return
        if self.manga_busy:
            return
        self.base_path.set(str(base))
        if not self.save_settings():
            return
        self.set_manga_busy(True)
        self.show_scan_progress("indeterminate")
        self.scan_progress.start(12)
        self.scan_started = time.monotonic()
        self.scan_status_text.set(tr("正在掃描…"))
        self.scan_count = 0
        threading.Thread(target=self.scan_worker, args=(base,), daemon=True).start()
        self.root.after(50, self.poll_scan_events)

    def source_is_current(self):
        if self.scan_data and self.base_path.get().strip() and Path(self.base_path.get()).resolve() != self.scan_data[0]:
            messagebox.showwarning(tr("警告"), tr("路徑已變更，請先成功重新掃描後再操作。"), parent=self.root)
            return False
        return True

    def set_manga_busy(self, busy):
        self.manga_busy = busy
        state = "disabled" if busy else "normal"
        for widget in (self.path_entry, self.browse_button, self.rescan_button,
                       self.start_entry, self.end_entry, self.aggregate_button,
                       self.aggregate_start_entry, self.aggregate_end_entry, self.aggregate_translate_button,
                       self.remove_sources_checkbox, self.keep_last_source_checkbox,
                       self.komga_entry, self.browse_komga_button,
                       self.skip_checkbox, self.open_checkbox,
                       self.export_selected_button, self.export_all_button,
                       self.cleanup_button):
            widget.configure(state=state)
        if hasattr(self, "translation_queue"):
            self.translation_queue.update_controls()
        if not busy and self.has_explicit_chapter_selection():
            for entry in self.range_entries():
                entry.configure(state="disabled")

    def range_entries(self):
        # The merge window shares the range variables, so both ignore them together.
        return self.start_entry, self.end_entry, self.aggregate_start_entry, self.aggregate_end_entry

    def show_scan_progress(self, mode):
        self.scan_progress.configure(mode=mode, value=0)
        self.scan_progress.pack(
            side="left", fill="x", expand=True, before=self.scan_status_label)

    def hide_scan_progress(self):
        self.scan_progress.stop()
        self.scan_progress.pack_forget()

    def show_export_progress(self, mode):
        self.progress.configure(mode=mode, value=0)
        self.progress.pack(fill="x", pady=px(8, 2), before=self.status_label)

    def hide_export_progress(self):
        self.progress.stop()
        self.progress.pack_forget()

    def scan_worker(self, base):
        try:
            logger.info("scan_start path=%s", base)
            data = self.scan_folder_data(base, progress=lambda count: self.scan_events.put(("progress", count)))
            self.scan_events.put(("done", base, data))
            logger.info("scan_done path=%s", base)
        except Exception as error:
            logger.exception("scan_failed path=%s", base)
            self.scan_events.put(("error", error))

    def poll_scan_events(self):
        count = self.scan_count
        while True:
            try:
                event = self.scan_events.get_nowait()
            except queue.Empty:
                elapsed = time.monotonic() - self.scan_started
                self.scan_status_text.set(tr("正在掃描…") + (f" {count:,}" if count else "") + f"｜{elapsed:.1f} s")
                self.root.after(50, self.poll_scan_events)
                return
            if event[0] != "progress":
                break
            count = self.scan_count = event[1]
        self.hide_scan_progress()
        self.set_manga_busy(False)
        if event[0] == "error":
            if self.scan_data:
                self.base_path.set(str(self.scan_data[0]))
            self.scan_status_text.set(tr("掃描失敗"))
            messagebox.showerror(tr("掃描失敗"), str(event[1]))
            return
        _, base, data = event
        self.base_path.set(str(base))
        self.apply_scan_data(base, data)
        self.translation_queue.render(refresh_counts=True)
        self.scan_status_text.set(self.scan_status_text.get() + f"｜{time.monotonic() - self.scan_started:.1f} s")

    scan_folder_data = staticmethod(scan_folder_data)

    def sorted_series(self, base):
        def sort_key(series):
            chapters = self.series_groups[series]
            if self.series_sort == "name":
                return natural_sort_key(str(series.relative_to(base)))
            if self.series_sort == "size":
                return sum(self.chapter_sizes[Path(item[0])] for item in chapters)
            if self.series_sort == "chapters":
                return len(chapters)
            return max(self.chapter_updates[Path(item[0])] for item in chapters)

        return sorted(self.series_groups, key=sort_key, reverse=self.series_sort_descending)

    def apply_scan_data(self, base, data):
        view = self.folder_tree.yview() if self.scan_data and self.scan_data[0] == base else ()
        self.scan_data = (base, data)
        selected_paths = {self.tree_items[item][1] for item in self.folder_tree.selection()}
        # A search opens every match, so only remember expansion from an unfiltered tree.
        if not self.applied_query:
            self.expanded_series = {self.tree_items[item][1] for item in self.folder_tree.get_children()
                                    if self.folder_tree.item(item, "open")}
        query = self.search_text.get().strip().casefold()
        self.applied_query = query
        (self.folders, self.series_groups, self.chapter_updates,
         self.chapter_sizes, self.ready_chapters, details, self.chapter_page_counts) = data
        existing_items = self.folder_tree.get_children()
        self.series_choices = {self.series_name(series): series for series in self.sorted_series(base)}
        self.manga_choice.configure(values=tuple(self.series_choices))
        if existing_items:
            self.folder_tree.delete(*existing_items)
        self.tree_items = {}
        self.pending_chapters = {}
        selected_parents = []
        for series_index, series in enumerate(self.series_choices.values(), 1):
            visible = {item[0] for item in self.series_groups[series]
                       if not query or query in str(series).casefold() or query in item[1].casefold()}
            if not visible:
                continue
            chapters = [Path(item[0]) for item in self.series_groups[series]]
            modified = max(self.chapter_updates[chapter] for chapter in chapters)
            series_name = self.series_name(series)
            parent = self.folder_tree.insert(
                "", "end", text=f"{series_index}. {series_name}", open=bool(query) or series in self.expanded_series,
                values=("", tr("{0} 單一｜{1} 整合").format(
                            sum(folder_kind(name) == "chapter" for _, name, _ in self.series_groups[series]),
                            sum(folder_kind(name) == "merged" for _, name, _ in self.series_groups[series])),
                        format_size(sum(self.chapter_sizes[chapter] for chapter in chapters)),
                        datetime.fromtimestamp(modified).strftime("%Y-%m-%d %H:%M:%S"),
                        f"{sum(self.chapter_page_counts[chapter] for chapter in chapters):,}"))
            self.tree_items[parent] = ("series", series)
            if series in selected_paths:
                selected_parents.append(parent)
            self.pending_chapters[parent] = (visible, selected_paths)
            self.folder_tree.insert(parent, 'end')
            if self.folder_tree.item(parent, 'open') or any(chapter in selected_paths for chapter in chapters
                                                            if str(chapter) in visible):
                self.load_series_children(parent)
        if selected_parents:
            self.folder_tree.selection_add(selected_parents)
        self.scan_status_text.set(tr("{0} 部漫畫 · {1} 個資料夾").format(
            len(self.series_groups), len(self.folders)))

        if view:
            self.folder_tree.yview_moveto(view[0])
        self.on_tree_select()

    def load_series_children(self, parent):
        pending = self.pending_chapters.pop(parent, None)
        if pending is None:
            return
        visible, selected = pending
        children = ttk.Treeview.get_children(self.folder_tree, parent)
        if children:
            self.folder_tree.delete(*children)
        series = self.tree_items[parent][1]
        details = self.scan_data[1][5]
        chosen = []
        for index, (path, name, _number) in enumerate(self.series_groups[series], 1):
            if path not in visible:
                continue
            chapter = Path(path)
            kind = folder_kind(name)
            item = self.folder_tree.insert(parent, 'end', text=f'{index}. {name}',
                values=(tr(FOLDER_KINDS[kind]), details[chapter], format_size(self.chapter_sizes[chapter]),
                        datetime.fromtimestamp(self.chapter_updates[chapter]).strftime('%Y-%m-%d %H:%M:%S'),
                        f'{self.chapter_page_counts[chapter]:,}'))
            self.tree_items[item] = (kind, chapter)
            if chapter in selected:
                chosen.append(item)
        # One call: each selection change queues a <<TreeviewSelect>> that reruns on_tree_select.
        if chosen:
            self.folder_tree.selection_add(chosen)

    def show_folder_context_menu(self, event):
        if self.folder_tree.identify_region(event.x, event.y) not in ("tree", "cell"):
            return
        item = self.folder_tree.identify_row(event.y)
        if item not in self.tree_items or not self.scan_data:
            return
        self.folder_tree.selection_set(item)
        self.folder_tree.focus(item)
        kind, path = self.tree_items[item]
        base = self.scan_data[0]
        disabled = self.manga_busy or self.translation_queue.running or path == base
        self.folder_context_menu.entryconfigure(
            0, label=tr("刪除系列資料夾…") if kind == "series" else
            tr("刪除整合資料夾…") if kind == "merged" else tr("刪除章節資料夾…"),
            state="disabled" if disabled else "normal",
            command=lambda: self.confirm_delete_folder(kind, path, base))
        for index, (target, label) in enumerate(((None, "全選所有資料夾"), ("chapter", "全選單一章節"),
                                                ("merged", "全選整合資料夾")), 1):
            available = (kind == "series" and not self.manga_busy and not self.translation_queue.running
                         and any(target is None or folder_kind(name) == target
                                 for _, name, _ in self.series_groups[path]))
            self.folder_context_menu.entryconfigure(
                index, label=tr(label) + (tr("（清除搜尋）") if self.search_text.get().strip() else ""),
                state="normal" if available else "disabled",
                command=lambda target=target: self.select_all_chapters(path, base, target))
        try:
            self.folder_context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.folder_context_menu.grab_release()

    def select_all_chapters(self, series, base, kind=None):
        if (self.manga_busy or self.translation_queue.running or not self.scan_data
                or self.scan_data[0] != base or series not in self.series_groups):
            return
        if self.search_text.get().strip():
            self.search_text.set("")
            self.apply_filter()
        parent = next((item for item in self.folder_tree.get_children()
                       if self.tree_items[item] == ("series", series)), None)
        if parent is None:
            return
        children = [item for item in self.folder_tree.get_children(parent)
                    if kind is None or self.tree_items[item][0] == kind]
        self.folder_tree.item(parent, open=True)
        self.folder_tree.selection_set(children)
        self.folder_tree.focus(parent)
        self.folder_tree.see(parent)
        self.on_tree_select()

    def confirm_delete_folder(self, kind, path, base):
        if not self.source_is_current():
            return
        if (self.manga_busy or self.translation_queue.running or path == base
                or not self.scan_data or self.scan_data[0] != base):
            return
        scope = (tr("整個系列及其下所有章節（包含搜尋未顯示的章節）")
                 if kind == "series" else tr("此整合資料夾及其所有檔案")
                 if kind == "merged" else tr("此章節及其所有檔案"))
        if not messagebox.askyesno(
                tr("確認移至資源回收筒"),
                tr("將移至資源回收筒：\n{0}\n\n範圍：{1}\n成功後才會移除相關佇列項目。\n可至資源回收筒還原。確定繼續嗎？").format(path, scope),
                parent=self.root, default=messagebox.NO):
            return
        self.set_manga_busy(True)
        self.show_scan_progress("indeterminate")
        self.scan_progress.start(12)
        self.scan_status_text.set(tr("正在移至資源回收筒：{0}").format(path.name))
        threading.Thread(target=self.delete_folder_worker, args=(path, base), daemon=True).start()
        self.root.after(50, self.poll_events)

    def delete_folder_worker(self, path, base):
        try:
            logger.info("folder_delete_start path=%s root=%s", path, base)
            delete_manga_folder(path, base)
            logger.info("folder_delete_done path=%s", path)
            self.events.put(("folder_deleted", path))
        except Exception as error:
            logger.exception("folder_delete_failed path=%s", path)
            self.events.put(("folder_delete_error", path, error))

    def filter_folders(self, *_args):
        if self.search_after is not None:
            self.root.after_cancel(self.search_after)
        self.search_after = self.root.after(180, self.apply_filter)

    def apply_filter(self):
        if self.search_after is not None:
            self.root.after_cancel(self.search_after)
        self.search_after = None
        if self.scan_data:
            self.apply_scan_data(*self.scan_data)

    def selected_chapters(self):
        paths = []
        for item in self.folder_tree.selection():
            kind, path = self.tree_items[item]
            if kind == "series":
                paths.extend(self.tree_items[child][1] for child in self.folder_tree.get_children(item))
            else:
                paths.append(path)
        return list(dict.fromkeys(paths))

    def add_chapter_to_queue(self, event):
        if self.folder_tree.identify_region(event.x, event.y) not in ("tree", "cell"):
            return
        item = self.tree_items.get(self.folder_tree.identify_row(event.y))
        if item and item[0] in FOLDER_KINDS:
            if not self.source_is_current():
                return "break"
            self.translation_queue.add_paths([item[1]])
            return "break"

    get_folders_with_numbers = staticmethod(get_folders_with_numbers)

    def selected_tree_item(self):
        selection = self.folder_tree.selection()
        if not selection:
            return None
        kind, path = self.tree_items[selection[0]]
        series = path if kind == "series" else path.parent
        return kind, path, self.series_groups[series]

    def has_explicit_chapter_selection(self):
        selection = self.folder_tree.selection()
        return len(selection) > 1 and all(self.tree_items[item][0] in FOLDER_KINDS for item in selection)

    def fit_folder_columns(self, event):
        if self.translation_queue.sash_after is not None:
            return
        width = event.width - 4
        if width < 100 or getattr(self, "_library_width", None) == width:
            return
        self._library_width = width
        font = tkfont.nametofont("TkDefaultFont", root=self.root)
        pages = max(px(65), font.measure(tr("頁數")) + px(16))
        self.folder_tree.column("pages", width=pages, minwidth=pages, stretch=False)
        self.folder_tree.column("#0", width=max(px(100), width - pages), minwidth=px(100), stretch=False)

    def on_tree_select(self, _event=None):
        selection = self.folder_tree.selection()
        invalid = [item for item in selection if item not in self.tree_items]
        if invalid:
            self.folder_tree.selection_remove(invalid)
            selection = self.folder_tree.selection()
        if selection and selection[0] in self.tree_items:
            item = selection[0]
            path = self.tree_items[item][1]
            values = self.folder_tree.item(item, "values")
            kind = self.tree_items[item][0]
            preview = ""
            if len(values) >= 5:
                status = tr("{0} 個資料夾").format(len(self.series_groups[path])) if kind == "series" else values[1]
                preview = " · ".join(filter(None, (status, tr("{0} 頁").format(values[4]))))
            details = [tr(title) + "：" + str(value) for title, value in zip(
                ("資料夾類型", "狀態", "資料夾大小", "更新時間", "頁數"), values) if value]
            self.folder_details.set("\n".join(filter(None, (path.name, preview, str(path), *details))))
            self.current_series.set(self.series_name(path if kind == 'series' else path.parent))
        else:
            self.folder_details.set("")
            self.current_series.set(tr("請先選擇漫畫"))
        paths = self.selected_chapters()
        self.selection_text.set(tr("已選取 {0} 個資料夾").format(len(paths)))
        if hasattr(self, "translation_queue"):
            self.translation_queue.update_controls()
        if self.manga_busy:
            return
        for entry in self.range_entries():
            entry.configure(state="normal")
        selected = self.selected_tree_item()
        if not selected:
            for entry in (self.start_entry, self.end_entry):
                entry.delete(0, tk.END)
            return
        kind, path, chapters = selected
        selected_paths = set(paths)
        selected_indices = [index for index, item in enumerate(chapters, 1) if Path(item[0]) in selected_paths]
        start, end = min(selected_indices), max(selected_indices)
        for entry, value in ((self.start_entry, start), (self.end_entry, end)):
            entry.delete(0, tk.END)
            entry.insert(0, str(value))
        if self.has_explicit_chapter_selection():
            for entry in self.range_entries():
                entry.configure(state="disabled")

    def confirm_aggregate(self, translate_after=False):
        if getattr(self, "manga_busy", False):
            return
        if not self.source_is_current():
            return
        selected = self.selected_tree_item()
        if not selected:
            messagebox.showwarning(tr("警告"), tr("請先選擇父系列或章節"))
            return
        if len({path.parent for path in self.selected_chapters()}) > 1:
            messagebox.showwarning(tr("警告"), tr("整合時請只選擇同一系列的章節"))
            return
        _, _, chapters = selected
        if self.has_explicit_chapter_selection():
            paths = set(self.selected_chapters())
            chapters = [item for item in chapters if Path(item[0]) in paths]
            start_idx, end_idx = 0, len(chapters) - 1
        else:
            start, end = self.start_entry.get(), self.end_entry.get()
            if not start.isdigit() or not end.isdigit():
                messagebox.showwarning(tr("警告"), tr("請輸入有效的起始和結束編號"))
                return
            start_idx, end_idx = int(start) - 1, int(end) - 1
        if start_idx < 0 or end_idx >= len(chapters) or start_idx > end_idx:
            messagebox.showwarning(tr("警告"), tr("請確保編號範圍有效"))
            return
        selected_chapters = chapters[start_idx:end_idx + 1]
        skipped = [name for _, name, _ in selected_chapters if folder_kind(name) == "merged"]
        chapters = [item for item in selected_chapters if folder_kind(item[1]) != "merged"]
        if not chapters:
            messagebox.showinfo(tr("確認整合"), tr("選取範圍內都是整合資料夾，已全部略過，沒有可整合的單一章節。"))
            return
        start_idx, end_idx = 0, len(chapters) - 1
        try:
            output = aggregate_output(chapters[start_idx:end_idx + 1])
        except ValueError as error:
            messagebox.showwarning(tr("警告"), str(error))
            return
        names = [chapters[index][1] for index in range(start_idx, end_idx + 1)]
        remove_sources = self.remove_sources_after_aggregate.get()
        keep_last = self.keep_last_source.get()
        confirmation = tr("您確定要整合以下資料夾嗎？\n\n{0}").format(summarize_names(names))
        if skipped:
            confirmation += tr("\n\n已略過 {0} 個整合資料夾（不作為來源）：\n{1}").format(len(skipped), summarize_names(skipped))
        if output.exists() and output not in {Path(item[0]).resolve() for item in chapters[start_idx:end_idx + 1]}:
            confirmation += tr("\n\n既有輸出將被取代（包含其中的翻譯結果）：{0}").format(output.name)
        if remove_sources:
            if keep_last:
                confirmation += tr("\n\n注意：整合成功後，將永久刪除本次範圍內除最後一個之外的來源資料夾與內容。\n保留：{0}").format(names[-1])
            else:
                confirmation += tr("\n\n注意：整合成功後，將永久刪除本次範圍內的所有來源資料夾與內容（整合輸出除外）。")
        if messagebox.askyesno(tr("確認整合"), confirmation):
            self.translate_after = translate_after
            self.set_manga_busy(True)
            self.show_scan_progress("determinate")
            self.scan_progress.configure(maximum=1)
            self.scan_status_text.set(tr("準備整合…"))
            threading.Thread(
                target=self.aggregate_worker,
                args=(chapters, start_idx, end_idx, remove_sources, keep_last), daemon=True).start()
            self.root.after(50, self.poll_aggregate_events)

    aggregate_folders = staticmethod(aggregate_folders)

    def aggregate_worker(self, chapters, start_idx, end_idx, remove_sources=False, keep_last=True):
        try:
            logger.info("aggregate_start sources=%s cleanup=%s keep_last=%s",
                        [item[0] for item in chapters[start_idx:end_idx + 1]], remove_sources, keep_last)
            output = self.aggregate_folders(
                chapters, start_idx, end_idx,
                lambda current, total: self.aggregate_events.put(
                    ("progress", current, total)))
            cleanup = ([], [])
            if remove_sources:
                cleanup = remove_aggregated_folders(
                    chapters, start_idx, end_idx, output, keep_last=keep_last)
            self.aggregate_events.put(("done", output, cleanup))
            logger.info("aggregate_done output=%s cleanup_errors=%s", output, cleanup[1])
        except Exception as error:
            logger.exception("aggregate_failed")
            self.aggregate_events.put(("error", error))

    def notify_operation(self, title, message, failed=False):
        try:
            self.translation_queue.notifications.send(
                "failed" if failed else "done", title, message, 4 if failed else 3)
        except Exception:
            logger.warning("operation_notification_failed", exc_info=True)

    def poll_aggregate_events(self):
        done = None
        while True:
            try:
                event = self.aggregate_events.get_nowait()
            except queue.Empty:
                break
            if event[0] == "progress":
                _, current, total = event
                self.scan_progress.configure(maximum=max(total, 1), value=current)
                self.scan_status_text.set(tr("正在整合… {0} / {1}").format(current, total))
            else:
                done = event
        if done is None:
            self.root.after(50, self.poll_aggregate_events)
            return
        self.hide_scan_progress()
        self.set_manga_busy(False)
        if done[0] == "error":
            self.translate_after = False
            self.scan_status_text.set(tr("整合失敗"))
            self.notify_operation(tr("整合失敗"), f"{self.base_path.get()}\n{done[1]}", True)
            messagebox.showerror(tr("整合失敗"), str(done[1]))
            return
        output = done[1]
        cleanup_errors = done[2][1]
        self.notify_operation(tr("整合完成（清理含錯誤）") if cleanup_errors else tr("整合完成"),
                              str(output) + "\n" + "\n".join(cleanup_errors), bool(cleanup_errors))
        if getattr(self, "translate_after", False):
            self.translate_after = False
            if done[2][1]:
                messagebox.showwarning(tr("來源清理失敗"), "\n".join(done[2][1]))
            self.translation_queue.add_paths([output], action="translate")
            self.translation_queue.label.set(tr("整合完成，已加入佇列，等待手動開始"))
            self.load_folders()
            return
        removed, cleanup_errors = done[2]
        summary = tr("檔案已重命名並複製到 {0}").format(output.name)
        if removed:
            summary += tr("\n已清除 {0} 個來源資料夾").format(len(removed))
        if cleanup_errors:
            messagebox.showwarning(
                tr("整合完成（清理含錯誤）"), summary + "\n\n" + "\n".join(cleanup_errors))
        else:
            messagebox.showinfo(tr("完成"), summary)
        self.load_folders()

    def export_selected(self):
        selected = self.selected_chapters()
        if not selected:
            messagebox.showwarning(tr("警告"), tr("請先選擇父系列或章節"))
            return
        ready = [path for path in selected if path in self.ready_chapters]
        if not ready:
            messagebox.showinfo(tr("Komga 匯出"), tr("選取項目沒有可匯出的翻譯結果"))
            return
        self.start_export(ready)

    def export_all(self):
        chapters = [path for path, _, _ in self.folders
                    if Path(path) in self.ready_chapters]
        if not chapters:
            messagebox.showinfo(tr("Komga 匯出"), tr("沒有可匯出的翻譯結果"))
            return
        self.start_export(chapters)

    def start_export(self, chapters):
        if getattr(self, "manga_busy", False):
            return
        if not self.source_is_current():
            return
        source_root = Path(self.base_path.get())
        output_root = Path(self.komga_path.get())
        if not source_root.is_dir() or not self.komga_path.get().strip():
            messagebox.showwarning(tr("警告"), tr("請確認漫畫與 Komga 路徑"))
            return
        if (output_root.resolve().is_relative_to(source_root.resolve())
                or source_root.resolve().is_relative_to(output_root.resolve())):
            messagebox.showwarning(tr("警告"), tr("匯出與漫畫路徑不可互相包含"))
            return
        if not self.save_settings():
            return
        self.work_tabs.select(self.export_tab)
        self.set_manga_busy(True)
        self.show_export_progress("determinate")
        self.progress.configure(maximum=1)
        self.status_text.set(tr("準備匯出…"))
        options = (self.komga_path.get(), self.skip_unchanged.get())
        threading.Thread(target=self.export_worker, args=(chapters, *options), daemon=True).start()
        self.root.after(50, self.poll_events)

    def export_worker(self, chapters, komga_path, skip_unchanged):
        logger.info("export_start chapters=%s output=%s skip_unchanged=%s", len(chapters), komga_path, skip_unchanged)
        root = Path(komga_path)
        state_file = root / ".comic-sorting-state.json"
        counts = {"created": 0, "updated": 0, "skipped": 0, "failed": 0}
        try:
            state = load_json(state_file, {}, strict=True)
        except ValueError as error:
            logger.exception("export_state_read_failed path=%s", state_file)
            counts["failed"] = len(chapters)
            self.events.put(("done", counts, [str(error)], str(root), None))
            return
        errors = []
        output_folders = set()
        for chapter in chapters:
            chapter = Path(chapter)
            name = chapter.name
            try:
                action, output = export_chapter(
                    chapter.parent, chapter, root, state, skip_unchanged,
                    lambda current, total, chapter_name=name: self.events.put(
                        ("progress", chapter_name, current, total)))
                counts[action] += 1
                output_folders.add(output.parent)
                logger.info("export_%s source=%s output=%s", action, chapter, output)
                self.events.put(("chapter", action, name))
            except Exception as error:
                logger.exception("export_failed source=%s", chapter)
                counts["failed"] += 1
                errors.append(f"{name}: {error}")
                self.events.put(("chapter", "failed", name))
        try:
            save_json(state_file, state)
        except Exception as error:
            logger.exception("export_state_save_failed path=%s", state_file)
            errors.append(tr("狀態檔: {0}").format(error))
        output_folder = output_folders.pop() if len(output_folders) == 1 else root
        logger.info("export_end counts=%s errors=%s", counts, errors)
        self.events.put(("done", counts, errors, str(output_folder), None))

    def confirm_cleanup(self):
        if self.manga_busy:
            return
        if not self.source_is_current():
            return
        value = self.base_path.get().strip()
        root = Path(value)
        if not value or not root.is_dir():
            messagebox.showwarning(tr("警告"), tr("請選擇有效的漫畫路徑"))
            return
        output = self.komga_path.get().strip()
        if not output or not Path(output).is_dir():
            messagebox.showwarning(tr("清理已匯出快取"), tr("請設定 Komga 輸出路徑"), parent=self.root)
            self.work_tabs.select(self.export_tab)
            return
        if not messagebox.askyesno(
                tr("確認清理"),
                tr("只清理已完整匯出且 CBZ 驗證通過的 mask / inpainted。\n未匯出、檔案已變更或驗證失敗的章節會保留。\n清理無法復原，確定繼續？") + "\n\n" + str(root.resolve()), parent=self.root):
            return
        self.set_manga_busy(True)
        self.show_export_progress("indeterminate")
        self.progress.start(12)
        self.status_text.set(tr("正在清理 mask / inpainted…"))
        threading.Thread(target=self.cleanup_worker, args=(root, output), daemon=True).start()
        self.root.after(50, self.poll_events)

    def cleanup_worker(self, root, output):
        try:
            logger.info("cleanup_start path=%s", root)
            result = clear_work_folders(root, output)
            logger.info("cleanup_end path=%s result=%s", root, result)
            self.events.put(("cleanup_done", result))
        except Exception as error:
            logger.exception("cleanup_failed path=%s", root)
            self.events.put(("cleanup_error", error))

    def poll_events(self):
        done = False
        rescan = False
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            if event[0] == "progress":
                _, chapter, current, total = event
                self.progress.configure(maximum=total, value=current)
                self.status_text.set(tr("正在處理：{0} — {1} / {2}").format(chapter, current, total))
            elif event[0] == "chapter":
                _, action, chapter = event
                action_label = {"created": tr("新增"), "updated": tr("更新"),
                                "skipped": tr("跳過"), "failed": tr("失敗")}[action]
                self.status_text.set(f"[{action_label}] {chapter}")
            elif event[0] in ("folder_deleted", "folder_delete_error"):
                self.hide_scan_progress()
                if event[0] == "folder_deleted":
                    q = self.translation_queue
                    q.jobs = [job for job in q.jobs if not job.path.is_relative_to(event[1])]
                    q.changed()
                    self.status_text.set(tr("已移至資源回收筒：{0}").format(event[1]))
                else:
                    self.status_text.set(tr("移至資源回收筒失敗"))
                    messagebox.showerror(tr("移至資源回收筒失敗"), f"{event[1]}\n\n{event[2]}", parent=self.root)
                done = True
                rescan = True
            elif event[0] == "cleanup_done":
                _, (folders, removed, errors) = event
                summary = tr("已清理 {0} 個資料夾、移除 {1} 個項目").format(folders, removed)
                self.hide_export_progress()
                self.status_text.set(summary)
                self.notify_operation(tr("清理完成（含錯誤）") if errors else tr("清理完成"),
                                      self.base_path.get() + "\n" + summary + "\n" + "\n".join(errors), bool(errors))
                if errors:
                    messagebox.showerror(tr("清理完成（含錯誤）"), summary + "\n\n" + "\n".join(errors))
                else:
                    messagebox.showinfo(tr("清理完成"), summary)
                done = True
                rescan = removed > 0
            elif event[0] == "cleanup_error":
                self.hide_export_progress()
                self.status_text.set(tr("清理失敗"))
                self.notify_operation(tr("清理失敗"), f"{self.base_path.get()}\n{event[1]}", True)
                messagebox.showerror(tr("清理失敗"), str(event[1]))
                done = True
            else:
                _, counts, errors, output_folder, cleanup = event
                self.hide_export_progress()
                summary = (tr("新增：{0}  更新：{1}  跳過：{2}  失敗：{3}").format(counts['created'], counts['updated'], counts['skipped'], counts['failed']))
                if cleanup:
                    summary += tr("  清理：{0} 個資料夾／{1} 個項目").format(cleanup[0], cleanup[1])
                self.status_text.set(summary)
                self.notify_operation(tr("Komga 匯出完成（含錯誤）") if errors else tr("Komga 匯出完成"),
                                      str(output_folder) + "\n" + summary + "\n" + "\n".join(errors),
                                      bool(errors or counts['failed']))
                if errors:
                    messagebox.showerror(tr("Komga 匯出完成（含錯誤）"), summary + "\n\n" + "\n".join(errors))
                else:
                    messagebox.showinfo(tr("Komga 匯出完成"), summary)
                if self.open_after_export.get() and Path(output_folder).is_dir():
                    try:
                        os.startfile(output_folder)
                    except OSError as error:
                        logger.exception("export_result_open_failed path=%s", output_folder)
                        messagebox.showwarning(tr("無法開啟結果位置"), f"{output_folder}\n\n{error}")
                done = True
                rescan = bool(cleanup and cleanup[1])
        if done:
            self.set_manga_busy(False)
            if rescan:
                self.load_folders()
        else:
            self.root.after(50, self.poll_events)


if __name__ == "__main__":
    instance = None
    try:
        if os.name == "nt":
            from app_instance import AppInstance
            instance = AppInstance(settings_path())
            if not instance.acquire():
                instance.activate_existing()
                sys.exit(0)
        configure_logging()
        logger.info("application_start executable=%s", sys.executable)
        if os.name == "nt":
            try:
                # System-aware DPI lets Tk draw sharp text; ui_theme.px scales the fixed layout sizes.
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except (AttributeError, OSError):
                pass
        root = tk.Tk()
        app = FileAggregatorApp(root)
        if app.window_size is None:
            root.geometry(f"{min(px(1280), root.winfo_screenwidth())}x{min(px(820), root.winfo_screenheight())}")
        if instance is not None:
            instance.register_window(root)
        root.mainloop()
    except Exception as error:
        logger.exception("application_failed")
        try:
            messagebox.showerror(tr("Comic sorting 錯誤"), str(error))
        except Exception:
            pass
    finally:
        if instance is not None:
            instance.close()
