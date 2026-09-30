"""Filesystem operations shared by the comic sorting UI and translation queue."""

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import time
import zipfile
from collections import Counter
from decimal import Decimal
from pathlib import Path

from ui_language import tr


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
WORK_FOLDER_NAMES = {"mask", "inpainted"}
FOLDER_KINDS = {"chapter": "單一章節", "merged": "整合資料夾"}


def natural_sort_key(value):
    return tuple(int(part) if part.isdigit() else part.casefold()
                 for part in re.split(r"(\d+)", str(value)))


def chapter_number(name):
    match = re.search(r"\d+(?:\.\d+)?", name)
    return Decimal(match.group()) if match else None


def chapter_range(name):
    """Recognize the app's range naming, including legacy spacing/dash variants."""
    match = re.fullmatch(r"Chapter\s+(\d+(?:\.\d+)?)\s*[-–—~～]\s*(\d+(?:\.\d+)?)", name.strip(), re.I)
    return (Decimal(match[1]), Decimal(match[2])) if match else None


def folder_kind(name):
    return "merged" if chapter_range(name) is not None else "chapter"


def aggregate_output(selected):
    """Validate non-overlapping inputs before deriving the merged folder name."""
    parent = Path(selected[0][0]).parent.resolve()
    end = None
    for path, name, start in selected:
        span = chapter_range(name)
        last = span[1] if span else start
        if Path(path).parent.resolve() != parent:
            raise ValueError(tr("整合時請只選擇同一系列的章節"))
        if last < start or (end is not None and start <= end):
            raise ValueError(tr("章節範圍重疊或順序錯誤：{0}。請只選擇不重疊的來源章節。").format(name))
        end = last
    return parent / f"Chapter {selected[0][2]}-{end}"


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


def translations_complete(sources, translated, selected=None):
    """Allow extension changes only when the source basename is unambiguous."""
    counts = Counter(path.stem.casefold() for path in sources)
    names = {path.name.casefold() for path in translated}
    stems = {path.stem.casefold() for path in translated}
    return all(path.name.casefold() in names or
               (counts[path.stem.casefold()] == 1 and path.stem.casefold() in stems)
               for path in (sources if selected is None else selected))


def image_count(folder):
    folder = Path(folder)
    if not folder.is_dir():
        return 0
    with os.scandir(folder) as entries:
        return sum(entry.is_file(follow_symlinks=False)
                   and os.path.splitext(entry.name)[1].casefold() in IMAGE_EXTENSIONS
                   for entry in entries)


def translation_status(chapter_folder, *, source_images=None):
    result = Path(chapter_folder) / "result"
    if not result.is_dir():
        result = next((child for child in Path(chapter_folder).iterdir()
                       if child.name.casefold() == "result" and child.is_dir()), result)
    if not result.is_dir() or is_link_or_junction(result):
        return tr("未翻譯"), []
    images = image_files(result)
    if images and not translations_complete(image_files(chapter_folder) if source_images is None else source_images, images):
        return tr("部分翻譯"), images
    return (tr("可匯出") if images else tr("結果為空")), images


def updated_at(folder, images):
    latest = Path(folder).stat().st_mtime
    groups = {}
    for image in images:
        groups.setdefault(image.parent, set()).add(image.name)
    for parent, names in groups.items():
        # DirEntry reuses enumeration metadata on Windows, avoiding per-image I/O.
        with os.scandir(parent) as entries:
            for entry in entries:
                if entry.name in names:
                    latest = max(latest, entry.stat().st_mtime)
                    names.remove(entry.name)
        for name in names:
            latest = max(latest, (parent / name).stat().st_mtime)
    return latest


def folder_size(folder):
    total = 0
    pending = [os.fspath(folder)]
    while pending:
        try:
            current = pending.pop()
            if is_link_or_junction(current):
                continue
            entries = os.scandir(current)
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


