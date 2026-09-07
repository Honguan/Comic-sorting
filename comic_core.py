"""Filesystem operations shared by the comic sorting UI and translation queue."""

import hashlib
import json
import os
import re
import shutil
import stat
import zipfile
from decimal import Decimal
from pathlib import Path

from ui_language import tr


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
WORK_FOLDER_NAMES = {"mask", "inpainted"}


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
        return tr("未翻譯"), []
    images = image_files(result)
    return (tr("可匯出") if images else tr("結果為空")), images


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
    return preview if len(names) <= 10 else tr("{0}\n…共 {1} 個章節").format(preview, len(names))


def _check_cleanup_path(path, root):
    info = path.lstat()
    if (stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
        raise ValueError(tr("{0}: 不清理符號連結").format(path))
    if not path.resolve().is_relative_to(root):
        raise ValueError(tr("不允許刪除系列目錄外或符號連結資料夾"))


def clear_work_folders(root):
    root = Path(root)
    root_resolved = root.resolve()
    targets = []
    removed = 0
    errors = []

    def report(error):
        errors.append(str(error))

    try:
        _check_cleanup_path(root, root_resolved)
    except (OSError, ValueError) as error:
        return 0, 0, [str(error)]
    for current, directory_names, _ in os.walk(root, onerror=report):
        for name in directory_names[:]:
            folder = Path(current) / name
            matched = name.casefold() in WORK_FOLDER_NAMES
            if matched:
                targets.append(folder)
            try:
                _check_cleanup_path(folder, root_resolved)
            except (OSError, ValueError) as error:
                if not matched:
                    report(error)
                directory_names.remove(name)
                continue
            if matched:
                directory_names.remove(name)
    for folder in targets:
        try:
            _check_cleanup_path(folder, root_resolved)
            children = list(folder.iterdir())
        except (OSError, ValueError) as error:
            errors.append(f"{folder}: {error}")
            continue
        for child in children:
            try:
                _check_cleanup_path(child, root_resolved)
                if child.is_dir():
                    # Validate the whole subtree before recursive deletion, including junctions.
                    def fail(error):
                        raise error

                    for current, directories, files in os.walk(child, onerror=fail):
                        for name in directories + files:
                            _check_cleanup_path(Path(current) / name, root_resolved)
                    shutil.rmtree(child)
                else:
                    child.unlink()
                removed += 1
            except (OSError, ValueError) as error:
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
                raise ValueError(tr("不允許刪除系列目錄外或符號連結資料夾"))
            if not folder.exists():
                continue
            if not folder.is_dir():
                raise ValueError(tr("來源不是資料夾"))
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
            raise ValueError(tr("CBZ 內容損壞"))
    if names != expected:
        raise ValueError(tr("CBZ 圖片數量、順序或名稱不符"))
    if any(Path(name).name != name or Path(name).suffix.casefold() not in IMAGE_EXTENSIONS
           for name in names):
        raise ValueError(tr("CBZ 含有子資料夾或非圖片檔案"))


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
    if status != tr("可匯出"):
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


