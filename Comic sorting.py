import hashlib
import json
import os
import queue
import re
import shutil
import sys
import threading
import zipfile
from datetime import datetime
from decimal import Decimal
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
WORK_FOLDER_NAMES = {"mask", "inpainted"}
SETTINGS_FILENAME = "comic-sorting.settings.json"


def resource_path(relative_path):
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    return base / relative_path


def natural_sort_key(value):
    return tuple(int(part) if part.isdigit() else part.casefold()
                 for part in re.split(r"(\d+)", str(value)))


def chapter_number(name):
    match = re.search(r"\d+(?:\.\d+)?", name)
    return Decimal(match.group()) if match else None


def image_files(folder):
    folder = Path(folder)
    if not folder.is_dir():
        return []
    with os.scandir(folder) as entries:
        images = [Path(entry.path) for entry in entries
                  if entry.is_file(follow_symlinks=False)
                  and Path(entry.name).suffix.casefold() in IMAGE_EXTENSIONS]
    return sorted(images,
                  key=lambda path: natural_sort_key(path.name))


def translation_status(chapter_folder):
    result = Path(chapter_folder) / "result"
    if not result.is_dir():
        result = next((child for child in Path(chapter_folder).iterdir()
                       if child.is_dir() and child.name.casefold() == "result"), result)
    if not result.is_dir():
        return "未翻譯", []
    images = image_files(result)
    return ("可匯出" if images else "結果為空"), images


def updated_at(folder, images):
    return max([Path(folder).stat().st_mtime, *(image.stat().st_mtime for image in images)])


def folder_size(folder):
    total = 0
    pending = [os.fspath(folder)]
    while pending:
        try:
            entries = os.scandir(pending.pop())
        except OSError:
            continue
        with entries:
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(entry.path)
                    elif entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
                except OSError:
                    # ponytail: unreadable files are omitted; report them if size auditing is needed.
                    pass
    return total


def format_size(size):
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024


def summarize_names(names):
    preview = ", ".join(names[:10])
    return preview if len(names) <= 10 else f"{preview}\n…共 {len(names)} 個章節"


def clear_work_folders(root):
    targets = []
    for current, directory_names, _ in os.walk(root):
        matched = [name for name in directory_names
                   if name.casefold() in WORK_FOLDER_NAMES]
        targets.extend(Path(current) / name for name in matched)
        directory_names[:] = [name for name in directory_names if name not in matched]
    removed = 0
    errors = []
    for folder in targets:
        if folder.is_symlink():
            errors.append(f"{folder}: 不清理符號連結")
            continue
        try:
            children = list(folder.iterdir())
        except OSError as error:
            errors.append(f"{folder}: {error}")
            continue
        for child in children:
            try:
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink()
                removed += 1
            except OSError as error:
                errors.append(f"{child}: {error}")
    return len(targets), removed, errors


def remove_aggregated_folders(chapters, start_idx, end_idx, output):
    output = Path(output)
    output_resolved = output.resolve()
    parent = output.parent.resolve()
    selected = [Path(item[0]) for item in chapters[start_idx:end_idx + 1]]
    removed = []
    errors = []
    for folder in selected[:-1]:
        try:
            resolved = folder.resolve()
            if resolved == output_resolved:
                continue
            if resolved.parent != parent or folder.is_symlink():
                raise ValueError("不允許刪除系列目錄外或符號連結資料夾")
            if not folder.exists():
                continue
            if not folder.is_dir():
                raise ValueError("來源不是資料夾")
            shutil.rmtree(folder)
            removed.append(folder.name)
        except (OSError, ValueError) as error:
            errors.append(f"{folder}: {error}")
    return removed, errors


