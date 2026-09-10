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
        for handle in self.root.tk.call("after", "info"):
            self.root.tk.call("after", "cancel", handle)
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
        deadline = time.monotonic() + 1
        while self.app.search_after is not None and time.monotonic() < deadline:
            self.root.update()
            time.sleep(.01)
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

    def test_translation_completion_opens_final_location_once(self):
        from queue_worker import run_jobs
        chapter = self.chapter("Chapter 1, 測試")
        (chapter / "1.png").write_bytes(b"source")
        q = self.app.translation_queue
        for exporting in (False, True):
            with self.subTest(exporting=exporting), \
                    mock.patch("queue_worker.translator_command", return_value=([], self.folder, None)), \
                    mock.patch("queue_worker.run_translation"), \
                    mock.patch("translation_queue.os.startfile") as startfile, \
                    mock.patch("translation_queue.subprocess.Popen") as explorer:
                q.add_paths([chapter], "translate")
                q.active_jobs = tuple(q.jobs)
                run_jobs(q.active_jobs, dict(bt_path="installed", bt_config="", bt_export=exporting),
                         str(self.folder / "Komga"), True, q.stop, q.events.put)
                q.poll()
                self.assertEqual(q.jobs[0].status, "done")
                if exporting:
                    archive = self.folder / "Komga" / "Series" / f"{chapter.name}.cbz"
                    self.assertTrue(archive.is_file())
                    explorer.assert_called_once_with(f'explorer.exe /select,"{archive}"')
                    startfile.assert_not_called()
                else:
                    startfile.assert_called_once_with(chapter / "result")
                    explorer.assert_not_called()
                q.start()  # Already completed jobs must not reopen their results.
                self.assertEqual(startfile.call_count + explorer.call_count, 1)

    def test_result_open_failure_does_not_interrupt_queue_completion(self):
        q = self.app.translation_queue
        chapter = self.chapter("Chapter 1")
        q.add_paths([chapter], "translate")
        q.active_jobs = tuple(q.jobs)
        q.running = True
        self.app.set_manga_busy(True)
        for event in (("status", 0, "done", ""), ("result", chapter / "result"), ("done",)):
            q.events.put(event)
        with mock.patch("translation_queue.os.startfile", side_effect=OSError("cannot open")), \
                mock.patch("translation_queue.messagebox.showwarning") as warning:
            q.poll()
        warning.assert_called_once()
        self.assertIn(str(chapter / "result"), warning.call_args.args[1])
        self.assertEqual(q.jobs[0].status, "done")
        self.assertFalse(q.running)
        self.assertFalse(self.app.manga_busy)

    def test_layout_fits_standard_desktop(self):
        self.root.update_idletasks()
        self.assertLessEqual(self.root.winfo_reqheight(), 780)
        self.assertEqual(len(self.app.work_tabs.tabs()), 3)

    def test_four_stage_progress_remains_independent_and_resets_per_job(self):
        from queue_worker import BT_STAGES
        from ui_language import tr
        q = self.app.translation_queue
        q.jobs = [Job(self.folder / "Chapter 1", "translate"), Job(self.folder / "Chapter 2", "translate")]
        q.active_jobs = tuple(q.jobs)
        q.render()
        for event in (("status", 0, "running", ""), ("bt_reset", 100, dict.fromkeys(BT_STAGES, True)),
                      ("bt_progress", "Text Detection", 90, 90, 100, "00:10"),
                      ("bt_progress", "OCR", 60, 60, 100, "01:30"),
                      ("bt_progress", "Inpaint", 55, 55, 100, "02:00"),
                      ("bt_progress", "Translation", 40, 40, 100, "12:34")):
            q.events.put(event)
        q.poll()
        self.assertEqual([q.bt_bars[name]["value"] for name in BT_STAGES], [90, 60, 55, 40])
        self.assertIn("90/100", q.bt_labels["Text Detection"].get())
        self.assertIn("12:34", q.bt_labels["Translation"].get())
        q.events.put(("status", 0, "cancelled", "stop"))
        q.poll()
        self.assertIn(tr("已停止"), q.bt_labels["Translation"].get())
        self.assertNotIn("12:34", q.bt_labels["Translation"].get())
        q.events.put(("status", 1, "running", ""))
        q.events.put(("bt_reset", 20, {name: name != "OCR" for name in BT_STAGES}))
        q.poll()
        self.assertEqual(q.bt_labels["OCR"].get(), tr("未啟用"))
        self.assertIn("0/20", q.bt_labels["Translation"].get())
        self.assertEqual(q.bt_bars["Text Detection"]["value"], 0)

    def test_export_progress_does_not_overwrite_translation_bars(self):
        q = self.app.translation_queue
        q.events.put(("bt_progress", "Translation", 100, 960, 960, "00:00"))
        q.events.put(("stage", "匯出", 50))
        q.poll()
        self.assertEqual(q.stage["value"], 50)
        self.assertEqual(q.stage.winfo_manager(), "pack")
        self.assertEqual(q.bt_bars["Translation"]["value"], 100)
        self.assertIn("960/960", q.bt_labels["Translation"].get())
