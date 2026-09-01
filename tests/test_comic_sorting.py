import importlib.util
import os
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).parents[1] / "Comic sorting.py"
SPEC = importlib.util.spec_from_file_location("comic_sorting", MODULE_PATH)
comic = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(comic)


def fixture(root):
    series = Path(root) / "漫畫＂系列"
    chapter = series / "Chapter 1"
    result = chapter / "result"
    result.mkdir(parents=True)
    for name in ("10.png", "2.jpg", "1.webp"):
        (result / name).write_bytes(name.encode())
    (result / "data.json").write_text("{}", encoding="utf-8")
    (result / "nested").mkdir()
    (result / "nested" / "3.png").write_bytes(b"nested")
    return series, chapter, result, Path(root) / "Komga"


class ComicSortingTests(unittest.TestCase):
    def test_chapter_parser(self):
        self.assertEqual(comic.chapter_number("Chapter 49.2"), comic.Decimal("49.2"))
        self.assertIsNone(comic.chapter_number("extras"))

    def test_chapter_sort(self):
        names = ["Chapter 49.2", "Chapter 13", "Chapter 12.5", "Chapter 12"]
        self.assertEqual(sorted(names, key=comic.chapter_number),
                         ["Chapter 12", "Chapter 12.5", "Chapter 13", "Chapter 49.2"])

    def test_natural_sort(self):
        names = ["10.png", "2.png", "page10.jpg", "page2.jpg", "001.png"]
        self.assertEqual(sorted(names, key=comic.natural_sort_key),
                         ["001.png", "2.png", "10.png", "page2.jpg", "page10.jpg"])

    def test_unicode_series_path(self):
        name = '＂Ou no Saien＂ no Kishi to, ＂Yasai＂ no Ojou-sama'
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / name
            path.mkdir()
            self.assertEqual(path.name, name)

    def test_image_filter(self):
        with tempfile.TemporaryDirectory() as temp:
            _, _, result, _ = fixture(temp)
            self.assertEqual([path.name for path in comic.image_files(result)],
                             ["1.webp", "2.jpg", "10.png"])

    def test_output_path_mapping(self):
        series = Path("D:/Mangas") / '＂series＂'
        self.assertEqual(comic.output_path_for("D:/Komga", series, series / "Chapter 1"),
                         Path("D:/Komga/＂series＂/Chapter 1.cbz"))

    def test_resource_path_uses_pyinstaller_bundle(self):
        with tempfile.TemporaryDirectory() as temp:
            with mock.patch.object(comic.sys, "_MEIPASS", temp, create=True):
                self.assertEqual(comic.resource_path("assets/icon.ico"),
                                 Path(temp) / "assets" / "icon.ico")

    def test_settings_path_is_bound_to_executable_folder(self):
        with tempfile.TemporaryDirectory() as temp:
            executable = Path(temp) / "Comic sorting.exe"
            with mock.patch.object(comic.sys, "frozen", True, create=True), \
                    mock.patch.object(comic.sys, "executable", str(executable)):
                path = comic.settings_path()
                self.assertEqual(path, executable.parent / "comic-sorting.settings.json")
                self.assertEqual(comic.load_json(path, {}), {})
                comic.save_json(path, {"manga_path": "first", "komga_path": "output"})
                comic.save_json(path, {"manga_path": "rebound", "komga_path": "new-output"})
                self.assertEqual(comic.load_json(path, {}),
                                 {"manga_path": "rebound", "komga_path": "new-output"})

    def test_load_json_rejects_wrong_root_type(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "settings.json"
            path.write_text("[]", encoding="utf-8")
            self.assertEqual(comic.load_json(path, {}), {})

    def test_settings_save_failure_is_reported(self):
        app = comic.FileAggregatorApp.__new__(comic.FileAggregatorApp)
        value = lambda text: type("Value", (), {"get": lambda self: text})()
        app.base_path = value("manga")
        app.komga_path = value("komga")
        with mock.patch.object(comic, "save_json", side_effect=OSError("denied")), \
                mock.patch.object(comic.messagebox, "showerror") as error:
            self.assertFalse(app.save_settings())
        self.assertIn("denied", error.call_args.args[1])

    def test_high_level_discovery(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = root / "Killer Shark in Another World" / "Chapter 10-42"
            second = root / "29-Years-Old Bachelor" / "Chapter 1"
            (first / "result").mkdir(parents=True)
            (second / "result").mkdir(parents=True)
            (first / "result" / "1.webp").write_bytes(b"first")
            (second / "result" / "1.webp").write_bytes(b"second")

            folders = comic.FileAggregatorApp.get_folders_with_numbers(root)

            self.assertEqual([Path(item[0]) for item in folders], [second, first])

    def test_high_level_export_uses_actual_series(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            chapter = root / "Mangas" / "Killer Shark" / "Chapter 10-42"
            result = chapter / "result"
            result.mkdir(parents=True)
            (result / "1.webp").write_bytes(b"image")
            (chapter / "mask").mkdir()
            (chapter / "mask" / "1.png").write_bytes(b"mask")
            (chapter / "inpainted").mkdir()
            (chapter / "inpainted" / "1.png").write_bytes(b"inpainted")
            output_root = root / "Komga"
            app = comic.FileAggregatorApp.__new__(comic.FileAggregatorApp)
            app.events = comic.queue.Queue()

            app.export_worker([str(chapter)], str(output_root), True)

            self.assertTrue((output_root / "Killer Shark" / "Chapter 10-42.cbz").is_file())
            self.assertEqual(list((chapter / "mask").iterdir()), [])
            self.assertEqual(list((chapter / "inpainted").iterdir()), [])

    def test_clear_work_folders_preserves_folders_and_other_data(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            mask = root / "Chapter 1" / "Mask"
            inpainted = root / "Chapter 2" / "inpainted"
            result = root / "Chapter 1" / "result"
            mask.mkdir(parents=True)
            inpainted.mkdir(parents=True)
            result.mkdir(parents=True)
            (mask / "1.png").write_bytes(b"mask")
            (mask / "nested").mkdir()
            (mask / "nested" / "2.png").write_bytes(b"nested")
            (inpainted / "1.png").write_bytes(b"inpainted")
            (result / "1.png").write_bytes(b"result")

            folders, removed, errors = comic.clear_work_folders(root)

            self.assertEqual((folders, removed, errors), (2, 3, []))
            self.assertTrue(mask.is_dir())
            self.assertTrue(inpainted.is_dir())
            self.assertEqual(list(mask.iterdir()), [])
            self.assertEqual(list(inpainted.iterdir()), [])
            self.assertTrue((result / "1.png").is_file())

    def test_failed_export_preserves_work_folders(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            chapter = root / "Mangas" / "Series" / "Chapter 1"
            mask = chapter / "mask"
            mask.mkdir(parents=True)
            (mask / "1.png").write_bytes(b"mask")
            app = comic.FileAggregatorApp.__new__(comic.FileAggregatorApp)
            app.events = comic.queue.Queue()

            app.export_worker([chapter], root / "Komga", True)

            self.assertTrue((mask / "1.png").is_file())

    def test_export_cleanup_does_not_touch_unselected_chapters(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            selected = root / "Mangas" / "Series" / "Chapter 1"
            unselected = root / "Mangas" / "Series" / "Chapter 2"
            (selected / "result").mkdir(parents=True)
            (selected / "result" / "1.png").write_bytes(b"result")
            for chapter in (selected, unselected):
                (chapter / "mask").mkdir(parents=True, exist_ok=True)
                (chapter / "mask" / "1.png").write_bytes(b"mask")
            app = comic.FileAggregatorApp.__new__(comic.FileAggregatorApp)
            app.events = comic.queue.Queue()

            app.export_worker([selected], root / "Komga", True)

            self.assertEqual(list((selected / "mask").iterdir()), [])
            self.assertTrue((unselected / "mask" / "1.png").is_file())

    def test_export_rejects_output_inside_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = comic.FileAggregatorApp.__new__(comic.FileAggregatorApp)
            value = lambda path: type("Value", (), {"get": lambda self: str(path)})()
            app.base_path = value(root)
            app.komga_path = value(root / "Komga")

            with mock.patch.object(comic.messagebox, "showwarning") as warning:
                app.start_export([])

            warning.assert_called_once_with(
                "警告", "Komga 輸出路徑不能位於漫畫來源路徑內")

    def test_create_cbz(self):
        with tempfile.TemporaryDirectory() as temp:
            series, chapter, _, output_root = fixture(temp)
            action, output = comic.export_chapter(series, chapter, output_root, {})
            self.assertEqual((action, output.is_file()), ("created", True))
            with zipfile.ZipFile(output) as cbz:
                self.assertIsNone(cbz.testzip())

    def test_cbz_root_entries(self):
        with tempfile.TemporaryDirectory() as temp:
            series, chapter, _, output_root = fixture(temp)
            _, output = comic.export_chapter(series, chapter, output_root, {})
            with zipfile.ZipFile(output) as cbz:
                self.assertEqual(cbz.namelist(), ["1.webp", "2.jpg", "10.png"])

    def test_incremental_skip(self):
        with tempfile.TemporaryDirectory() as temp:
            series, chapter, _, output_root = fixture(temp)
            state = {}
            comic.export_chapter(series, chapter, output_root, state)
            self.assertEqual(comic.export_chapter(series, chapter, output_root, state)[0], "skipped")

    def test_incremental_update(self):
        with tempfile.TemporaryDirectory() as temp:
            series, chapter, result, output_root = fixture(temp)
            state = {}
            comic.export_chapter(series, chapter, output_root, state)
            time.sleep(0.01)
            (result / "2.jpg").write_bytes(b"changed")
            self.assertEqual(comic.export_chapter(series, chapter, output_root, state)[0], "updated")

    def test_atomic_replace(self):
        with tempfile.TemporaryDirectory() as temp:
            series, chapter, result, output_root = fixture(temp)
            _, output = comic.export_chapter(series, chapter, output_root, {})
            original = output.read_bytes()
            with self.assertRaises(OSError):
                comic.create_cbz(comic.image_files(result), output,
                                 lambda *_: (_ for _ in ()).throw(OSError("stop")))
            self.assertEqual(output.read_bytes(), original)
            self.assertFalse(output.with_suffix(".cbz.tmp").exists())

    def test_translation_states(self):
        with tempfile.TemporaryDirectory() as temp:
            chapter = Path(temp) / "Chapter 1"
            chapter.mkdir()
            self.assertEqual(comic.translation_status(chapter)[0], "未翻譯")
            (chapter / "result").mkdir()
            self.assertEqual(comic.translation_status(chapter)[0], "結果為空")
            (chapter / "result" / "1.png").write_bytes(b"image")
            self.assertEqual(comic.translation_status(chapter)[0], "可匯出")

    def test_translation_status_accepts_result_case(self):
        with tempfile.TemporaryDirectory() as temp:
            chapter = Path(temp) / "Chapter 1"
            result = chapter / "Result"
            result.mkdir(parents=True)
            (result / "1.png").write_bytes(b"image")
            self.assertEqual(comic.translation_status(chapter)[0], "可匯出")

    def test_aggregate_direct_copy(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first, second = root / "Chapter 12", root / "Chapter 13"
            first.mkdir()
            second.mkdir()
            (first / "2.png").write_bytes(b"2")
            (first / "notes.json").write_text("{}")
            (second / "1.webp").write_bytes(b"1")
            app = comic.FileAggregatorApp.__new__(comic.FileAggregatorApp)
            app.base_path = type("Value", (), {"get": lambda self: str(root)})()
            app.folders = [(str(first), first.name, comic.Decimal("12")),
                           (str(second), second.name, comic.Decimal("13"))]
            output = app.aggregate_folders(app.folders, 0, 1)
            self.assertEqual([path.name for path in comic.image_files(output)], ["1.png", "2.webp"])
            with_existing = [(str(output), output.name, comic.Decimal("12")), *app.folders]
            output = app.aggregate_folders(with_existing, 0, 2)
            self.assertEqual([path.name for path in comic.image_files(output)], ["1.png", "2.webp"])
            (output / "stale.png").write_bytes(b"stale")
            output = app.aggregate_folders(app.folders, 0, 1)
            self.assertFalse((output / "stale.png").exists())
            original = {path.name: path.read_bytes() for path in output.iterdir()}
            with mock.patch.object(comic.shutil, "copy2", side_effect=OSError("stop")):
                with self.assertRaises(OSError):
                    app.aggregate_folders(app.folders, 0, 1)
            self.assertEqual({path.name: path.read_bytes() for path in output.iterdir()}, original)
            backup = root / ".Chapter 12-13.backup"
            output.rename(backup)
            with mock.patch.object(comic.shutil, "copy2", side_effect=OSError("stop")):
                with self.assertRaises(OSError):
                    app.aggregate_folders(app.folders, 0, 1)
            self.assertEqual({path.name: path.read_bytes() for path in output.iterdir()}, original)
            self.assertFalse((root / "temp").exists())
            self.assertFalse((root / ".Chapter 12-13.tmp").exists())
            self.assertFalse((root / ".Chapter 12-13.backup").exists())

    def test_long_confirmation_summary(self):
        names = [f"Chapter {number}" for number in range(1, 13)]
        summary = comic.summarize_names(names)
        self.assertIn("Chapter 10", summary)
        self.assertNotIn("Chapter 11", summary)
        self.assertIn("共 12 個章節", summary)

    def test_updated_at_uses_newest_image(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / "Chapter 1"
            folder.mkdir()
            older = folder / "1.png"
            newer = folder / "2.png"
            older.write_bytes(b"old")
            newer.write_bytes(b"new")
            os.utime(older, (100, 100))
            os.utime(newer, (200, 200))
            os.utime(folder, (50, 50))

            self.assertEqual(comic.updated_at(folder, [older, newer]), 200)

    def test_folder_size_and_format(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder / "nested").mkdir()
            (folder / "1.bin").write_bytes(b"a" * 1024)
            (folder / "nested" / "2.bin").write_bytes(b"b" * 512)

            self.assertEqual(comic.folder_size(folder), 1536)
            self.assertEqual(comic.format_size(1536), "1.5 KB")

    def test_aggregate_requires_tree_selection(self):
        app = comic.FileAggregatorApp.__new__(comic.FileAggregatorApp)
        app.folder_tree = type("Tree", (), {"selection": lambda self: ()})()
        with mock.patch.object(comic.messagebox, "showwarning") as warning:
            app.confirm_aggregate()
        warning.assert_called_once_with("警告", "請先選擇父系列或章節")

    def test_tree_selection_defaults_to_last_chapter(self):
        class Entry:
            def delete(self, *_):
                self.value = ""

            def insert(self, _, value):
                self.value = value

        series = Path("D:/Mangas/Series")
        chapters = [(str(series / f"Chapter {number}"), f"Chapter {number}", comic.Decimal(number))
                    for number in (1, 2, 3)]
        app = comic.FileAggregatorApp.__new__(comic.FileAggregatorApp)
        app.start_entry, app.end_entry = Entry(), Entry()
        app.series_groups = {series: chapters}
        app.tree_items = {"series": ("series", series), "chapter": ("chapter", series / "Chapter 2")}

        app.folder_tree = type("Tree", (), {"selection": lambda self: ("series",)})()
        app.on_tree_select()
        self.assertEqual((app.start_entry.value, app.end_entry.value), ("1", "3"))

        app.folder_tree = type("Tree", (), {"selection": lambda self: ("chapter",)})()
        app.on_tree_select()
        self.assertEqual((app.start_entry.value, app.end_entry.value), ("2", "3"))

    def test_tree_numbers_series_by_time_and_chapters_by_number(self):
        class Tree:
            def __init__(self):
                self.items = []

            def get_children(self):
                return ()

            def delete(self, *_):
                pass

            def insert(self, parent, _, text, **kwargs):
                item = f"item{len(self.items)}"
                self.items.append((item, parent, text, kwargs.get("values")))
                return item

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for series, chapter, modified in (("Newer", 10, 300), ("Newer", 2, 200),
                                               ("Older", 1, 100)):
                result = root / series / f"Chapter {chapter}" / "result"
                result.mkdir(parents=True)
                image = result / "1.webp"
                image.write_bytes(b"image")
                os.utime(image, (modified, modified))
                os.utime(result.parent, (modified, modified))
            app = comic.FileAggregatorApp.__new__(comic.FileAggregatorApp)
            app.base_path = type("Value", (), {"get": lambda self: str(root)})()
            app.folder_tree = Tree()
            app.scan_status_text = type("Value", (), {"set": lambda self, value: None})()

            app.apply_scan_data(root, app.scan_folder_data(root))

            parents = [item for item in app.folder_tree.items if not item[1]]
            self.assertEqual([item[2] for item in parents], ["1. Newer", "2. Older"])
            newer_children = [item[2] for item in app.folder_tree.items
                              if item[1] == parents[0][0]]
            self.assertEqual(newer_children, ["1. Chapter 2", "2. Chapter 10"])

    def test_background_workers_report_completion(self):
        with tempfile.TemporaryDirectory() as temp:
            series, chapter, _, _ = fixture(temp)
            app = comic.FileAggregatorApp.__new__(comic.FileAggregatorApp)
            app.scan_events = comic.queue.Queue()
            app.aggregate_events = comic.queue.Queue()

            app.scan_worker(series)
            scan_event = app.scan_events.get_nowait()
            self.assertEqual((scan_event[0], len(scan_event[2][0])), ("done", 1))
            self.assertIn(chapter, scan_event[2][4])

            chapters = [(str(chapter), chapter.name, comic.Decimal("1"))]
            (chapter / "1.png").write_bytes(b"image")
            app.aggregate_worker(chapters, 0, 0)
            events = list(app.aggregate_events.queue)
            self.assertEqual(events[-1][0], "done")
            self.assertTrue(events[-1][1].is_dir())

    def test_progress_bars_are_shown_and_hidden(self):
        class Progress:
            def __init__(self):
                self.visible = False
                self.stopped = False

            def configure(self, **kwargs):
                self.options = kwargs

            def pack(self, **kwargs):
                self.visible = True
                self.pack_options = kwargs

            def stop(self):
                self.stopped = True

            def pack_forget(self):
                self.visible = False

        app = comic.FileAggregatorApp.__new__(comic.FileAggregatorApp)
        app.scan_progress = Progress()
        app.progress = Progress()
        app.scan_status_label = object()
        app.status_label = object()

        app.show_scan_progress("indeterminate")
        app.show_export_progress("determinate")
        self.assertTrue(app.scan_progress.visible)
        self.assertTrue(app.progress.visible)

        app.hide_scan_progress()
        app.hide_export_progress()
        self.assertFalse(app.scan_progress.visible)
        self.assertFalse(app.progress.visible)
        self.assertTrue(app.scan_progress.stopped)
        self.assertTrue(app.progress.stopped)


if __name__ == "__main__":
    unittest.main()