def source_fingerprint(images):
    details = []
    latest = 0
    for image in images:
        stat = image.stat()
        latest = max(latest, stat.st_mtime_ns)
        details.append((image.name, stat.st_size, stat.st_mtime_ns))
    encoded = json.dumps(details, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return {
        "count": len(images),
        "latestWriteTime": latest,
        "fingerprint": hashlib.sha256(encoded).hexdigest(),
    }


def output_path_for(komga_root, series_path, chapter_path):
    return Path(komga_root) / Path(series_path).name / f"{Path(chapter_path).name}.cbz"


def validate_cbz(archive, expected_images):
    expected = [path.name for path in expected_images]
    with zipfile.ZipFile(archive, "r") as cbz:
        names = cbz.namelist()
        if cbz.testzip() is not None:
            raise ValueError("CBZ 內容損壞")
    if names != expected:
        raise ValueError("CBZ 圖片數量、順序或名稱不符")
    if any(Path(name).name != name or Path(name).suffix.casefold() not in IMAGE_EXTENSIONS
           for name in names):
        raise ValueError("CBZ 含有子資料夾或非圖片檔案")


def create_cbz(images, output, progress=None):
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    try:
        temporary.unlink(missing_ok=True)
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_STORED) as cbz:
            for index, image in enumerate(images, 1):
                cbz.write(image, image.name)
                if progress:
                    progress(index, len(images))
        validate_cbz(temporary, images)
        os.replace(temporary, output)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as file:
            value = json.load(file)
            return value if isinstance(value, type(default)) else default
    except (OSError, ValueError):
        return default


