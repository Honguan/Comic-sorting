import hashlib
import json
import os
import queue
import re
import shutil
import threading
import zipfile
from decimal import Decimal
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
DEFAULT_KOMGA_PATH = r"D:\Komga"


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
    return sorted((path for path in folder.iterdir()
                   if path.is_file() and path.suffix.casefold() in IMAGE_EXTENSIONS),
                  key=lambda path: natural_sort_key(path.name))


def translation_status(chapter_folder):
    result = Path(chapter_folder) / "result"
    if not result.is_dir():
        return "未翻譯", []
    images = image_files(result)
    return ("可匯出" if images else "結果為空"), images


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
            return json.load(file)
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
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "ComicSorting" / "settings.json"


class FileAggregatorApp:
    def __init__(self, root):
        self.root = root
        self.root.title("漫畫整合工具")
        self.folders = []
        self.events = queue.Queue()
        settings = load_json(settings_path(), {})
        self.base_path = tk.StringVar(value=settings.get("manga_path", ""))
        self.komga_path = tk.StringVar(value=settings.get("komga_path", DEFAULT_KOMGA_PATH))
        self.skip_unchanged = tk.BooleanVar(value=True)
        self.open_after_export = tk.BooleanVar(value=False)
        self.status_text = tk.StringVar(value="就緒")

        manga = ttk.LabelFrame(root, text="漫畫整合", padding=8)
        manga.pack(fill="both", expand=True, padx=10, pady=6)
        path_row = ttk.Frame(manga)
        path_row.pack(fill="x")
        ttk.Label(path_row, text="漫畫路徑：").pack(side="left")
        ttk.Entry(path_row, textvariable=self.base_path).pack(side="left", fill="x", expand=True)
        ttk.Button(path_row, text="瀏覽", command=self.browse).pack(side="left", padx=(6, 0))
        ttk.Button(path_row, text="重新掃描", command=self.load_folders).pack(side="left", padx=(6, 0))

        list_frame = ttk.Frame(manga)
        list_frame.pack(fill="both", expand=True, pady=8)
        self.folder_listbox = tk.Listbox(list_frame, selectmode=tk.SINGLE, width=82, height=12)
        scrollbar = ttk.Scrollbar(list_frame, command=self.folder_listbox.yview)
        self.folder_listbox.configure(yscrollcommand=scrollbar.set)
        self.folder_listbox.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        range_row = ttk.Frame(manga)
        range_row.pack()
        ttk.Label(range_row, text="起始編號：").pack(side="left")
        self.start_entry = ttk.Entry(range_row, width=6)
        self.start_entry.pack(side="left")
        ttk.Label(range_row, text="結束編號：").pack(side="left", padx=(10, 0))
        self.end_entry = ttk.Entry(range_row, width=6)
        self.end_entry.pack(side="left")
        ttk.Button(range_row, text="確認整合", command=self.confirm_aggregate).pack(side="left", padx=10)

        export = ttk.LabelFrame(root, text="Komga 匯出", padding=8)
        export.pack(fill="x", padx=10, pady=6)
        komga_row = ttk.Frame(export)
        komga_row.pack(fill="x")
        ttk.Label(komga_row, text="Komga 輸出路徑：").pack(side="left")
        ttk.Entry(komga_row, textvariable=self.komga_path).pack(side="left", fill="x", expand=True)
        ttk.Button(komga_row, text="瀏覽", command=self.browse_komga).pack(side="left", padx=(6, 0))
        options = ttk.Frame(export)
        options.pack(fill="x", pady=6)
        ttk.Checkbutton(options, text="已存在且來源未變更時跳過", variable=self.skip_unchanged).pack(side="left")
        ttk.Checkbutton(options, text="匯出完成後開啟輸出資料夾", variable=self.open_after_export).pack(side="left", padx=12)
        buttons = ttk.Frame(export)
        buttons.pack(fill="x")
        self.export_selected_button = ttk.Button(buttons, text="匯出選取項目", command=self.export_selected)
        self.export_selected_button.pack(side="left")
        self.export_all_button = ttk.Button(buttons, text="匯出所有已翻譯項目", command=self.export_all)
        self.export_all_button.pack(side="left", padx=6)
        self.progress = ttk.Progressbar(export, mode="determinate")
        self.progress.pack(fill="x", pady=(8, 2))
        ttk.Label(export, textvariable=self.status_text).pack(anchor="w")

        self.root.protocol("WM_DELETE_WINDOW", self.close)
        if self.base_path.get() and Path(self.base_path.get()).is_dir():
            self.load_folders()

    def save_settings(self):
        save_json(settings_path(), {"manga_path": self.base_path.get(), "komga_path": self.komga_path.get()})

    def close(self):
        try:
            self.save_settings()
        except OSError:
            pass
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
        self.folders = self.get_folders_with_numbers(base)
        self.folder_listbox.delete(0, tk.END)
        for index, (folder_path, folder_name, _) in enumerate(self.folders, 1):
            status, translated = translation_status(folder_path)
            if status == "可匯出":
                detail = f"{len(translated)} translated | Ready"
            elif status == "結果為空":
                detail = "Result empty"
            else:
                detail = f"{len(image_files(folder_path))} images | Not translated"
            self.folder_listbox.insert(tk.END, f"{index} | {folder_name} | {detail}")

    @staticmethod
    def get_folders_with_numbers(base_path):
        folders = []
        for folder in Path(base_path).iterdir():
            number = chapter_number(folder.name)
            if folder.is_dir() and number is not None:
                folders.append((str(folder), folder.name, number))
        return sorted(folders, key=lambda item: (item[2], natural_sort_key(item[1])))

    def confirm_aggregate(self):
        start, end = self.start_entry.get(), self.end_entry.get()
        if not start.isdigit() or not end.isdigit():
            messagebox.showwarning("警告", "請輸入有效的起始和結束編號")
            return
        start_idx, end_idx = int(start) - 1, int(end) - 1
        if start_idx < 0 or end_idx >= len(self.folders) or start_idx > end_idx:
            messagebox.showwarning("警告", "請確保編號範圍有效")
            return
        names = [self.folders[index][1] for index in range(start_idx, end_idx + 1)]
        if messagebox.askyesno("確認整合", f"您確定要整合以下資料夾嗎？\n\n{', '.join(names)}"):
            try:
                output = self.aggregate_folders(start_idx, end_idx)
                messagebox.showinfo("完成", f"檔案已重命名並複製到 {output.name}")
                self.load_folders()
            except Exception as error:
                messagebox.showerror("整合失敗", str(error))

    def aggregate_folders(self, start_idx, end_idx):
        selected = self.folders[start_idx:end_idx + 1]
        output_name = f"Chapter {selected[0][2]}-{selected[-1][2]}"
        output = Path(self.base_path.get()) / output_name
        output.mkdir(exist_ok=True)
        index = 1
        for folder_path, _, _ in selected:
            for source in image_files(folder_path):
                shutil.copy2(source, output / f"{index}{source.suffix}")
                index += 1
        return output

    def export_selected(self):
        selection = self.folder_listbox.curselection()
        if not selection:
            messagebox.showwarning("警告", "請先在列表選取一個章節")
            return
        self.start_export([self.folders[selection[0]][0]])

    def export_all(self):
        chapters = [path for path, _, _ in self.folders if translation_status(path)[0] == "可匯出"]
        if not chapters:
            messagebox.showinfo("Komga 匯出", "沒有可匯出的翻譯結果")
            return
        self.start_export(chapters)

    def start_export(self, chapters):
        if not Path(self.base_path.get()).is_dir() or not self.komga_path.get().strip():
            messagebox.showwarning("警告", "請確認漫畫與 Komga 路徑")
            return
        self.save_settings()
        self.export_selected_button.configure(state="disabled")
        self.export_all_button.configure(state="disabled")
        self.progress.configure(value=0, maximum=1)
        self.status_text.set("準備匯出…")
        options = (self.base_path.get(), self.komga_path.get(), self.skip_unchanged.get())
        threading.Thread(target=self.export_worker, args=(chapters, *options), daemon=True).start()
        self.root.after(50, self.poll_events)

    def export_worker(self, chapters, series_path, komga_path, skip_unchanged):
        root = Path(komga_path)
        state_file = root / ".comic-sorting-state.json"
        state = load_json(state_file, {})
        counts = {"created": 0, "updated": 0, "skipped": 0, "failed": 0}
        errors = []
        for chapter in chapters:
            name = Path(chapter).name
            try:
                action, _ = export_chapter(
                    series_path, chapter, root, state, skip_unchanged,
                    lambda current, total, chapter_name=name: self.events.put(
                        ("progress", chapter_name, current, total)))
                counts[action] += 1
                self.events.put(("chapter", action, name))
            except Exception as error:
                counts["failed"] += 1
                errors.append(f"{name}: {error}")
                self.events.put(("chapter", "failed", name))
        try:
            save_json(state_file, state)
        except Exception as error:
            counts["failed"] += 1
            errors.append(f"狀態檔: {error}")
        self.events.put(("done", counts, errors, str(root / Path(series_path).name)))

    def poll_events(self):
        done = False
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
                self.status_text.set(f"[{action.upper()}] {chapter}")
            else:
                _, counts, errors, output_folder = event
                summary = (f"Created: {counts['created']}  Updated: {counts['updated']}  "
                           f"Skipped: {counts['skipped']}  Failed: {counts['failed']}")
                self.status_text.set(summary)
                if errors:
                    messagebox.showerror("Komga 匯出完成（含錯誤）", summary + "\n\n" + "\n".join(errors))
                else:
                    messagebox.showinfo("Komga 匯出完成", summary)
                if self.open_after_export.get() and Path(output_folder).is_dir():
                    os.startfile(output_folder)
                done = True
        if done:
            self.export_selected_button.configure(state="normal")
            self.export_all_button.configure(state="normal")
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