def is_link_or_junction(path):
    info = Path(path).lstat()
    return bool(stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _check_cleanup_path(path, root):
    if is_link_or_junction(path):
        raise ValueError(tr("{0}: 不清理符號連結").format(path))
    if not path.resolve().is_relative_to(root):
        raise ValueError(tr("不允許刪除系列目錄外或符號連結資料夾"))


def delete_manga_folder(folder, root):
    """Recycle one folder strictly below the scanned root, rejecting linked paths."""
    root = Path(root).resolve()
    folder = Path(os.path.abspath(folder))
    if folder == root or not folder.is_relative_to(root):
        raise ValueError(tr("只能刪除漫畫路徑內的系列或章節，不能刪除漫畫根目錄"))
    current = folder
    while True:
        _check_cleanup_path(current, root)
        if current == root:
            break
        current = current.parent
    if not folder.is_dir():
        raise ValueError(tr("來源不是資料夾"))

    def fail(error):
        raise error

    for current, directories, files in os.walk(folder, onerror=fail):
        for name in directories + files:
            _check_cleanup_path(Path(current) / name, root)
    _check_cleanup_path(folder, root)
    from windows_recycle import recycle_folder
    recycle_folder(folder)


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


def remove_aggregated_folders(chapters, start_idx, end_idx, output, keep_last=True):
    output = Path(output)
    output_resolved = output.resolve()
    parent = output.parent.resolve()
    selected = [Path(item[0]) for item in chapters[start_idx:end_idx + 1]]
    removed = []
    errors = []
    for folder in selected[:-1] if keep_last else selected:
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


def _replace_file(source, target):
    # Windows readers or a pending deletion can briefly block atomic replacement.
    for attempt in range(6):
        try:
            os.replace(source, target)
            return
        except OSError as error:
            if getattr(error, "winerror", None) not in (5, 32, 33) or attempt == 5:
                raise
            time.sleep(0.02 * 2 ** attempt)


def create_cbz(images, output, progress=None):
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(dir=output.parent, prefix=f".{output.name}.", suffix=".tmp")
    os.close(descriptor)
    temporary = Path(name)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_STORED) as cbz:
            for index, image in enumerate(images, 1):
                cbz.write(image, image.name)
                if progress:
                    progress(index, len(images))
        validate_cbz(temporary, images)
        _replace_file(temporary, output)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def load_json(path, default, *, strict=False):
    try:
        with open(path, "r", encoding="utf-8-sig") as file:
            value = json.load(file)
            if strict and not isinstance(value, type(default)):
                raise ValueError("Unexpected JSON root type")
            return value if isinstance(value, type(default)) else default
    except FileNotFoundError:
        return default
    except (OSError, ValueError):
        if strict:
            raise ValueError(tr("無法讀取 JSON：{0}。請檢查權限與內容，或還原備份後再試；原檔未變更。").format(path)) from None
        return default


def save_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
        _replace_file(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def export_chapter(series_path, chapter_path, komga_root, state, skip_unchanged=True,
                   progress=None):
    chapter_path, komga_root = Path(chapter_path).resolve(), Path(komga_root).resolve()
    if chapter_path.is_relative_to(komga_root) or komga_root.is_relative_to(chapter_path):
        raise ValueError(tr("匯出與漫畫路徑不可互相包含"))
    status, images = translation_status(chapter_path)
    if status != tr("可匯出"):
        raise ValueError(status)
    output = output_path_for(komga_root, series_path, chapter_path)
    key = f"{Path(series_path).name}/{Path(chapter_path).name}"
    fingerprint = source_fingerprint(images)
    previous = state.get(key, {})
    if not isinstance(previous, dict):
        previous = {}
    source = images[0].parent.resolve()
    if previous.get("source") and Path(previous["source"]).resolve() != source:
        raise ValueError(tr("輸出已屬於其他來源，請改用不同的 Komga 路徑或系列名稱：{0}").format(output))
    if skip_unchanged and output.is_file() and previous.get("fingerprint") == fingerprint["fingerprint"]:
        info = output.stat()
        output_stat = [info.st_size, info.st_mtime_ns]
        if "outputStat" not in previous:
            try:
                validate_cbz(output, images)
                previous["outputStat"] = output_stat
            except (OSError, ValueError, zipfile.BadZipFile):
                pass  # Rebuild damaged archives from the unchanged source images.
        if previous.get("outputStat") == output_stat:
            return "skipped", output
    action = "updated" if output.exists() else "created"
    create_cbz(images, output, progress)
    info = output.stat()
    state[key] = {
        "source": str(source),
        "output": str(output),
        "outputStat": [info.st_size, info.st_mtime_ns],
        **fingerprint,
    }
    return action, output




def get_folders_with_numbers(base_path):
    folders = []
    base = Path(base_path)
    if is_link_or_junction(base):
        return folders

    def fail(error):
        raise error

    for current, directory_names, file_names in os.walk(base, onerror=fail):
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


def scan_folder_data(base):
    folders = get_folders_with_numbers(base)
    series_groups = {}
    chapter_updates = {}
    chapter_sizes = {}
    ready_chapters = set()
    details = {}
    page_counts = {}
    for folder_path, folder_name, number in folders:
        chapter = Path(folder_path)
        sources = image_files(folder_path)
        page_counts[chapter] = len(sources)
        status, translated = translation_status(folder_path, source_images=sources)
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
            images = sources
            detail = tr("{0} 張圖片｜未翻譯").format(len(images))
        chapter_updates[chapter] = updated_at(chapter, images)
        chapter_sizes[chapter] = folder_size(chapter)
        details[chapter] = detail
        series_groups.setdefault(chapter.parent, []).append(
            (str(chapter), folder_name, number))
    for chapters in series_groups.values():
        chapters.sort(key=lambda item: (item[2], natural_sort_key(item[1])))
    return (folders, series_groups, chapter_updates, chapter_sizes,
            ready_chapters, details, page_counts)


def aggregate_folders(chapters, start_idx, end_idx, progress=None):
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