def save_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(temporary, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def export_chapter(series_path, chapter_path, komga_root, state, skip_unchanged=True,
                   progress=None):
    status, images = translation_status(chapter_path)
    if status != "可匯出":
        raise ValueError(status)
    output = output_path_for(komga_root, series_path, chapter_path)
    key = f"{Path(series_path).name}/{Path(chapter_path).name}"
    fingerprint = source_fingerprint(images)
    previous = state.get(key, {})
    if skip_unchanged and output.is_file() and previous.get("fingerprint") == fingerprint["fingerprint"]:
        return "skipped", output
    action = "updated" if output.exists() else "created"
    create_cbz(images, output, progress)
    state[key] = {
        "source": str(Path(chapter_path) / "result"),
        "output": str(output),
        **fingerprint,
    }
    return action, output


def settings_path():
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
    return base / SETTINGS_FILENAME


class FileAggregatorApp:
    def __init__(self, root):
        self.root = root
        icon = resource_path("assets/comic-sorting.ico")
        if icon.is_file():
            self.root.iconbitmap(default=icon)
        self.root.title("漫畫整合工具")
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
        settings = load_json(settings_path(), {})
        self.base_path = tk.StringVar(value=settings.get("manga_path", ""))
        self.komga_path = tk.StringVar(value=settings.get("komga_path", ""))
        self.skip_unchanged = tk.BooleanVar(value=True)
        self.open_after_export = tk.BooleanVar(value=False)
        self.remove_sources_after_aggregate = tk.BooleanVar(
            value=settings.get("remove_sources_after_aggregate") is True)
        self.status_text = tk.StringVar(value="就緒")

        manga = ttk.LabelFrame(root, text="漫畫整合", padding=8)
        manga.pack(fill="both", expand=True, padx=10, pady=6)
        path_row = ttk.Frame(manga)
        path_row.pack(fill="x")
        ttk.Label(path_row, text="漫畫路徑：").pack(side="left")
        self.path_entry = ttk.Entry(path_row, textvariable=self.base_path)
        self.path_entry.pack(side="left", fill="x", expand=True)
        self.browse_button = ttk.Button(path_row, text="瀏覽", command=self.browse)
        self.browse_button.pack(side="left", padx=(6, 0))
        self.rescan_button = ttk.Button(path_row, text="重新掃描", command=self.load_folders)
        self.rescan_button.pack(side="left", padx=(6, 0))

        list_frame = ttk.Frame(manga)
        list_frame.pack(fill="both", expand=True, pady=8)
        self.folder_tree = ttk.Treeview(
            list_frame, columns=("status", "size", "updated"), show="tree headings",
            selectmode="browse", height=12)
        self.folder_tree.heading("#0", text="序號｜系列 / 章節")
        self.folder_tree.heading("status", text="狀態")
        self.folder_tree.heading("size", text="資料夾大小")
        self.folder_tree.heading("updated", text="更新時間 ↓")
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

        scan_row = ttk.Frame(manga)
        scan_row.pack(fill="x")
        self.scan_progress = ttk.Progressbar(scan_row, mode="indeterminate")
        self.scan_progress.pack(side="left", fill="x", expand=True)
        self.scan_status_text = tk.StringVar(value="尚未掃描")
        self.scan_status_label = ttk.Label(scan_row, textvariable=self.scan_status_text)
        self.scan_status_label.pack(side="left", padx=(8, 0))
        self.scan_progress.pack_forget()

        range_row = ttk.Frame(manga)
        range_row.pack()
        ttk.Label(range_row, text="起始編號：").pack(side="left")
        self.start_entry = ttk.Entry(range_row, width=6)
        self.start_entry.pack(side="left")
        ttk.Label(range_row, text="結束編號：").pack(side="left", padx=(10, 0))
        self.end_entry = ttk.Entry(range_row, width=6)
        self.end_entry.pack(side="left")
        self.aggregate_button = ttk.Button(range_row, text="確認整合", command=self.confirm_aggregate)
        self.aggregate_button.pack(side="left", padx=10)
        self.remove_sources_checkbox = ttk.Checkbutton(
            range_row, text="整合後清除來源（保留最後一個）",
            variable=self.remove_sources_after_aggregate, command=self.save_settings)
        self.remove_sources_checkbox.pack(side="left")

        export = ttk.LabelFrame(root, text="Komga 匯出", padding=8)
        export.pack(fill="x", padx=10, pady=6)
        komga_row = ttk.Frame(export)
        komga_row.pack(fill="x")
        ttk.Label(komga_row, text="Komga 輸出路徑：").pack(side="left")
        self.komga_entry = ttk.Entry(komga_row, textvariable=self.komga_path)
        self.komga_entry.pack(side="left", fill="x", expand=True)
        self.browse_komga_button = ttk.Button(
            komga_row, text="瀏覽", command=self.browse_komga)
        self.browse_komga_button.pack(side="left", padx=(6, 0))
        options = ttk.Frame(export)
        options.pack(fill="x", pady=6)
        self.skip_checkbox = ttk.Checkbutton(
            options, text="已存在且來源未變更時跳過", variable=self.skip_unchanged)
        self.skip_checkbox.pack(side="left")
        self.open_checkbox = ttk.Checkbutton(
            options, text="匯出完成後開啟輸出資料夾", variable=self.open_after_export)
        self.open_checkbox.pack(side="left", padx=12)
        buttons = ttk.Frame(export)
        buttons.pack(fill="x")
        self.export_selected_button = ttk.Button(buttons, text="匯出選取項目", command=self.export_selected)
        self.export_selected_button.pack(side="left")
        self.export_all_button = ttk.Button(buttons, text="匯出所有已翻譯項目", command=self.export_all)
        self.export_all_button.pack(side="left", padx=6)
        self.cleanup_button = ttk.Button(
            buttons, text="清理所有 mask / inpainted", command=self.confirm_cleanup)
        self.cleanup_button.pack(side="left")
        self.progress = ttk.Progressbar(export, mode="determinate")
        self.progress.pack(fill="x", pady=(8, 2))
        self.status_label = ttk.Label(export, textvariable=self.status_text)
        self.status_label.pack(anchor="w")
        self.progress.pack_forget()

        self.root.protocol("WM_DELETE_WINDOW", self.close)
        if self.base_path.get() and Path(self.base_path.get()).is_dir():
            self.load_folders()

    def save_settings(self):
        try:
            save_json(settings_path(), {
                "manga_path": self.base_path.get(),
                "komga_path": self.komga_path.get(),
                "remove_sources_after_aggregate": self.remove_sources_after_aggregate.get(),
            })
            return True
        except OSError as error:
            messagebox.showerror(
                "設定保存失敗",
                f"無法寫入 EXE 同目錄的設定檔：\n{settings_path()}\n\n{error}")
            return False

    def close(self):
        self.save_settings()
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
        base = Path(self.base_path.get())
        if not base.is_dir():
            messagebox.showwarning("警告", "請選擇有效的漫畫路徑")
            return
        if self.manga_busy:
            return
        self.set_manga_busy(True)
        self.show_scan_progress("indeterminate")
        self.scan_progress.start(12)
        self.scan_status_text.set("正在掃描…")
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
            self.scan_events.put(("done", base, self.scan_folder_data(base)))
        except Exception as error:
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
            self.scan_status_text.set("掃描失敗")
            messagebox.showerror("掃描失敗", str(event[1]))
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
            if status == "可匯出":
                detail = f"{len(translated)} 張翻譯圖片｜可匯出"
                images = translated
                ready_chapters.add(chapter)
            elif status == "結果為空":
                detail = "結果資料夾為空"
                images = []
            else:
                images = image_files(folder_path)
                detail = f"{len(images)} 張圖片｜未翻譯"
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
            modified = max(self.chapter_updates[Path(item[0])]
                           for item in self.series_groups[series])
            relative_series = series.relative_to(base)
            series_name = series.name if relative_series == Path(".") else str(relative_series)
            parent = self.folder_tree.insert(
                "", "end", text=f"{series_index}. {series_name}", open=False,
                values=(f"{len(self.series_groups[series])} 個章節",
                        format_size(sum(self.chapter_sizes[Path(item[0])]
                                        for item in self.series_groups[series])),
                        datetime.fromtimestamp(modified).strftime("%Y-%m-%d %H:%M:%S")))
            self.tree_items[parent] = ("series", series)
            for chapter_index, (folder_path, folder_name, _) in enumerate(
                    self.series_groups[series], 1):
                chapter = Path(folder_path)
                item = self.folder_tree.insert(
                    parent, "end", text=f"{chapter_index}. {folder_name}",
                    values=(details[chapter], format_size(self.chapter_sizes[chapter]),
                            datetime.fromtimestamp(
                        self.chapter_updates[chapter]).strftime("%Y-%m-%d %H:%M:%S")))
                self.tree_items[item] = ("chapter", chapter)
        self.scan_status_text.set(
            f"{len(self.series_groups)} 個系列，{len(self.folders)} 個章節")

    @staticmethod
    def get_folders_with_numbers(base_path):
        folders = []
        base = Path(base_path)
        for current, directory_names, file_names in os.walk(base):
            folder = Path(current)
            has_result = any(name.casefold() == "result" for name in directory_names)
            directory_names[:] = [name for name in directory_names
                                  if name.casefold() not in {"result", "inpainted"}]
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

    def on_tree_select(self, _event=None):
        selected = self.selected_tree_item()
        if not selected:
            return
        kind, path, chapters = selected
        start = 1 if kind == "series" else next(
            index for index, item in enumerate(chapters, 1) if Path(item[0]) == path)
        for entry, value in ((self.start_entry, start), (self.end_entry, len(chapters))):
            entry.delete(0, tk.END)
            entry.insert(0, str(value))

    def confirm_aggregate(self):
        selected = self.selected_tree_item()
        if not selected:
            messagebox.showwarning("警告", "請先選擇父系列或章節")
            return
        _, _, chapters = selected
        start, end = self.start_entry.get(), self.end_entry.get()
        if not start.isdigit() or not end.isdigit():
            messagebox.showwarning("警告", "請輸入有效的起始和結束編號")
            return
        start_idx, end_idx = int(start) - 1, int(end) - 1
        if start_idx < 0 or end_idx >= len(chapters) or start_idx > end_idx:
            messagebox.showwarning("警告", "請確保編號範圍有效")
            return
        names = [chapters[index][1] for index in range(start_idx, end_idx + 1)]
        remove_sources = self.remove_sources_after_aggregate.get()
        confirmation = f"您確定要整合以下資料夾嗎？\n\n{summarize_names(names)}"
        if remove_sources:
            confirmation += (
                "\n\n注意：整合成功後，將永久刪除本次範圍內除最後一個"
                f"之外的來源資料夾與內容。\n保留：{names[-1]}")
        if messagebox.askyesno("確認整合", confirmation):
            self.set_manga_busy(True)
            self.show_scan_progress("determinate")
            self.scan_progress.configure(maximum=1)
            self.scan_status_text.set("準備整合…")
            threading.Thread(
                target=self.aggregate_worker,
                args=(chapters, start_idx, end_idx, remove_sources), daemon=True).start()
            self.root.after(50, self.poll_aggregate_events)

    def aggregate_folders(self, chapters, start_idx, end_idx, progress=None):
        selected = chapters[start_idx:end_idx + 1]
        output_name = f"Chapter {selected[0][2]}-{selected[-1][2]}"
        output = Path(selected[0][0]).parent / output_name
        sources = [source for folder_path, _, _ in selected if Path(folder_path) != output
                   for source in image_files(folder_path)]
        if not sources:
            raise ValueError("選取範圍沒有可整合的圖片")
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
            raise ValueError(f"輸出路徑不是資料夾：{output}")
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
            output = self.aggregate_folders(
                chapters, start_idx, end_idx,
                lambda current, total: self.aggregate_events.put(
                    ("progress", current, total)))
            cleanup = ([], [])
            if remove_sources:
                cleanup = remove_aggregated_folders(
                    chapters, start_idx, end_idx, output)
            self.aggregate_events.put(("done", output, cleanup))
        except Exception as error:
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
                self.scan_status_text.set(f"正在整合… {current} / {total}")
            else:
                done = event
        if done is None:
            self.root.after(50, self.poll_aggregate_events)
            return
        self.hide_scan_progress()
        self.set_manga_busy(False)
        if done[0] == "error":
            self.scan_status_text.set("整合失敗")
            messagebox.showerror("整合失敗", str(done[1]))
            return
        output = done[1]
        removed, cleanup_errors = done[2]
        summary = f"檔案已重命名並複製到 {output.name}"
        if removed:
            summary += f"\n已清除 {len(removed)} 個來源資料夾"
        if cleanup_errors:
            messagebox.showwarning(
                "整合完成（清理含錯誤）", summary + "\n\n" + "\n".join(cleanup_errors))
        else:
            messagebox.showinfo("完成", summary)
        self.load_folders()

    def export_selected(self):
        selected = self.selected_tree_item()
        if not selected:
            messagebox.showwarning("警告", "請先選擇父系列或章節")
            return
        kind, path, chapters = selected
        candidates = chapters if kind == "series" else [(str(path), path.name, chapter_number(path.name))]
        ready = [folder_path for folder_path, _, _ in candidates
                 if Path(folder_path) in self.ready_chapters]
        if not ready:
            messagebox.showinfo("Komga 匯出", "選取項目沒有可匯出的翻譯結果")
            return
        self.start_export(ready)

    def export_all(self):
        chapters = [path for path, _, _ in self.folders
                    if Path(path) in self.ready_chapters]
        if not chapters:
            messagebox.showinfo("Komga 匯出", "沒有可匯出的翻譯結果")
            return
        self.start_export(chapters)

    def start_export(self, chapters):
        source_root = Path(self.base_path.get())
        output_root = Path(self.komga_path.get())
        if not source_root.is_dir() or not self.komga_path.get().strip():
            messagebox.showwarning("警告", "請確認漫畫與 Komga 路徑")
            return
        if output_root.resolve().is_relative_to(source_root.resolve()):
            messagebox.showwarning("警告", "Komga 輸出路徑不能位於漫畫來源路徑內")
            return
        self.save_settings()
        self.set_manga_busy(True)
        self.show_export_progress("determinate")
        self.progress.configure(maximum=1)
        self.status_text.set("準備匯出…")
        options = (self.komga_path.get(), self.skip_unchanged.get())
        threading.Thread(target=self.export_worker, args=(chapters, *options), daemon=True).start()
        self.root.after(50, self.poll_events)

    def export_worker(self, chapters, komga_path, skip_unchanged):
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
                self.events.put(("chapter", action, name))
            except Exception as error:
                counts["failed"] += 1
                errors.append(f"{name}: {error}")
                self.events.put(("chapter", "failed", name))
        state_saved = True
        try:
            save_json(state_file, state)
        except Exception as error:
            state_saved = False
            counts["failed"] += 1
            errors.append(f"狀態檔: {error}")
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
            errors.extend(f"清理失敗：{error}" for error in cleanup_errors)
        output_folder = output_folders.pop() if len(output_folders) == 1 else root
        self.events.put(("done", counts, errors, str(output_folder), cleanup))

    def confirm_cleanup(self):
        root = Path(self.base_path.get())
        if not root.is_dir():
            messagebox.showwarning("警告", "請選擇有效的漫畫路徑")
            return
        if not messagebox.askyesno(
                "確認清理",
                "將清空目前漫畫路徑下所有 mask 與 inpainted 資料夾內容。\n"
                "資料夾本身會保留，此操作無法復原。確定繼續嗎？"):
            return
        self.set_manga_busy(True)
        self.show_export_progress("indeterminate")
        self.progress.start(12)
        self.status_text.set("正在清理 mask / inpainted…")
        threading.Thread(target=self.cleanup_worker, args=(root,), daemon=True).start()
        self.root.after(50, self.poll_events)

    def cleanup_worker(self, root):
        try:
            self.events.put(("cleanup_done", clear_work_folders(root)))
        except Exception as error:
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
                self.status_text.set(f"正在處理：{chapter} — {current} / {total}")
            elif event[0] == "chapter":
                _, action, chapter = event
                action_label = {"created": "新增", "updated": "更新",
                                "skipped": "跳過", "failed": "失敗"}[action]
                self.status_text.set(f"[{action_label}] {chapter}")
            elif event[0] == "cleanup_done":
                _, (folders, removed, errors) = event
                summary = f"已清理 {folders} 個資料夾、移除 {removed} 個項目"
                self.hide_export_progress()
                self.status_text.set(summary)
                if errors:
                    messagebox.showerror("清理完成（含錯誤）", summary + "\n\n" + "\n".join(errors))
                else:
                    messagebox.showinfo("清理完成", summary)
                done = True
                rescan = removed > 0
            elif event[0] == "cleanup_error":
                self.hide_export_progress()
                self.status_text.set("清理失敗")
                messagebox.showerror("清理失敗", str(event[1]))
                done = True
            else:
                _, counts, errors, output_folder, cleanup = event
                self.hide_export_progress()
                summary = (f"新增：{counts['created']}  更新：{counts['updated']}  "
                           f"跳過：{counts['skipped']}  失敗：{counts['failed']}")
                if cleanup:
                    summary += f"  清理：{cleanup[0]} 個資料夾／{cleanup[1]} 個項目"
                self.status_text.set(summary)
                if errors:
                    messagebox.showerror("Komga 匯出完成（含錯誤）", summary + "\n\n" + "\n".join(errors))
                else:
                    messagebox.showinfo("Komga 匯出完成", summary)
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
        root = tk.Tk()
        FileAggregatorApp(root)
        root.mainloop()
    except Exception as error:
        try:
            messagebox.showerror("Comic sorting 錯誤", str(error))
        except Exception:
            pass
