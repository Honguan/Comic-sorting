import os
import tempfile
import threading
import time
import tkinter as tk
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import comic_core
from queue_worker import Job, run_jobs
from test_comic_sorting import comic
from ui_language import LANGUAGES, set_language


class FileImprovementsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def chapter(self, name, pages=("1", "2")):
        chapter = self.root / name
        (chapter / "result").mkdir(parents=True)
        for page in pages:
            (chapter / f"{page}.png").write_bytes(f"source {page}".encode())
            (chapter / "result" / f"{page}.png").write_bytes(f"translated {page}".encode())
        return chapter

    def test_partial_translation_cannot_export_or_cleanup(self):
        chapter = self.chapter("Series/Chapter 1")
        (chapter / "result/2.png").unlink()
        (chapter / "mask").mkdir()
        (chapter / "mask/keep.png").write_bytes(b"keep")
        self.assertEqual(comic_core.translation_status(chapter)[0], "部分翻譯")
        events = []
        run_jobs([Job(chapter, "export"), Job(chapter, "cleanup")], {"bt_cleanup": True},
                 str(self.root / "Komga"), True, threading.Event(), events.append)
        self.assertEqual([e[2] for e in events if e[0] == "status"], ["running", "failed", "blocked"])
        self.assertTrue((chapter / "mask/keep.png").exists())
        self.assertFalse((self.root / "Komga/Series/Chapter 1.cbz").exists())

    def test_same_named_sources_cannot_overwrite_each_other(self):
        first = self.chapter("A/Series/Chapter 1")
        second = self.chapter("B/Series/Chapter 1")
        (second / "result/1.png").write_bytes(b"different")
        state = {}
        _, output = comic_core.export_chapter(first.parent, first, self.root / "Komga", state)
        original = output.read_bytes()
        with self.assertRaisesRegex(ValueError, "來源"):
            comic_core.export_chapter(second.parent, second, self.root / "Komga", state)
        self.assertEqual(output.read_bytes(), original)

    def test_changed_archive_is_rebuilt_and_unchanged_archive_is_not_read(self):
        chapter = self.chapter("Series/Chapter 1")
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                state = {}
                _, output = comic_core.export_chapter(chapter.parent, chapter, self.root / "Komga", state)
                if legacy:
                    state["Series/Chapter 1"].pop("outputStat", None)
                output.write_bytes(b"truncated")
                self.assertEqual(comic_core.export_chapter(chapter.parent, chapter, self.root / "Komga", state)[0], "updated")
                with zipfile.ZipFile(output) as archive:
                    self.assertEqual(archive.read("1.png"), b"translated 1")
                with mock.patch("comic_core.validate_cbz") as validate, mock.patch("comic_core.create_cbz") as create:
                    self.assertEqual(comic_core.export_chapter(chapter.parent, chapter, self.root / "Komga", state)[0], "skipped")
                    validate.assert_not_called()
                    create.assert_not_called()

    def test_legacy_state_is_validated_once(self):
        chapter = self.chapter("Series/Chapter 1")
        state = {}
        comic_core.export_chapter(chapter.parent, chapter, self.root / "Komga", state)
        state["Series/Chapter 1"].pop("outputStat", None)
        with mock.patch("comic_core.validate_cbz", wraps=comic_core.validate_cbz) as validate:
            for _ in range(2):
                self.assertEqual(comic_core.export_chapter(chapter.parent, chapter, self.root / "Komga", state)[0], "skipped")
            self.assertEqual(validate.call_count, 1)

    def test_overlapping_aggregate_preserves_merged_pages(self):
        merged = self.chapter("Series/Chapter 1-3", ("1", "2", "3"))
        last = self.chapter("Series/Chapter 3", ("1",))
        chapters = [(str(p), p.name, comic.chapter_number(p.name)) for p in (merged, last)]
        app = comic.FileAggregatorApp.__new__(comic.FileAggregatorApp)
        with self.assertRaisesRegex(ValueError, "重疊"):
            app.aggregate_folders(chapters, 0, 1)
        self.assertEqual(len(comic_core.image_files(merged)), 3)
        self.assertEqual((merged / "result/3.png").read_bytes(), b"translated 3")

    def test_aggregate_continues_full_ranges_without_dropping_sources(self):
        first = self.chapter("Series/Chapter 1-3", ("1", "2", "3"))
        second = self.chapter("Series/Chapter 4-6", ("4", "5", "6"))
        chapters = [(str(p), p.name, comic.chapter_number(p.name)) for p in (first, second)]
        app = comic.FileAggregatorApp.__new__(comic.FileAggregatorApp)
        output = app.aggregate_folders(chapters, 0, 1)
        self.assertEqual(output.name, "Chapter 1-6")
        self.assertEqual([p.read_bytes() for p in comic_core.image_files(output)],
                         [f"source {i}".encode() for i in range(1, 7)])

    def test_selecting_only_existing_merge_preserves_translations(self):
        chapter = self.chapter("Series/Chapter 1-3")
        app = comic.FileAggregatorApp.__new__(comic.FileAggregatorApp)
        self.assertEqual(app.aggregate_folders([(str(chapter), chapter.name, comic.Decimal(1))], 0, 0), chapter)
        self.assertEqual((chapter / "result/2.png").read_bytes(), b"translated 2")

    def test_partial_results_are_not_ready_in_scan(self):
        chapter = self.chapter("Series/Chapter 1")
        (chapter / "result/2.png").unlink()
        data = comic.FileAggregatorApp.scan_folder_data(self.root)
        self.assertNotIn(chapter, data[4])
        self.assertIn("部分翻譯", data[5][chapter])

    def test_export_worker_collision_preserves_second_sources_and_work(self):
        first = self.chapter("A/Series/Chapter 1")
        second = self.chapter("B/Series/Chapter 1")
        (second / "mask").mkdir()
        (second / "mask/keep.png").write_bytes(b"keep")
        app = comic.FileAggregatorApp.__new__(comic.FileAggregatorApp)
        app.events = comic.queue.Queue()
        app.export_worker([first, second], str(self.root / "Komga"), True)
        event = list(app.events.queue)[-1]
        self.assertEqual((event[1]["created"], event[1]["failed"]), (1, 1))
        self.assertTrue((second / "mask/keep.png").exists())

    def test_shared_export_rejects_containing_paths(self):
        chapter = self.chapter("Series/Chapter 1")
        for target in (chapter, chapter / "Komga", chapter.parent):
            with self.subTest(target=target), self.assertRaisesRegex(ValueError, "互相包含"):
                comic_core.export_chapter(chapter.parent, chapter, target, {})

    def test_aggregate_empty_source_fails_before_cleanup(self):
        first = self.chapter("Series/Chapter 1", ())
        (first / "notes.txt").write_text("keep")
        second = self.chapter("Series/Chapter 2")
        chapters = [(str(p), p.name, comic.chapter_number(p.name)) for p in (first, second)]
        app = comic.FileAggregatorApp.__new__(comic.FileAggregatorApp)
        app.aggregate_events = comic.queue.Queue()
        app.aggregate_worker(chapters, 0, 1, True)
        self.assertEqual(app.aggregate_events.get_nowait()[0], "error")
        self.assertTrue((first / "notes.txt").exists())
        self.assertFalse((first.parent / "Chapter 1-2").exists())

    def test_scan_excludes_work_and_aggregate_intermediate_folders(self):
        real = self.chapter("Series/Chapter 1")
        for name in ("Chapter 1/mask/Chapter 9", ".Chapter 1-2.tmp", ".Chapter 1-2.backup"):
            self.chapter(f"Series/{name}")
        self.assertEqual([Path(f[0]) for f in comic.FileAggregatorApp.get_folders_with_numbers(self.root)], [real])

    @unittest.skipUnless(os.name == "nt", "Windows junctions")
    def test_scan_and_size_do_not_follow_junctions(self):
        from _winapi import CreateJunction
        chapter = self.chapter("Series/Chapter 1", ("1",))
        outside = self.chapter("Outside/Chapter 9")
        link = chapter / "linked"
        expected = sum(p.stat().st_size for p in chapter.rglob("*.png"))
        CreateJunction(str(outside.parent), str(link))
        try:
            self.assertEqual(comic_core.folder_size(chapter), expected)
            self.assertEqual([Path(f[0]) for f in comic.FileAggregatorApp.get_folders_with_numbers(chapter.parent)], [chapter])
        finally:
            self.assertEqual(link.parent.resolve(), chapter.resolve())
            link.rmdir()

    @unittest.skipUnless(os.name == "nt", "Windows junctions")
    def test_translation_status_does_not_read_linked_results(self):
        from _winapi import CreateJunction
        chapter = self.root / "Series/Chapter 1"
        chapter.mkdir(parents=True)
        outside = self.chapter("Outside/Chapter 9")
        link = chapter / "result"
        CreateJunction(str(outside / "result"), str(link))
        try:
            self.assertEqual(comic_core.translation_status(chapter), ("未翻譯", []))
        finally:
            self.assertEqual(link.parent.resolve(), chapter.resolve())
            link.rmdir()


class UIImprovementsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name).resolve()
        self.settings_file = self.folder / "settings.json"
        self.patch = mock.patch.object(comic, "settings_path", return_value=self.settings_file)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.close_ui)
        self.app = comic.FileAggregatorApp(self.root)

    def close_ui(self):
        for handle in self.root.tk.call("after", "info"):
            self.root.tk.call("after", "cancel", handle)
        self.root.update_idletasks()
        self.root.destroy()

    def pump(self, seconds=.3):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.root.update()
            time.sleep(.01)

    def test_empty_paths_never_scan_or_cleanup_working_directory(self):
        for path in ("", "   "):
            with self.subTest(path=path), mock.patch.object(comic.threading, "Thread") as worker, \
                    mock.patch.object(comic.messagebox, "showwarning"), \
                    mock.patch.object(comic.messagebox, "askyesno", return_value=True) as confirm:
                self.app.base_path.set(path)
                self.app.load_folders()
                self.app.confirm_cleanup()
                worker.assert_not_called()
                confirm.assert_not_called()

    def test_finished_queue_jobs_are_checkpointed_before_batch_ends(self):
        q = self.app.translation_queue
        q.add_paths([self.folder / "Chapter 1", self.folder / "Chapter 2"], "export")
        q.active_jobs = tuple(q.jobs)
        q.running = True
        self.app.set_manga_busy(True)
        q.events.put(("status", 0, "done", ""))
        q.events.put(("status", 1, "running", ""))
        q.poll()
        saved = comic_core.load_json(self.settings_file, {})["bt_jobs"]
        self.assertEqual([j["status"] for j in saved], ["done", "running"])
        self.assertTrue(q.running)

    def test_checkpoint_failure_requests_stop(self):
        q = self.app.translation_queue
        q.add_paths([self.folder / "Chapter 1"], "export")
        q.active_jobs = tuple(q.jobs)
        q.running = True
        q.events.put(("status", 0, "done", ""))
        with mock.patch.object(self.app, "save_settings", return_value=False):
            q.poll()
        self.assertTrue(q.stop.is_set())

    def test_restart_preserves_completed_jobs_and_requires_retry_for_interrupted_job(self):
        comic_core.save_json(self.settings_file, {"bt_jobs": [
            {"path": str(self.folder / "Chapter 1"), "action": "translate", "status": "done"},
            {"path": str(self.folder / "Chapter 2"), "action": "translate", "status": "running"},
            {"path": "", "action": "cleanup"},
        ]})
        another = tk.Toplevel(self.root)
        app = comic.FileAggregatorApp(another)
        self.assertEqual([j.status for j in app.translation_queue.jobs], ["done", "cancelled"])
        self.assertTrue(app.translation_queue.jobs[1].error)

    def test_search_burst_renders_once_and_updates_selection_count(self):
        chapter = self.folder / "Series/Chapter 1"
        chapter.mkdir(parents=True)
        (chapter / "1.png").touch()
        self.app.apply_scan_data(self.folder, self.app.scan_folder_data(self.folder))
        with mock.patch.object(self.app, "apply_scan_data", wraps=self.app.apply_scan_data) as render:
            for query in ("C", "Ch", "Cha", "Chap", "Chapt", "Chapter"):
                self.app.search_text.set(query)
            self.assertEqual(render.call_count, 0)
            self.pump()
            self.assertEqual(render.call_count, 1)
        parent = self.app.folder_tree.get_children()[0]
        self.app.folder_tree.selection_set(parent)
        self.pump(.02)
        self.assertIn("1", self.app.selection_text.get())
        self.app.search_text.set("no match")
        self.pump()
        self.assertIn("0", self.app.selection_text.get())

    def test_export_options_survive_restart_and_installation_starts_empty(self):
        self.assertEqual(self.app.translation_queue.installation.get(), "")
        self.app.skip_unchanged.set(False)
        self.app.open_after_export.set(True)
        self.app.save_settings()
        another = tk.Toplevel(self.root)
        app = comic.FileAggregatorApp(another)
        self.assertFalse(app.skip_unchanged.get())
        self.assertTrue(app.open_after_export.get())

    def test_valid_typed_path_is_saved_before_scan(self):
        self.app.base_path.set(str(self.folder))
        with mock.patch.object(comic.threading, "Thread") as thread:
            self.app.load_folders()
        self.assertEqual(comic_core.load_json(self.settings_file, {})["manga_path"], str(self.folder))
        thread.assert_called_once()

    def test_missing_output_and_job_errors_select_relevant_tabs(self):
        q = self.app.translation_queue
        q.add_paths([self.folder / "missing"], "export")
        with mock.patch.object(comic.messagebox, "showerror"):
            self.assertFalse(q.validate())
            self.assertEqual(self.app.work_tabs.select(), str(self.app.export_tab))
            self.app.komga_path.set(str(self.folder / "Komga"))
            self.assertFalse(q.validate())
            self.assertEqual(self.app.work_tabs.select(), str(self.app.queue_tab))
            q.jobs[0].action = "translate"
            self.assertFalse(q.validate())
            self.assertEqual(self.app.work_tabs.select(), str(self.app.settings_tab))

    def test_queue_redraw_preserves_scroll_selection_and_focus(self):
        q = self.app.translation_queue
        q.add_paths([self.folder / f"Chapter {i}" for i in range(100)], "export")
        self.root.deiconify()
        self.root.update_idletasks()
        item = str(id(q.jobs[50]))
        q.tree.selection_set(item)
        q.tree.focus(item)
        q.tree.yview_moveto(.45)
        before = q.tree.yview()
        q.render()
        self.root.update_idletasks()
        self.assertEqual(q.tree.selection(), (item,))
        self.assertEqual(q.tree.focus(), item)
        self.assertAlmostEqual(q.tree.yview()[0], before[0])

    def test_busy_selection_summary_stays_current(self):
        chapter = self.folder / "Series/Chapter 1"
        chapter.mkdir(parents=True)
        (chapter / "1.png").touch()
        self.app.apply_scan_data(self.folder, self.app.scan_folder_data(self.folder))
        self.app.set_manga_busy(True)
        self.app.folder_tree.selection_set(self.app.folder_tree.get_children()[0])
        self.app.on_tree_select()
        self.assertIn("1", self.app.selection_text.get())

    def test_explicit_multi_selection_excludes_unselected_retained_chapter(self):
        for name in ("Chapter 1-45", "Chapter 45", "Chapter 46"):
            chapter = self.folder / "Series" / name
            chapter.mkdir(parents=True)
            (chapter / "1.png").touch()
        self.app.apply_scan_data(self.folder, self.app.scan_folder_data(self.folder))
        parent = self.app.folder_tree.get_children()[0]
        children = self.app.folder_tree.get_children(parent)
        self.app.folder_tree.selection_set(children[0], children[2])
        self.app.on_tree_select()
        self.assertEqual(str(self.app.start_entry.cget("state")), "disabled")
        self.assertEqual(str(self.app.end_entry.cget("state")), "disabled")
        with mock.patch.object(comic.messagebox, "askyesno", return_value=True), \
                mock.patch.object(comic.messagebox, "showwarning") as warning, \
                mock.patch.object(comic.threading, "Thread") as thread:
            self.app.confirm_aggregate()
        warning.assert_not_called()
        chapters, start, end, cleanup = thread.call_args.kwargs["args"]
        self.assertEqual([item[1] for item in chapters], ["Chapter 1-45", "Chapter 46"])
        self.assertEqual((start, end), (0, 1))
        self.app.aggregate_worker(chapters, start, end, True)
        self.assertTrue((self.folder / "Series/Chapter 45/1.png").exists())
        self.assertEqual(len(comic_core.image_files(self.folder / "Series/Chapter 1-46")), 2)

    def test_minimum_window_keeps_actions_visible_in_all_languages(self):
        self.root.withdraw()
        try:
            for language in LANGUAGES:
                with self.subTest(language=language), mock.patch.object(comic, "load_json", return_value={"ui_language": language}):
                    window = tk.Toplevel(self.root)
                    window.geometry("820x640")
                    app = comic.FileAggregatorApp(window)
                    self.pump(.1)
                    for name in app.translation_queue.bt_bars:
                        app.translation_queue.bt_progress[name] = (60, 582, 960, "4:00:04", True)
                        app.translation_queue.show_bt_progress(name)
                    app.translation_queue.stage.pack(fill="x", before=app.translation_queue.bt_frame)
                    details = [widget for widget in next(iter(app.translation_queue.bt_bars.values())).master.winfo_children()
                               if isinstance(widget, tk.ttk.Label)]
                    for tab, controls in (
                            (app.queue_tab, (app.translation_queue.start_button, app.translation_queue.stop_button, app.translation_queue.range_button,
                                             app.translation_queue.total, app.translation_queue.stage,
                                             *app.translation_queue.bt_bars.values(), *details)),
                            (app.export_tab, (app.export_selected_button, app.export_all_button, app.cleanup_button)),
                            (app.settings_tab, (app.translation_queue.controls[0],))):
                        app.work_tabs.select(tab)
                        self.pump(.04)
                        for widget in (*controls, app.aggregate_button):
                            self.assertTrue(widget.winfo_ismapped(), str(widget))
                            self.assertGreater(widget.winfo_width(), 1)
                            self.assertLessEqual(widget.winfo_rootx() - window.winfo_rootx() + widget.winfo_reqwidth(), 820)
                            self.assertLessEqual(widget.winfo_rooty() - window.winfo_rooty() + widget.winfo_reqheight(), 640)
                    window.destroy()
        finally:
            set_language("zh-TW")


if __name__ == "__main__":
    unittest.main()
