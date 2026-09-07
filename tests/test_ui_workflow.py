import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest import mock

from test_comic_sorting import comic
from queue_worker import Job
from ui_language import set_language


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name).resolve()
        self.settings = self.folder / "settings.json"
        self.patch = mock.patch.object(comic, "settings_path", return_value=self.settings)
        self.patch.start()
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = comic.FileAggregatorApp(self.root)

    def tearDown(self):
        self.root.update_idletasks()
        self.root.destroy()
        self.patch.stop()
        self.temp.cleanup()
        set_language("zh-TW")

    def chapter(self, name):
        path = self.folder / "Comics" / "Series" / name
        (path / "result").mkdir(parents=True)
        (path / "result/1.png").write_bytes(b"image")
        return path

    def test_multi_selection_filter_and_rescan_preserve_scope(self):
        first, second = self.chapter("Chapter 1"), self.chapter("Chapter 2")
        base = self.folder / "Comics"
        self.app.apply_scan_data(base, self.app.scan_folder_data(base))
        parent = self.app.folder_tree.get_children()[0]
        children = self.app.folder_tree.get_children(parent)
        self.app.folder_tree.selection_set(parent, *children)
        self.assertEqual(self.app.selected_chapters(), [first, second])
        with mock.patch.object(self.app, "start_export") as export:
            self.app.export_selected()
            export.assert_called_once_with([first, second])
        self.app.folder_tree.selection_set(children[1])
        self.app.folder_tree.item(parent, open=True)
        self.app.apply_scan_data(base, self.app.scan_folder_data(base))
        self.assertEqual(self.app.selected_chapters(), [second])
        parent = self.app.folder_tree.get_children()[0]
        self.assertTrue(self.app.folder_tree.item(parent, "open"))
        self.app.search_text.set("Chapter 1")
        parent = self.app.folder_tree.get_children()[0]
        self.app.folder_tree.selection_set(parent)
        self.assertEqual(self.app.selected_chapters(), [first])

    def test_queue_persists_orders_retries_and_skips_completed(self):
        first, second = self.chapter("Chapter 1"), self.chapter("Chapter 2")
        q = self.app.translation_queue
        q.add_paths([first, second], "export")
        q.tree.selection_set(str(id(q.jobs[1])))
        q.move(-1)
        self.assertEqual([j.path for j in q.jobs], [second, first])
        q.jobs[0].status = "failed"
        q.jobs[0].error = "problem"
        q.retry()
        self.assertEqual((q.jobs[0].status, q.jobs[0].error), ("pending", ""))
        q.jobs[0].status = "done"
        self.app.komga_path.set(str(self.folder / "Komga"))
        q.start()
        deadline = time.monotonic() + 5
        while q.running and time.monotonic() < deadline:
            self.root.update()
            time.sleep(.01)
        self.assertFalse(q.running)
        self.assertTrue((self.folder / "Komga/Series/Chapter 1.cbz").is_file())
        self.assertFalse((self.folder / "Komga/Series/Chapter 2.cbz").exists())
        self.assertEqual([j.status for j in q.jobs], ["done", "done"])
        with mock.patch("translation_queue.threading.Thread") as thread:
            q.start()
            thread.assert_not_called()
        self.root.destroy()
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = comic.FileAggregatorApp(self.root)
        restored = self.app.translation_queue
        self.assertEqual([(j.path, j.status) for j in restored.jobs], [(second, "done"), (first, "done")])
        restored.clear_completed()
        self.assertEqual(restored.jobs, [])

    def test_cancelled_predecessor_cannot_be_bypassed(self):
        chapter = self.chapter("Chapter 1")
        q = self.app.translation_queue
        q.jobs = [Job(chapter, "export", "cancelled"), Job(chapter, "cleanup")]
        with mock.patch.object(comic.messagebox, "showerror") as error:
            self.assertFalse(q.validate())
        error.assert_called_once()

    def test_layout_fits_standard_desktop(self):
        self.root.update_idletasks()
        self.assertLessEqual(self.root.winfo_reqheight(), 780)
        self.assertEqual(len(self.app.work_tabs.tabs()), 3)
