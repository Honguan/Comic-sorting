import os
import queue
import shutil
import sys
import threading
from datetime import datetime
from decimal import Decimal
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from translation_queue import TranslationQueue
from app_logging import configure_logging, logger, log_path, redact
from ui_language import LANGUAGES, set_language, tr
from comic_core import (
    IMAGE_EXTENSIONS, chapter_number, image_files, translation_status,
    natural_sort_key, updated_at, folder_size, format_size, summarize_names,
    clear_work_folders, remove_aggregated_folders, source_fingerprint,
    output_path_for, validate_cbz, create_cbz, load_json, save_json, export_chapter,
    aggregate_output, is_link_or_junction,
)


SETTINGS_FILENAME = "comic-sorting.settings.json"


def resource_path(relative_path):
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    return base / relative_path


def settings_path():
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
    return base / SETTINGS_FILENAME


class FileAggregatorApp:
    def __init__(self, root):
        self.root = root
        self.root.report_callback_exception = self.report_callback_exception
        settings = load_json(settings_path(), {})
        language = settings.get("ui_language", "zh-TW")
        set_language(language)
        self.ui_language = tk.StringVar(value=LANGUAGES.get(language, LANGUAGES["zh-TW"]))
        icon = resource_path("assets/comic-sorting.ico")
        if icon.is_file():
            self.root.iconbitmap(default=icon)
        self.root.title(tr("漫畫整合工具"))
        self.root.minsize(820, 640)
        self.folders = []
        self.series_groups = {}
        self.tree_items = {}
        self.chapter_updates = {}
        self.chapter_sizes = {}
        self.ready_chapters = set()
        self.events = queue.Queue()
        self.scan_events = queue.Queue()
        self.aggregate_events = queue.Queue()
        self.manga_busy = False
        self.base_path = tk.StringVar(value=settings.get("manga_path", ""))
        self.komga_path = tk.StringVar(value=settings.get("komga_path", ""))
        self.skip_unchanged = tk.BooleanVar(value=settings.get("skip_unchanged", True) is True)
        self.open_after_export = tk.BooleanVar(value=settings.get("open_after_export", False) is True)
        self.remove_sources_after_aggregate = tk.BooleanVar(
            value=settings.get("remove_sources_after_aggregate") is True)
        self.status_text = tk.StringVar(value=tr("就緒"))
        self.search_text = tk.StringVar()
        self.scan_data = None
        self.search_after = None

        language_row = ttk.Frame(root, padding=(10, 4))
        language_row.pack(fill="x")
        ttk.Label(language_row, text="介面語言 / Language / 表示言語").pack(side="left")
        language_choice = ttk.Combobox(
            language_row, textvariable=self.ui_language, values=tuple(LANGUAGES.values()),
            state="readonly", width=12)
        language_choice.pack(side="left", padx=8)
        language_choice.bind("<<ComboboxSelected>>", lambda _event: self.save_settings())
        ttk.Label(language_row, text=tr("重新啟動後套用語言")).pack(side="left")

        self.panes = ttk.Panedwindow(root, orient="vertical")
        self.panes.pack(fill="both", expand=True, padx=10, pady=6)
        manga = ttk.LabelFrame(self.panes, text=tr("漫畫整合"), padding=8)
        self.panes.add(manga, weight=1)
        path_row = ttk.Frame(manga)
        path_row.pack(fill="x")
        ttk.Label(path_row, text=tr("漫畫路徑：")).pack(side="left")
        self.path_entry = ttk.Entry(path_row, textvariable=self.base_path)
        self.path_entry.pack(side="left", fill="x", expand=True)
        self.browse_button = ttk.Button(path_row, text=tr("瀏覽"), command=self.browse)
        self.browse_button.pack(side="left", padx=(6, 0))
        self.rescan_button = ttk.Button(path_row, text=tr("重新掃描"), command=self.load_folders)
        self.rescan_button.pack(side="left", padx=(6, 0))
        self.path_entry.bind("<Return>", lambda _event: self.load_folders())
        search_row = ttk.Frame(manga)
        search_row.pack(fill="x", pady=(6, 0))
        ttk.Label(search_row, text=tr("搜尋系列／章節")).pack(side="left")
        self.search_entry = ttk.Entry(search_row, textvariable=self.search_text)
        self.search_entry.pack(side="left", fill="x", expand=True, padx=6)
        self.root.bind("<Control-f>", lambda _event: self.search_entry.focus_set())
        self.search_entry.bind("<Escape>", lambda _event: self.search_text.set(""))
        ttk.Button(search_row, text=tr("清除搜尋"), command=lambda: self.search_text.set("")).pack(side="left", padx=(0, 6))
        self.selection_text = tk.StringVar(value=tr("已選取 {0} 個章節").format(0))
        ttk.Label(search_row, textvariable=self.selection_text).pack(side="left")
        self.search_text.trace_add("write", self.filter_folders)

        manga_footer = ttk.Frame(manga)
        manga_footer.pack(side="bottom", fill="x")
        list_frame = ttk.Frame(manga)
        list_frame.pack(fill="both", expand=True, pady=8)
        self.folder_tree = ttk.Treeview(
            list_frame, columns=("status", "size", "updated"), show="tree headings",
            selectmode="extended", height=8)
        self.folder_tree.heading("#0", text=tr("序號｜系列 / 章節"))
        self.folder_tree.heading("status", text=tr("狀態"))
        self.folder_tree.heading("size", text=tr("資料夾大小"))
        self.folder_tree.heading("updated", text=tr("更新時間 ↓"))
        self.folder_tree.column("#0", width=450, stretch=True)
        self.folder_tree.column("status", width=150, stretch=False)
        self.folder_tree.column("size", width=110, anchor="e", stretch=False)
        self.folder_tree.column("updated", width=150, anchor="center", stretch=False)
        vertical = ttk.Scrollbar(list_frame, command=self.folder_tree.yview)
        horizontal = ttk.Scrollbar(list_frame, orient="horizontal", command=self.folder_tree.xview)
        self.folder_tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.folder_tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        list_frame.rowconfigure(0, weight=1)
        list_frame.columnconfigure(0, weight=1)
        self.folder_tree.bind("<<TreeviewSelect>>", self.on_tree_select)

        scan_row = ttk.Frame(manga_footer)
        scan_row.pack(fill="x")
        self.scan_progress = ttk.Progressbar(scan_row, mode="indeterminate")
        self.scan_progress.pack(side="left", fill="x", expand=True)
        self.scan_status_text = tk.StringVar(value=tr("尚未掃描"))
        self.scan_status_label = ttk.Label(scan_row, textvariable=self.scan_status_text)
        self.scan_status_label.pack(side="left", padx=(8, 0))
        self.scan_progress.pack_forget()

        range_row = ttk.Frame(manga_footer)
        range_row.pack()
        ttk.Label(range_row, text=tr("起始編號：")).pack(side="left")
        self.start_entry = ttk.Entry(range_row, width=6)
        self.start_entry.pack(side="left")
        ttk.Label(range_row, text=tr("結束編號：")).pack(side="left", padx=(10, 0))
        self.end_entry = ttk.Entry(range_row, width=6)
        self.end_entry.pack(side="left")
        self.aggregate_button = ttk.Button(range_row, text=tr("確認整合"), command=self.confirm_aggregate)
        self.aggregate_button.pack(side="left", padx=10)
        self.remove_sources_checkbox = ttk.Checkbutton(
            range_row, text=tr("整合後清除來源（保留最後一個）"),
            variable=self.remove_sources_after_aggregate, command=self.save_settings)
        self.remove_sources_checkbox.pack(side="left")

        self.work_tabs = ttk.Notebook(self.panes)
        self.panes.add(self.work_tabs, weight=1)
        self.queue_tab = ttk.Frame(self.work_tabs)
        self.export_tab = ttk.Frame(self.work_tabs)
        self.settings_tab = ttk.Frame(self.work_tabs)
        self.work_tabs.add(self.queue_tab, text=tr("主佇列"))
        self.work_tabs.add(self.export_tab, text=tr("Komga 匯出"))
        self.work_tabs.add(self.settings_tab, text=tr("設定"))

        export = ttk.LabelFrame(self.export_tab, text=tr("Komga 匯出"), padding=8)
        export.pack(fill="x", padx=10, pady=6)
        komga_row = ttk.Frame(export)
        komga_row.pack(fill="x")
        ttk.Label(komga_row, text=tr("Komga 輸出路徑：")).pack(side="left")
        self.komga_entry = ttk.Entry(komga_row, textvariable=self.komga_path)
        self.komga_entry.pack(side="left", fill="x", expand=True)
        self.browse_komga_button = ttk.Button(
            komga_row, text=tr("瀏覽"), command=self.browse_komga)
        self.browse_komga_button.pack(side="left", padx=(6, 0))
        options = ttk.Frame(export)
        options.pack(fill="x", pady=6)
        self.skip_checkbox = ttk.Checkbutton(
            options, text=tr("已存在且來源未變更時跳過"), variable=self.skip_unchanged, command=self.save_settings)
        self.skip_checkbox.pack(side="left")
        self.open_checkbox = ttk.Checkbutton(
            options, text=tr("匯出完成後開啟輸出資料夾"), variable=self.open_after_export, command=self.save_settings)
        self.open_checkbox.pack(side="left", padx=12)
        buttons = ttk.Frame(export)
        buttons.pack(fill="x")
        self.export_selected_button = ttk.Button(buttons, text=tr("匯出選取項目"), command=self.export_selected)
        self.export_selected_button.pack(side="left")
        self.export_all_button = ttk.Button(buttons, text=tr("匯出所有已翻譯項目"), command=self.export_all)
        self.export_all_button.pack(side="left", padx=6)
        self.cleanup_button = ttk.Button(
            buttons, text=tr("清理所有 mask / inpainted"), command=self.confirm_cleanup)
        self.cleanup_button.pack(side="left")
        self.progress = ttk.Progressbar(export, mode="determinate")
        self.progress.pack(fill="x", pady=(8, 2))
        self.status_label = ttk.Label(export, textvariable=self.status_text)
        self.status_label.pack(anchor="w")
        self.progress.pack_forget()

        self.translation_queue = TranslationQueue(self, settings)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        if self.base_path.get() and Path(self.base_path.get()).is_dir():
            self.load_folders()

    def save_settings(self):
        try:
            save_json(settings_path(), {
                "manga_path": self.base_path.get(),
                "komga_path": self.komga_path.get(),
                "remove_sources_after_aggregate": self.remove_sources_after_aggregate.get(),
                "skip_unchanged": self.skip_unchanged.get(),
                "open_after_export": self.open_after_export.get(),
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

    def close(self):
        if self.manga_busy:
            messagebox.showwarning(tr("工作執行中"), tr("請等待目前工作完成，或先停止翻譯佇列。"))
            return
        if self.save_settings():
            logger.info("application_close")
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
        self.scan_status_text.set(tr("正在掃描…"))
        threading.Thread(target=self.scan_worker, args=(base,), daemon=True).start()
        self.root.after(50, self.poll_scan_events)

    def set_manga_busy(self, busy):
        self.manga_busy = busy
        state = "disabled" if busy else "normal"
        for widget in (self.path_entry, self.browse_button, self.rescan_button,
                       self.start_entry, self.end_entry, self.aggregate_button,
                       self.remove_sources_checkbox,
                       self.komga_entry, self.browse_komga_button,
                       self.skip_checkbox, self.open_checkbox,
                       self.export_selected_button, self.export_all_button,
                       self.cleanup_button):
            widget.configure(state=state)
        if hasattr(self, "translation_queue"):
            self.translation_queue.update_controls()
        if not busy and self.has_explicit_chapter_selection():
            self.start_entry.configure(state="disabled")
            self.end_entry.configure(state="disabled")

    def show_scan_progress(self, mode):
        self.scan_progress.configure(mode=mode, value=0)
        self.scan_progress.pack(
            side="left", fill="x", expand=True, before=self.scan_status_label)

    def hide_scan_progress(self):
        self.scan_progress.stop()
        self.scan_progress.pack_forget()

    def show_export_progress(self, mode):
        self.progress.configure(mode=mode, value=0)
        self.progress.pack(fill="x", pady=(8, 2), before=self.status_label)

    def hide_export_progress(self):
        self.progress.stop()
        self.progress.pack_forget()

    def scan_worker(self, base):
        try:
            logger.info("scan_start path=%s", base)
            self.scan_events.put(("done", base, self.scan_folder_data(base)))
            logger.info("scan_done path=%s", base)
        except Exception as error:
            logger.exception("scan_failed path=%s", base)
            self.scan_events.put(("error", error))

    def poll_scan_events(self):
        try:
            event = self.scan_events.get_nowait()
        except queue.Empty:
            self.root.after(50, self.poll_scan_events)
            return
        self.hide_scan_progress()
        self.set_manga_busy(False)
        if event[0] == "error":
            self.scan_status_text.set(tr("掃描失敗"))
            messagebox.showerror(tr("掃描失敗"), str(event[1]))
            return
        _, base, data = event
        self.apply_scan_data(base, data)

    @staticmethod
    def scan_folder_data(base):
        folders = FileAggregatorApp.get_folders_with_numbers(base)
        series_groups = {}
        chapter_updates = {}
        chapter_sizes = {}
        ready_chapters = set()
        details = {}
        for folder_path, folder_name, number in folders:
            chapter = Path(folder_path)
            status, translated = translation_status(folder_path)
            if status == tr("可匯出"):
                detail = tr("{0} 張翻譯圖片｜可匯出").format(len(translated))
                images = translated
                ready_chapters.add(chapter)
            elif status == tr("結果為空"):
                detail = tr("結果資料夾為空")
                images = []
            elif status == tr("部分翻譯"):
                detail = tr("{0} 張翻譯圖片｜部分翻譯").format(len(translated))
                images = translated
            else:
                images = image_files(folder_path)
                detail = tr("{0} 張圖片｜未翻譯").format(len(images))
            chapter_updates[chapter] = updated_at(chapter, images)
            chapter_sizes[chapter] = folder_size(chapter)
            details[chapter] = detail
            series_groups.setdefault(chapter.parent, []).append(
                (str(chapter), folder_name, number))
        for chapters in series_groups.values():
            chapters.sort(key=lambda item: (item[2], natural_sort_key(item[1])))
        return (folders, series_groups, chapter_updates, chapter_sizes,
                ready_chapters, details)

    def apply_scan_data(self, base, data):
        view = self.folder_tree.yview() if self.scan_data and self.scan_data[0] == base else ()
        self.scan_data = (base, data)
        selected_paths = {self.tree_items[item][1] for item in self.folder_tree.selection()}
        expanded_paths = {self.tree_items[item][1] for item in self.folder_tree.get_children()
                          if self.folder_tree.item(item, "open")}
        query = self.search_text.get().strip().casefold()
        (self.folders, self.series_groups, self.chapter_updates,
         self.chapter_sizes, self.ready_chapters, details) = data
        existing_items = self.folder_tree.get_children()
        if existing_items:
            self.folder_tree.delete(*existing_items)
        self.tree_items = {}
        series_order = sorted(
            self.series_groups,
            key=lambda series: max(self.chapter_updates[Path(item[0])]
                                   for item in self.series_groups[series]),
            reverse=True)
        for series_index, series in enumerate(series_order, 1):
            visible = {item[0] for item in self.series_groups[series]
                       if not query or query in str(series).casefold() or query in item[1].casefold()}
            if not visible:
                continue
            modified = max(self.chapter_updates[Path(item[0])]
                           for item in self.series_groups[series])
            relative_series = series.relative_to(base)
            series_name = series.name if relative_series == Path(".") else str(relative_series)
            parent = self.folder_tree.insert(
                "", "end", text=f"{series_index}. {series_name}", open=bool(query) or series in expanded_paths,
                values=(tr("{0} 個章節").format(len(self.series_groups[series])),
                        format_size(sum(self.chapter_sizes[Path(item[0])]
                                        for item in self.series_groups[series])),
                        datetime.fromtimestamp(modified).strftime("%Y-%m-%d %H:%M:%S")))
            self.tree_items[parent] = ("series", series)
            if series in selected_paths:
                self.folder_tree.selection_add(parent)
            for chapter_index, (folder_path, folder_name, _) in enumerate(
                    self.series_groups[series], 1):
                if folder_path not in visible:
                    continue
                chapter = Path(folder_path)
                item = self.folder_tree.insert(
                    parent, "end", text=f"{chapter_index}. {folder_name}",
                    values=(details[chapter], format_size(self.chapter_sizes[chapter]),
                            datetime.fromtimestamp(
                        self.chapter_updates[chapter]).strftime("%Y-%m-%d %H:%M:%S")))
                self.tree_items[item] = ("chapter", chapter)
                if chapter in selected_paths:
                    self.folder_tree.selection_add(item)
        self.scan_status_text.set(
            tr("{0} 個系列，{1} 個章節").format(len(self.series_groups), len(self.folders)))

        if view:
            self.folder_tree.yview_moveto(view[0])
        self.on_tree_select()

    def filter_folders(self, *_args):
        if self.search_after is not None:
            self.root.after_cancel(self.search_after)
        self.search_after = self.root.after(180, self.apply_filter)

    def apply_filter(self):
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

    @staticmethod
    def get_folders_with_numbers(base_path):
        folders = []
        base = Path(base_path)
        if is_link_or_junction(base):
            return folders
        for current, directory_names, file_names in os.walk(base):
            folder = Path(current)
            has_result = any(name.casefold() == "result" for name in directory_names)
            directory_names[:] = [name for name in directory_names
                                  if name.casefold() not in {"result", "inpainted", "mask"}
                                  and not (name.casefold().startswith(".chapter ")
                                           and name.casefold().endswith((".tmp", ".backup")))
                                  and not is_link_or_junction(folder / name)]
            number = chapter_number(folder.name)
            has_images = any(Path(name).suffix.casefold() in IMAGE_EXTENSIONS
                             for name in file_names)
            if folder != base and number is not None and (has_result or has_images):
                folders.append((str(folder), folder.name, number))
        return sorted(folders, key=lambda item: (
            natural_sort_key(str(Path(item[0]).parent.relative_to(base))),
            item[2], natural_sort_key(item[1])))

    def selected_tree_item(self):
        selection = self.folder_tree.selection()
        if not selection:
            return None
        kind, path = self.tree_items[selection[0]]
        series = path if kind == "series" else path.parent
        return kind, path, self.series_groups[series]

    def has_explicit_chapter_selection(self):
        selection = self.folder_tree.selection()
        return len(selection) > 1 and all(self.tree_items[item][0] == "chapter" for item in selection)

    def on_tree_select(self, _event=None):
        paths = self.selected_chapters()
        self.selection_text.set(tr("已選取 {0} 個章節").format(len(paths)))
        if self.manga_busy:
            return
        for entry in (self.start_entry, self.end_entry):
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
                entry.configure(state="disabled")

    def confirm_aggregate(self, translate_after=False):
        if getattr(self, "manga_busy", False):
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
        try:
            output = aggregate_output(chapters[start_idx:end_idx + 1])
        except ValueError as error:
            messagebox.showwarning(tr("警告"), str(error))
            return
        names = [chapters[index][1] for index in range(start_idx, end_idx + 1)]
        remove_sources = self.remove_sources_after_aggregate.get()
        confirmation = tr("您確定要整合以下資料夾嗎？\n\n{0}").format(summarize_names(names))
        if output.exists() and output not in {Path(item[0]).resolve() for item in chapters[start_idx:end_idx + 1]}:
            confirmation += tr("\n\n既有輸出將被取代（包含其中的翻譯結果）：{0}").format(output.name)
        if remove_sources:
            confirmation += (
                tr("\n\n注意：整合成功後，將永久刪除本次範圍內除最後一個之外的來源資料夾與內容。\n保留：{0}").format(names[-1]))
        if messagebox.askyesno(tr("確認整合"), confirmation):
            self.translate_after = translate_after
            self.set_manga_busy(True)
            self.show_scan_progress("determinate")
            self.scan_progress.configure(maximum=1)
            self.scan_status_text.set(tr("準備整合…"))
            threading.Thread(
                target=self.aggregate_worker,
                args=(chapters, start_idx, end_idx, remove_sources), daemon=True).start()
            self.root.after(50, self.poll_aggregate_events)

    def aggregate_folders(self, chapters, start_idx, end_idx, progress=None):
        selected = chapters[start_idx:end_idx + 1]
        output = aggregate_output(selected)
        if len(selected) == 1 and Path(selected[0][0]).resolve() == output:
            return output
        sources = []
        for folder_path, _, _ in selected:
            images = image_files(folder_path)
            if not images:
                raise ValueError(tr("來源沒有可整合的圖片：{0}").format(folder_path))
            sources.extend(images)
        temporary = output.with_name(f".{output.name}.tmp")
        backup = output.with_name(f".{output.name}.backup")
        if temporary.exists():
            shutil.rmtree(temporary)
        if backup.exists():
            if output.exists():
                shutil.rmtree(backup)
            else:
                os.replace(backup, output)
        if output.exists() and not output.is_dir():
            raise ValueError(tr("輸出路徑不是資料夾：{0}").format(output))
        temporary.mkdir()
        try:
            for index, source in enumerate(sources, 1):
                shutil.copy2(source, temporary / f"{index}{source.suffix}")
                if progress:
                    progress(index, len(sources))
            if output.exists():
                os.replace(output, backup)
            os.replace(temporary, output)
        except Exception:
            if temporary.exists():
                shutil.rmtree(temporary)
            if backup.exists() and not output.exists():
                os.replace(backup, output)
            raise
        if backup.exists():
            try:
                shutil.rmtree(backup)
            except OSError:
                pass
        return output

    def aggregate_worker(self, chapters, start_idx, end_idx, remove_sources=False):
        try:
            logger.info("aggregate_start sources=%s cleanup=%s", [item[0] for item in chapters[start_idx:end_idx + 1]], remove_sources)
            output = self.aggregate_folders(
                chapters, start_idx, end_idx,
                lambda current, total: self.aggregate_events.put(
                    ("progress", current, total)))
            cleanup = ([], [])
            if remove_sources:
                cleanup = remove_aggregated_folders(
                    chapters, start_idx, end_idx, output)
            self.aggregate_events.put(("done", output, cleanup))
            logger.info("aggregate_done output=%s cleanup_errors=%s", output, cleanup[1])
        except Exception as error:
            logger.exception("aggregate_failed")
            self.aggregate_events.put(("error", error))

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
            messagebox.showerror(tr("整合失敗"), str(done[1]))
            return
        output = done[1]
        if getattr(self, "translate_after", False):
            self.translate_after = False
            if done[2][1]:
                messagebox.showwarning(tr("來源清理失敗"), "\n".join(done[2][1]))
            self.translation_queue.add_paths([output], action="translate")
            self.translation_queue.start(cleanup_confirmed=True)
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
        state = load_json(state_file, {})
        counts = {"created": 0, "updated": 0, "skipped": 0, "failed": 0}
        errors = []
        output_folders = set()
        exported_chapters = []
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
                exported_chapters.append(chapter)
                logger.info("export_%s source=%s output=%s", action, chapter, output)
                self.events.put(("chapter", action, name))
            except Exception as error:
                logger.exception("export_failed source=%s", chapter)
                counts["failed"] += 1
                errors.append(f"{name}: {error}")
                self.events.put(("chapter", "failed", name))
        state_saved = True
        try:
            save_json(state_file, state)
        except Exception as error:
            logger.exception("export_state_save_failed path=%s", state_file)
            state_saved = False
            counts["failed"] += 1
            errors.append(tr("狀態檔: {0}").format(error))
        cleanup = None
        if state_saved and exported_chapters:
            folders = removed = 0
            cleanup_errors = []
            for chapter in exported_chapters:
                result = clear_work_folders(chapter)
                folders += result[0]
                removed += result[1]
                cleanup_errors.extend(result[2])
            cleanup = folders, removed, cleanup_errors
            errors.extend(tr("清理失敗：{0}").format(error) for error in cleanup_errors)
        output_folder = output_folders.pop() if len(output_folders) == 1 else root
        logger.info("export_end counts=%s errors=%s cleanup=%s", counts, errors, cleanup)
        self.events.put(("done", counts, errors, str(output_folder), cleanup))

    def confirm_cleanup(self):
        if self.manga_busy:
            return
        value = self.base_path.get().strip()
        root = Path(value)
        if not value or not root.is_dir():
            messagebox.showwarning(tr("警告"), tr("請選擇有效的漫畫路徑"))
            return
        if not messagebox.askyesno(
                tr("確認清理"),
                tr("將清空目前漫畫路徑下所有 mask 與 inpainted 資料夾內容。\n資料夾本身會保留，此操作無法復原。確定繼續嗎？")):
            return
        self.set_manga_busy(True)
        self.show_export_progress("indeterminate")
        self.progress.start(12)
        self.status_text.set(tr("正在清理 mask / inpainted…"))
        threading.Thread(target=self.cleanup_worker, args=(root,), daemon=True).start()
        self.root.after(50, self.poll_events)

    def cleanup_worker(self, root):
        try:
            logger.info("cleanup_start path=%s", root)
            result = clear_work_folders(root)
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
            elif event[0] == "cleanup_done":
                _, (folders, removed, errors) = event
                summary = tr("已清理 {0} 個資料夾、移除 {1} 個項目").format(folders, removed)
                self.hide_export_progress()
                self.status_text.set(summary)
                if errors:
                    messagebox.showerror(tr("清理完成（含錯誤）"), summary + "\n\n" + "\n".join(errors))
                else:
                    messagebox.showinfo(tr("清理完成"), summary)
                done = True
                rescan = removed > 0
            elif event[0] == "cleanup_error":
                self.hide_export_progress()
                self.status_text.set(tr("清理失敗"))
                messagebox.showerror(tr("清理失敗"), str(event[1]))
                done = True
            else:
                _, counts, errors, output_folder, cleanup = event
                self.hide_export_progress()
                summary = (tr("新增：{0}  更新：{1}  跳過：{2}  失敗：{3}").format(counts['created'], counts['updated'], counts['skipped'], counts['failed']))
                if cleanup:
                    summary += tr("  清理：{0} 個資料夾／{1} 個項目").format(cleanup[0], cleanup[1])
                self.status_text.set(summary)
                if errors:
                    messagebox.showerror(tr("Komga 匯出完成（含錯誤）"), summary + "\n\n" + "\n".join(errors))
                else:
                    messagebox.showinfo(tr("Komga 匯出完成"), summary)
                if self.open_after_export.get() and Path(output_folder).is_dir():
                    os.startfile(output_folder)
                done = True
                rescan = bool(cleanup and cleanup[1])
        if done:
            self.set_manga_busy(False)
            if rescan:
                self.load_folders()
        else:
            self.root.after(50, self.poll_events)


if __name__ == "__main__":
    try:
        configure_logging()
        logger.info("application_start executable=%s", sys.executable)
        root = tk.Tk()
        FileAggregatorApp(root)
        root.mainloop()
    except Exception as error:
        logger.exception("application_failed")
        try:
            messagebox.showerror(tr("Comic sorting 錯誤"), str(error))
        except Exception:
            pass
