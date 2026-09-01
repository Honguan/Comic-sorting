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
            output_root = root / "Komga"
            app = comic.FileAggregatorApp.__new__(comic.FileAggregatorApp)
            app.events = comic.queue.Queue()

            app.export_worker([str(chapter)], str(output_root), True)

            self.assertTrue((output_root / "Killer Shark" / "Chapter 10-42.cbz").is_file())

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
            self.assertFalse((root / "temp").exists())

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


if __name__ == "__main__":
    unittest.main()
