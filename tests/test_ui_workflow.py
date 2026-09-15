import tempfile
import threading
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest import mock

from test_comic_sorting import comic
from queue_worker import Job
from ui_language import set_language


def detail_values(window):
    return "\n".join(str(value) for table in window.detail_tables.values()
                     for item in table.get_children() for value in table.item(item, 'values'))


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

    def wait_counts(self, q):
        deadline = time.monotonic() + 3
        while q.counting and time.monotonic() < deadline:
            self.root.update()
            time.sleep(.01)
        self.assertFalse(q.counting)

    def test_failed_scan_restores_visible_source_and_blocks_edited_path(self):
        chapter = self.chapter('Chapter 1')
        base = chapter.parent.parent
        self.app.base_path.set(str(base))
        self.app.apply_scan_data(base, self.app.scan_folder_data(base))
        other = self.folder / 'other'
        other.mkdir()
        self.app.base_path.set(str(other))
        with mock.patch.object(comic.messagebox, 'showwarning'), mock.patch.object(comic.threading, 'Thread') as worker:
            self.app.confirm_cleanup()
            worker.assert_not_called()
        self.app.scan_events.put(('error', PermissionError('test')))
        with mock.patch.object(comic.messagebox, 'showerror'):
            self.app.poll_scan_events()
        self.assertEqual(self.app.base_path.get(), str(base))
        self.assertEqual(self.app.scan_data[0], base)

    def test_queue_checkpoint_failure_never_starts_second_job(self):
        from comic_core import load_json
        q = self.app.translation_queue
        for fail_at in ('settings', 'history'):
            q.jobs = [Job(self.chapter(f'Checkpoint {fail_at} 1'), 'cleanup'), Job(self.chapter(f'Checkpoint {fail_at} 2'), 'cleanup')]
            q.render()
            save_settings, save_history = self.app.save_settings, q.save_history
            def settings():
                return False if fail_at == 'settings' and q.jobs[0].status == 'done' else save_settings()
            def history(*args, **kwargs):
                return False if fail_at == 'history' and q.jobs[0].status == 'done' else save_history(*args, **kwargs)
            with mock.patch.object(q, 'confirm_cleanup', return_value=True), mock.patch.object(q, 'notify_job'), mock.patch.object(q, 'notify_run'), \
                    mock.patch.object(self.app, 'save_settings', side_effect=settings), mock.patch.object(q, 'save_history', side_effect=history), \
                    mock.patch('queue_worker.clear_work_folders', return_value=(0, 0, [])) as clean:
                q.start()
                deadline = time.monotonic() + 4
                while q.running and time.monotonic() < deadline:
                    self.root.update()
                    time.sleep(.01)
                self.assertFalse(q.running)
                self.assertTrue(q.stop.is_set())
                self.assertEqual(clean.call_count, 1)
                self.assertEqual([j.status for j in q.jobs], ['done', 'pending'])
            self.assertTrue(load_json(self.settings, {})['bt_jobs'])

    def test_queue_count_cache_skips_disk_on_move_and_refreshes_on_rescan(self):
        q = self.app.translation_queue
        paths = [self.chapter('Chapter 1'), self.chapter('Chapter 2')]
        for path in paths:
            (path / '1.png').touch()
        q.add_paths(paths)
        self.wait_counts(q)
        q.tree.selection_set(str(id(q.jobs[1])))
        with mock.patch('translation_queue.image_files', side_effect=AssertionError('must not read disk')):
            q.move(-1)
            q.render()
            self.assertFalse(q.counting)
        (paths[0] / '2.png').touch()
        q.render(refresh_counts=True)
        self.wait_counts(q)
        self.assertEqual(sum(q.pending_file_counts.values()), 3)

    def chapter(self, name):
        path = self.folder / "Comics" / "Series" / name
        (path / "result").mkdir(parents=True)
        (path / "result/1.png").write_bytes(b"image")
        return path

    def test_queue_actions_follow_selection_and_running_state(self):
        q = self.app.translation_queue
        q.jobs = [Job(self.chapter('Chapter 1'), 'translate', 'failed', 'error'),
                  Job(self.chapter('Chapter 2'), 'translate', 'done')]
        q.render()
        q.update_controls()
        self.assertEqual(str(q.selection_buttons['移除選取']['state']), 'disabled')
        self.assertEqual(str(q.selection_buttons['重試選取']['state']), 'disabled')
        q.tree.selection_set(str(id(q.jobs[0])))
        q.update_controls()
        self.assertEqual(str(q.selection_buttons['上移']['state']), 'disabled')
        self.assertEqual(str(q.selection_buttons['下移']['state']), 'normal')
        self.assertEqual(str(q.selection_buttons['重試選取']['state']), 'normal')
        before = [(id(job), job.status, job.error) for job in q.jobs]
        q.running = True
        try:
            q.update_controls()
            self.assertTrue(all(str(button['state']) == 'disabled' for button in q.selection_buttons.values()))
            q.add_paths([q.jobs[0].path])
            q.add_paths([self.folder / 'extra'])
            q.retry()
            q.move(1)
            q.clear_completed()
            self.assertEqual([(id(job), job.status, job.error) for job in q.jobs], before)
            q.pause_requested.set()
            q.update_controls()
            self.assertEqual(q.start_button['text'], '繼續佇列')
        finally:
            q.running = False
            q.pause_requested.clear()

    def test_table_shortcuts_select_copy_and_clear_without_editing_queue(self):
        from ui_interactions import bind_table_shortcuts
        q = self.app.translation_queue
        q.jobs = [Job(self.chapter('Chapter 1'), 'translate'), Job(self.chapter('Chapter 2'), 'translate')]
        q.render()
        # Invoke the registered Tcl callbacks so this also works with a withdrawn test window.
        bind_table_shortcuts(q.tree, q.selected_paths)
        for event in ('<Control-a>', '<Control-c>'):
            command = q.tree.bind(event).split('[', 1)[1].split()[0]
            self.root.tk.call(command)
        self.assertEqual(self.root.clipboard_get().splitlines(), [str(j.path) for j in q.jobs])
        self.assertEqual(len(q.selected_job_ids()), 2)
        command = q.tree.bind('<Escape>').split('[', 1)[1].split()[0]
        self.root.tk.call(command)
        self.assertEqual(q.tree.selection(), ())
        self.assertEqual(len(q.jobs), 2)

    def test_live_usage_is_shown_in_separate_columns(self):
        q = self.app.translation_queue
        self.assertEqual(q.usage_table.set('total', 'tokens'), '尚未回報')
        q.usage_records = {(0, 'total'): dict(total_tokens=12345, cost='0.125', requests=4, unpriced_requests=1, missing_usage_requests=1)}
        q.show_usage()
        self.assertEqual(q.usage_table.item('total', 'values'), ('合計', '12,345', 'US$0.13', '4', '僅含已知金額', '回報不完整'))
        q.show_usage()
        self.assertEqual(len(q.usage_table.get_children()), 3)
        self.assertEqual(q.elapsed_label.get(), '00:00:00')

    def test_manga_columns_fit_and_selection_shows_complete_details(self):
        from types import SimpleNamespace
        chapter = self.chapter("Chapter 1-50")
        base = self.folder / "Comics"
        self.app.apply_scan_data(base, self.app.scan_folder_data(base))
        tree = self.app.folder_tree
        for width in (760, 1200, 1600):
            self.app.fit_folder_columns(SimpleNamespace(width=width))
            self.assertLessEqual(sum(tree.column(c, "width") for c in ("#0", *tree['columns'])), width)
        parent = tree.get_children()[0]
        leaf = tree.get_children(parent)[0]
        tree.selection_set(leaf)
        self.app.on_tree_select()
        self.assertIn(str(chapter), self.app.folder_details.get())
        self.assertIn(tree.set(leaf, 'updated'), self.app.folder_details.get())
        self.assertIn(tree.set(leaf, 'status'), self.app.folder_details.get())

    def test_queue_columns_fit_window_and_selected_details_keep_full_path(self):
        from types import SimpleNamespace
        q = self.app.translation_queue
        path = self.folder / ("Long 漫畫 name " * 12) / "Chapter 1-100"
        q.jobs = [Job(path, "translate", status="failed", error="Selected model is at capacity")]
        q.render()
        item = str(id(q.jobs[0]))
        q.tree.selection_set(item)
        q.update_controls()
        for width in (760, 1200, 1600):
            q.fit_columns(SimpleNamespace(width=width))
            self.assertLessEqual(sum(q.tree.column(c, "width") for c in ("#0", *q.tree['columns'])), width)
            self.assertTrue(all(q.tree.column(c, "width") > 0 for c in q.tree['columns']))
        self.assertIn(str(path), q.selection_details.get())
        self.assertIn('滿載', q.selection_details.get())
        self.assertNotIn('LLM_CAPACITY', q.selection_details.get())
        q.tree.selection_set(q.tree.parent(item))
        q.update_controls()
        self.assertEqual(q.selection_details.get(), str(path.parent))

    def test_queue_groups_paths_series_and_chapters_without_reordering_jobs(self):
        q = self.app.translation_queue
        base = self.folder / "Comics"
        paths = [base / "漫畫 A" / "Chapter 1-10", base / "漫畫 B" / "Chapter 1-5", base / "漫畫 A" / "Chapter 11-20"]
        q.jobs = [Job(path, "translate") for path in paths]
        q.render()
        root = q.tree.get_children()[0]
        series = q.tree.get_children(root)
        self.assertEqual(q.tree.item(root, "text"), str(base))
        self.assertEqual([q.tree.item(item, "text") for item in series], ["漫畫 A", "漫畫 B"])
        self.assertEqual([q.tree.item(item, "text") for item in q.tree.get_children(series[0])], ["1. Chapter 1-10", "3. Chapter 11-20"])
        self.assertTrue(q.tree.item(root, "open"))
        self.assertFalse(q.tree.item(series[0], "open"))
        self.assertEqual([job.path for job in q.jobs], paths)
        q.tree.item(root, open=False)
        q.render()
        self.assertFalse(q.tree.item(root, "open"))
        q.tree.item(root, open=True)
        q.tree.item(series[0], open=True)
        q.tree.selection_set(series[0])
        q.render()
        self.assertTrue(q.tree.item(series[0], "open"))
        self.assertEqual(q.selected_job_ids(), {str(id(q.jobs[0])), str(id(q.jobs[2]))})
        q.remove()
        self.assertEqual([job.path for job in q.jobs], [paths[1]])
        q.tree.delete(*q.tree.get_children())
        q.jobs.append(Job(self.folder / "Other" / "漫畫 C" / "Chapter 1", "translate"))
        q.render()
        roots = q.tree.get_children()
        self.assertEqual(len(roots), 2)
        self.assertTrue(all(not q.tree.item(item, "open") for item in roots))

    def test_export_all_manga_names_ignores_filter_and_handles_cancel_and_errors(self):
        base = self.folder / "Comics"
        for name in ("日本語 漫畫", "Series 10", "Series 2"):
            for chapter in ("Chapter 1", "Chapter 2"):
                folder = base / name / chapter / "result"
                folder.mkdir(parents=True)
                (folder / "1.png").write_bytes(b"image")
        self.app.apply_scan_data(base, self.app.scan_folder_data(base))
        self.app.sort_by_column("name")
        self.app.search_text.set("Series 2")
        self.root.after_cancel(self.app.search_after)
        self.app.apply_filter()
        output = self.folder / "names.txt"
        with mock.patch.object(comic.filedialog, "asksaveasfilename", return_value=str(output)), mock.patch.object(comic.messagebox, "showinfo"):
            self.app.export_manga_names()
        self.assertEqual(output.read_text(encoding="utf-8-sig").splitlines(), ["Series 2", "Series 10", "日本語 漫畫"])
        with mock.patch.object(comic.filedialog, "asksaveasfilename", return_value=""), mock.patch.object(Path, "write_text") as write:
            self.app.export_manga_names()
            write.assert_not_called()
        with mock.patch.object(comic.filedialog, "asksaveasfilename", return_value=str(output)), mock.patch.object(Path, "write_text", side_effect=PermissionError("locked")), mock.patch.object(comic.messagebox, "showerror") as error:
            self.app.export_manga_names()
            error.assert_called_once()

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

    def test_parent_menu_selects_all_chapters_including_filtered_then_enqueues(self):
        chapters = [self.chapter(f'Chapter {n}') for n in (1, 2, 10)]
        base = self.folder / 'Comics'
        other = base / 'Other' / 'Chapter 1'
        other.mkdir(parents=True)
        (other / '1.png').write_bytes(b'image')
        self.app.apply_scan_data(base, self.app.scan_folder_data(base))
        tree, q, menu = self.app.folder_tree, self.app.translation_queue, self.app.folder_context_menu
        self.root.deiconify()
        self.root.update()

        def right_click(path):
            item = next(key for key, (_, value) in self.app.tree_items.items() if value == path)
            tree.see(item)
            self.root.update()
            x, y, _, height = tree.bbox(item)
            with mock.patch.object(menu, 'tk_popup') as popup:
                tree.event_generate('<Button-3>', x=x + 50, y=y + height // 2)
                popup.assert_called_once()
            return item

        parent = right_click(chapters[0].parent)
        self.assertFalse(tree.item(parent, 'open'))
        menu.invoke(1)
        self.assertTrue(tree.item(parent, 'open'))
        self.assertEqual(tree.selection(), tree.get_children(parent))
        self.assertEqual(self.app.selected_chapters(), chapters)
        self.assertEqual(self.app.selection_text.get(), '已選取 3 個資料夾')
        tree.selection_remove(tree.get_children(parent)[1])
        self.assertEqual(self.app.selected_chapters(), [chapters[0], chapters[2]])

        self.app.search_text.set('Chapter 2')
        self.root.after_cancel(self.app.search_after)
        self.app.apply_filter()
        parent = right_click(chapters[0].parent)
        self.assertEqual(len(tree.get_children(parent)), 1)
        self.assertIn('清除搜尋', menu.entrycget(1, 'label'))
        menu.invoke(1)
        self.assertEqual(self.app.search_text.get(), '')
        self.assertIsNone(self.app.search_after)
        self.assertEqual(self.app.selected_chapters(), chapters)
        self.assertEqual(len(tree.selection()), 3)
        self.assertTrue(all(self.app.tree_items[item][0] == 'chapter' for item in tree.selection()))
        self.assertEqual(self.app.start_entry.get(), '1')
        self.assertEqual(self.app.end_entry.get(), '3')
        with mock.patch.object(q, 'start') as start:
            q.add_selected()
            q.add_selected()
            start.assert_not_called()
        self.assertEqual([job.path for job in q.jobs], chapters)
        self.assertTrue(all(job.status == 'pending' for job in q.jobs))
        right_click(chapters[0])
        self.assertEqual(menu.entrycget(1, 'state'), 'disabled')
        for running, busy in ((True, False), (False, True)):
            q.running, self.app.manga_busy = running, busy
            parent = right_click(chapters[0].parent)
            self.assertEqual(menu.entrycget(1, 'state'), 'disabled')
            self.app.select_all_chapters(chapters[0].parent, base)
            self.assertEqual(tree.selection(), (parent,))
        q.running = self.app.manga_busy = False
        self.app.apply_scan_data(chapters[0].parent, self.app.scan_folder_data(chapters[0].parent))
        right_click(chapters[0].parent)
        self.assertEqual(menu.entrycget(0, 'state'), 'disabled')  # Root cannot be deleted, but can select chapters.
        menu.invoke(1)
        self.assertEqual(self.app.selected_chapters(), chapters)

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

    def test_folder_types_stay_distinct_in_scan_selection_queue_and_merge(self):
        first, last, merged = [self.chapter(name) for name in ('Chapter 1', 'Chapter 45', 'Chapter 1-45')]
        base = self.folder / 'Comics'
        self.app.apply_scan_data(base, self.app.scan_folder_data(base))
        tree, q = self.app.folder_tree, self.app.translation_queue
        items = {path: item for item, (_, path) in self.app.tree_items.items()}
        self.assertEqual(tree.set(items[first], 'kind'), '單一章節')
        self.assertEqual(tree.set(items[merged], 'kind'), '整合資料夾')
        self.assertEqual(self.app.tree_items[items[merged]][0], 'merged')
        self.assertIn('2 單一', tree.set(items[first.parent], 'status'))
        self.assertIn('1 整合', tree.set(items[first.parent], 'status'))
        self.app.select_all_chapters(first.parent, base, 'chapter')
        self.assertEqual(self.app.selected_chapters(), [first, last])
        q.add_selected()
        self.app.search_text.set('Chapter 45')
        self.root.after_cancel(self.app.search_after)
        self.app.apply_filter()
        self.app.select_all_chapters(first.parent, base, 'merged')
        self.assertEqual(self.app.selected_chapters(), [merged])
        self.assertEqual(self.app.search_text.get(), '')
        q.add_selected()
        self.assertEqual([q.tree.set(str(id(job)), 'kind') for job in q.jobs],
                         ['單一章節', '單一章節', '整合資料夾'])
        # Confirmation skips merged inputs but still warns about a same-named output.
        self.app.select_all_chapters(first.parent, base)
        self.assertTrue(self.app.has_explicit_chapter_selection())
        with mock.patch.object(comic.messagebox, 'showwarning') as warning, \
                mock.patch.object(comic.messagebox, 'askyesno', return_value=False) as confirm, \
                mock.patch.object(comic.threading, 'Thread') as worker:
            self.app.confirm_aggregate()
            worker.assert_not_called()
            warning.assert_not_called()
            self.assertIn('已略過 1 個整合資料夾', confirm.call_args.args[1])
            self.assertIn('既有輸出將被取代', confirm.call_args.args[1])
        # Rescan and double-click retain the merged leaf's existing queue behavior.
        self.app.select_all_chapters(first.parent, base, 'merged')
        self.app.apply_scan_data(base, self.app.scan_folder_data(base))
        self.assertEqual(self.app.selected_chapters(), [merged])
        self.root.deiconify()
        item = next(key for key, (_, path) in self.app.tree_items.items() if path == merged)
        tree.see(item)
        self.root.update()
        x, y, _, height = tree.bbox(item)
        self.double_click(x + 80, y + height // 2, 50000)
        self.assertEqual(len(q.jobs), 3)
        self.assertTrue(all(job.status == 'pending' for job in q.jobs))
        parent = tree.parent(item)
        tree.see(parent)
        self.root.update()
        x, y, _, height = tree.bbox(parent)
        with mock.patch.object(self.app.folder_context_menu, 'tk_popup'):
            tree.event_generate('<Button-3>', x=x + 50, y=y + height // 2)
        self.app.folder_context_menu.invoke(2)
        self.assertEqual(self.app.selected_chapters(), [first, last])
        self.app.folder_context_menu.invoke(3)
        self.assertEqual(self.app.selected_chapters(), [merged])

    def test_queue_error_columns_update_persist_show_details_and_clear_on_retry(self):
        first = self.chapter('Chapter 1')
        q = self.app.translation_queue
        q.add_paths([first], 'translate')
        with mock.patch.object(q, 'validate', return_value=True), mock.patch('translation_queue.threading.Thread'):
            q.start()
        original = 'Codex turn failed: Selected model is at capacity. Please try a different model.'
        q.events.put(('status', 0, 'failed', original))
        q.events.put(('done',))
        q.poll()
        item = str(id(q.jobs[0]))
        self.assertEqual(q.tree.set(item, 'status'), '失敗')
        self.assertNotIn('error_code', q.tree['columns'])
        self.assertIn('滿載', q.tree.set(item, 'error_reason'))
        q.tree.selection_set(item)
        with mock.patch('translation_queue.messagebox.showinfo') as detail:
            q.show_details()
        self.assertNotIn('LLM_CAPACITY', detail.call_args.args[1])
        self.assertNotIn(original, detail.call_args.args[1])
        q.open_history()
        history = q.history_window
        batch = history.tree.get_children()[0]
        folder = history.tree.get_children(batch)[0]
        self.assertIn('滿載', history.tree.set(folder, 'error_reason'))
        history.tree.selection_set(folder)
        history.show_details()
        self.assertNotIn(original, detail_values(history))
        self.assertNotIn('LLM_CAPACITY', detail_values(history))
        # Existing persisted raw errors remain sufficient after restart.
        saved = comic.load_json(self.settings, {})
        self.assertEqual(saved['bt_jobs'][0]['error'], original)
        restored_root = tk.Toplevel(self.root)
        try:
            restored = comic.FileAggregatorApp(restored_root).translation_queue
            item = str(id(restored.jobs[0]))
            self.assertIn('滿載', restored.tree.set(item, 'error_reason'))
            restored.tree.selection_set(item)
            restored.retry()
            self.assertNotIn('error_code', restored.tree['columns'])
            self.assertEqual(restored.tree.set(item, 'error_reason'), '')
        finally:
            restored_root.destroy()
        history.refresh()
        folder = history.tree.get_children(history.tree.get_children()[0])[0]
        self.assertIn('滿載', history.tree.set(folder, 'error_reason'))

    def test_ntfy_queue_events_include_final_usage_and_deduplicate(self):
        q = self.app.translation_queue
        q.add_paths([self.chapter('Chapter 1'), self.chapter('Chapter 2')], 'translate')
        with mock.patch.object(q, 'validate', return_value=True), mock.patch('translation_queue.threading.Thread'):
            q.start()
        usage = dict(scope='total', total_tokens=1500, cost='0.011', requests=1, unpriced_requests=0, missing_usage_requests=0)
        with mock.patch.object(q.notifications, 'send') as send:
            for index, status, error in ((0, 'failed', 'Selected model is at capacity'), (1, 'done', '')):
                q.events.put(('usage', index, usage))
                q.events.put(('status', index, status, error))
                q.events.put(('job_time', index, '2026-09-15T01:00:00+08:00', '2026-09-15T01:00:15+08:00', 15))
                q.events.put(('job_time', index, '2026-09-15T01:00:00+08:00', '2026-09-15T01:00:15+08:00', 15))
            q.events.put(('run_time', '2026-09-15T01:00:00+08:00', '2026-09-15T01:00:30+08:00', 30))
            q.events.put(('done',))
            q.poll()
            q.notify_run()
            self.assertEqual(send.call_count, 3)
            failure, success, total = [call.args for call in send.call_args_list]
            self.assertEqual((failure[0], success[0], total[0]), ('failed', 'success', 'done'))
            self.assertNotIn('LLM_CAPACITY', failure[2])
            self.assertIn('Series / Chapter 1', failure[2])
            self.assertNotIn(str(self.folder), failure[2])
            self.assertIn('1.50K', failure[2])
            self.assertIn('US$0.02', failure[2])
            self.assertIn('完成 1｜異常 0｜失敗 1', total[2])
            self.assertIn('3.00K', total[2])
            self.assertIn('US$0.03', total[2])
            self.assertIn('00:00:30', total[2])
            self.assertFalse(q.running)
            self.assertFalse(q.stop.is_set())

    def test_ntfy_settings_defaults_encrypted_token_and_restart(self):
        n = self.app.translation_queue.notifications
        self.assertFalse(n.enabled.get())
        self.assertTrue(n.done.get())
        self.assertTrue(n.failed.get())
        self.assertFalse(n.success.get())
        with mock.patch.object(n, 'enqueue') as enqueue:
            n.send('done', 'title', 'body')
            enqueue.assert_not_called()
        n.enabled.set(True)
        n.topic.set('comic-private')
        n.token.set('tk_' + 'z' * 40)
        n.success.set(True)
        self.assertTrue(n.save())
        saved = comic.load_json(self.settings, {})
        self.assertNotIn('tk_' + 'z' * 40, self.settings.read_text(encoding='utf-8'))
        self.assertTrue(saved['ntfy_token_protected'])
        restored_root = tk.Toplevel(self.root)
        try:
            restored = comic.FileAggregatorApp(restored_root).translation_queue.notifications
            self.assertTrue(restored.enabled.get())
            self.assertTrue(restored.success.get())
            self.assertEqual(restored.token.get(), 'tk_' + 'z' * 40)
            with mock.patch.object(restored, 'enqueue') as enqueue:
                restored.send('failed', 'title', 'body', 4)
                self.assertEqual(enqueue.call_args.args[0]['token'], 'tk_' + 'z' * 40)
            restored.token.set('')
            restored.enabled.set(False)
            self.assertTrue(restored.save())
            self.assertEqual(comic.load_json(self.settings, {})['ntfy_token_protected'], '')
        finally:
            restored_root.destroy()

    def test_ntfy_delivery_runs_in_background_and_failure_does_not_stop_queue(self):
        n = self.app.translation_queue.notifications
        entered, release = threading.Event(), threading.Event()

        def delayed_publish(*_args):
            entered.set()
            release.wait(3)
            return 'NTFY_CLOUDFLARE_1010'

        with mock.patch('ntfy_notifications.publish', side_effect=delayed_publish):
            n.test()  # Explicit test works even while automatic notifications are off.
            self.assertTrue(entered.wait(2))
            self.assertEqual(str(n.test_button['state']), 'disabled')
            self.root.update()
            self.assertEqual(n.pending, 1)
            release.set()
            deadline = time.monotonic() + 3
            while n.pending and time.monotonic() < deadline:
                self.root.update()
                time.sleep(.01)
            self.assertEqual(n.pending, 0)
            self.assertIn('NTFY_CLOUDFLARE_1010', n.status.get())
            self.assertIn('Cloudflare 阻擋用戶端', n.status.get())
            self.assertFalse(self.app.translation_queue.stop.is_set())
            self.assertEqual(str(n.test_button['state']), 'normal')

    def test_merge_enqueues_without_starting_or_requiring_translation_settings(self):
        first, second = self.chapter("Chapter 1"), self.chapter("Chapter 2")
        for chapter, content in ((first, b"first"), (second, b"second")):
            (chapter / "1.png").write_bytes(content)
        base = self.folder / "Comics"
        self.app.base_path.set(str(base))
        self.app.apply_scan_data(base, self.app.scan_folder_data(base))
        self.app.folder_tree.selection_set(self.app.folder_tree.get_children()[0])
        self.app.on_tree_select()
        q = self.app.translation_queue
        q.add_paths([first], "translate")
        q.export.set(True)
        q.cleanup.set(True)
        self.assertEqual((q.installation.get(), self.app.komga_path.get()), ("", ""))
        with mock.patch.object(comic.messagebox, "askyesno", return_value=True) as confirm, \
                mock.patch.object(comic.messagebox, "showerror") as error, \
                mock.patch.object(q, "validate", return_value=False) as validate, \
                mock.patch.object(q, "confirm_cleanup", return_value=False) as cleanup, \
                mock.patch.object(q, "start") as start:
            q.integrate()
            deadline = time.monotonic() + 5
            while self.app.manga_busy and time.monotonic() < deadline:
                self.root.update()
                time.sleep(.01)
            self.assertFalse(self.app.manga_busy)
            confirm.assert_called_once()
            error.assert_not_called()
            validate.assert_not_called()
            cleanup.assert_not_called()
            start.assert_not_called()
        output = first.parent / "Chapter 1-2"
        self.assertEqual([(p.name, p.read_bytes()) for p in sorted(output.iterdir())],
                         [("1.png", b"first"), ("2.png", b"second")])
        self.assertEqual([(j.path, j.status) for j in q.jobs], [(first, "pending"), (output, "pending")])
        self.assertEqual([(j["path"], j["status"]) for j in comic.load_json(self.settings, {})["bt_jobs"]],
                         [(str(first), "pending"), (str(output), "pending")])
        self.assertFalse(q.running)
        self.assertIsNone(q.history_run)
        self.assertFalse(self.app.history_path.exists())
        self.assertEqual(self.app.work_tabs.select(), str(self.app.queue_tab))
        self.assertIn(output, [Path(item[0]) for item in self.app.selected_tree_item()[2]])
        with mock.patch.object(q, "validate", return_value=True) as validate, \
                mock.patch.object(q, "confirm_cleanup", return_value=False) as cleanup, \
                mock.patch("translation_queue.threading.Thread") as worker:
            q.start()
            validate.assert_called_once_with()
            cleanup.assert_called_once_with()
            worker.assert_not_called()

    def test_sort_keeps_manual_merge_range_in_confirmation(self):
        from ui_language import tr
        for number in range(1, 5):
            self.chapter(f"Chapter {number}")
        base = self.folder / "Comics"
        self.app.apply_scan_data(base, self.app.scan_folder_data(base))
        self.app.folder_tree.selection_set(self.app.folder_tree.get_children()[0])
        self.root.update()
        for entry, number in ((self.app.start_entry, "2"), (self.app.end_entry, "3")):
            entry.delete(0, "end")
            entry.insert(0, number)
        self.root.tk.call(self.app.folder_tree.heading("#0", "command"))
        self.root.update()
        self.assertEqual((self.app.start_entry.get(), self.app.end_entry.get()), ("2", "3"))
        with mock.patch.object(comic.messagebox, "askyesno", return_value=False) as confirm:
            self.app.confirm_aggregate()
        message = confirm.call_args.args[1]
        for number in (2, 3):
            self.assertIn(f"Chapter {number}", message)
        for number in (1, 4):
            self.assertNotIn(f"Chapter {number}", message)

    def test_merge_confirmation_skips_merged_within_original_range_and_cleanup(self):
        folders = [self.chapter(name) for name in ('Chapter 1', 'Chapter 1-4', 'Chapter 2', 'Chapter 3', 'Chapter 9')]
        for folder in folders:
            (folder / '1.png').write_bytes(folder.name.encode())
        base = self.folder / 'Comics'
        self.app.apply_scan_data(base, self.app.scan_folder_data(base))
        self.app.folder_tree.selection_set(self.app.folder_tree.get_children()[0])
        self.app.on_tree_select()
        for entry, value in ((self.app.start_entry, '2'), (self.app.end_entry, '4')):
            entry.delete(0, 'end')
            entry.insert(0, value)
        self.app.remove_sources_after_aggregate.set(True)
        self.app.keep_last_source.set(False)
        for enqueue in (False, True):
            with mock.patch.object(comic.messagebox, 'askyesno', return_value=True) as confirm, \
                    mock.patch.object(comic.threading, 'Thread') as worker:
                self.app.confirm_aggregate(translate_after=enqueue)
                args = worker.call_args.kwargs['args']
                self.assertEqual([item[1] for item in args[0]], ['Chapter 2', 'Chapter 3'])
                self.assertEqual(args[1:], (0, 1, True, False))
                self.assertIn('已略過 1 個整合資料夾', confirm.call_args.args[1])
                self.assertEqual(self.app.translate_after, enqueue)
            self.app.set_manga_busy(False)
        self.app.aggregate_worker(*args)
        output = folders[0].parent / 'Chapter 2-3'
        self.assertEqual([(output / f'{i}.png').read_bytes() for i in (1, 2)], [b'Chapter 2', b'Chapter 3'])
        self.assertTrue((folders[1] / 'result/1.png').exists())
        self.assertTrue(folders[0].exists())
        self.assertTrue(folders[4].exists())
        self.assertFalse(folders[2].exists())
        self.assertFalse(folders[3].exists())
        self.app.select_all_chapters(folders[0].parent, base, 'merged')
        with mock.patch.object(comic.messagebox, 'showinfo') as info, \
                mock.patch.object(comic.messagebox, 'askyesno') as confirm, \
                mock.patch.object(comic.threading, 'Thread') as worker:
            self.app.confirm_aggregate(translate_after=True)
            self.assertIn('沒有可整合的單一章節', info.call_args.args[1])
            confirm.assert_not_called()
            worker.assert_not_called()
            self.assertFalse(self.app.manga_busy)

    def test_keep_last_source_is_independent_persisted_and_used_in_confirmation(self):
        self.assertFalse(self.app.remove_sources_after_aggregate.get())
        self.assertTrue(self.app.keep_last_source.get())
        self.app.keep_last_source_checkbox.invoke()
        self.assertFalse(self.app.remove_sources_after_aggregate.get())
        self.app.remove_sources_checkbox.invoke()
        saved = comic.load_json(self.settings, {})
        self.assertIs(saved['remove_sources_after_aggregate'], True)
        self.assertIs(saved['keep_last_source'], False)
        for number in (1, 2):
            self.chapter(f"Chapter {number}")
        base = self.folder / "Comics"
        self.app.apply_scan_data(base, self.app.scan_folder_data(base))
        self.app.folder_tree.selection_set(self.app.folder_tree.get_children()[0])
        self.root.update()
        with mock.patch.object(comic.messagebox, "askyesno", return_value=True) as confirm, \
                mock.patch.object(comic.threading, "Thread") as thread:
            self.app.confirm_aggregate()
        self.assertIn("所有來源資料夾", confirm.call_args.args[1])
        self.assertIn("整合輸出除外", confirm.call_args.args[1])
        self.assertEqual(thread.call_args.kwargs['args'][-2:], (True, False))
        self.assertTrue(self.app.keep_last_source_checkbox.instate(['disabled']))
        self.app.set_manga_busy(False)
        self.app.keep_last_source.set(True)
        with mock.patch.object(comic.messagebox, "askyesno", return_value=False) as confirm:
            self.app.confirm_aggregate()
        self.assertIn("保留：Chapter 2", confirm.call_args.args[1])
        for handle in self.root.tk.call("after", "info"):
            self.root.tk.call("after", "cancel", handle)
        self.root.destroy()
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = comic.FileAggregatorApp(self.root)
        self.assertTrue(self.app.remove_sources_after_aggregate.get())
        self.assertFalse(self.app.keep_last_source.get())

    def test_series_sort_settings_preserve_chapters_selection_and_survive_restart(self):
        from ui_language import tr
        base = self.folder / "Comics"
        for name, count in (("Series 2", 1), ("Series 10", 3), ("Other", 2)):
            for number in (2, 10, 11)[:count]:
                result = base / name / f"Chapter {number}" / "result"
                result.mkdir(parents=True)
                (result / "1.png").write_bytes(b"image")
        data = self.app.scan_folder_data(base)
        for path in data[2]:
            data[2][path], data[3][path] = {
                "Series 2": (200, 3000), "Series 10": (100, 30), "Other": (300, 200)
            }[path.parent.name]
        self.app.apply_scan_data(base, data)
        tree = self.app.folder_tree
        parent = next(item for item in tree.get_children() if self.app.tree_items[item][1].name == "Series 10")
        tree.item(parent, open=True)
        tree.selection_set(tree.get_children(parent)[1])
        self.root.update()
        selected = base / "Series 10/Chapter 10"

        def displayed_names():
            return [self.app.tree_items[item][1].name for item in self.app.folder_tree.get_children()]

        expected = {"updated": ["Series 10", "Series 2", "Other"],
                    "name": ["Other", "Series 2", "Series 10"],
                    "chapters": ["Series 2", "Other", "Series 10"],
                    "size": ["Series 10", "Other", "Series 2"]}
        self.assertEqual(displayed_names(), list(reversed(expected["updated"])))
        for key, names in expected.items():
            for descending in (False, True):
                with self.subTest(field=key, descending=descending):
                    column = {"name": "#0", "chapters": "status"}.get(key, key)
                    self.root.tk.call(tree.heading(column, "command"))
                    self.assertEqual(displayed_names(), list(reversed(names)) if descending else names)
                    self.assertEqual(self.app.selected_chapters(), [selected])
                    parent = tree.parent(tree.selection()[0])
                    self.assertTrue(tree.item(parent, "open"))
                    self.assertEqual([tree.item(item, 'text') for item in tree.get_children(parent)],
                                     ["1. Chapter 2", "2. Chapter 10", "3. Chapter 11"])
                    self.assertEqual((self.app.start_entry.get(), self.app.end_entry.get()), ("2", "2"))
                    column = {"name": "#0", "chapters": "status"}.get(key, key)
                    self.assertTrue(tree.heading(column, 'text').endswith('↓' if descending else '↑'))
                    saved = comic.load_json(self.settings, {})
                    self.assertEqual((saved['series_sort'], saved['series_sort_descending']), (key, descending))

        with mock.patch.object(self.app, "save_settings", return_value=False):
            self.root.tk.call(tree.heading("#0", "command"))
        self.assertEqual(self.app.series_sort, "size")
        self.app.search_text.set("Series")
        self.root.after_cancel(self.app.search_after)
        self.app.apply_filter()
        self.assertEqual(displayed_names(), ["Series 2", "Series 10"])
        self.root.update_idletasks()
        self.root.destroy()
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = comic.FileAggregatorApp(self.root)
        self.app.apply_scan_data(base, data)
        self.assertEqual(displayed_names(), ["Series 2", "Other", "Series 10"])

    def test_queue_right_click_remove_confirms_target_and_preserves_files(self):
        first, second, third = [self.chapter(f"Chapter {n}") for n in (1, 2, 3)]
        q = self.app.translation_queue
        q.add_paths([first, second, third], "translate")
        self.root.geometry('1024x960')
        self.root.deiconify()
        self.root.update()
        items = tuple(str(id(job)) for job in q.jobs)
        q.tree.selection_set(items[0])

        def right_click(item):
            q.tree.see(item)
            self.root.update()
            x, y, _, height = q.tree.bbox(item)
            q.tree.event_generate('<Button-3>', x=x + 50, y=y + height // 2)

        with mock.patch.object(q.context_menu, 'tk_popup') as popup:
            right_click(items[1])
            popup.assert_called_once()
        self.assertEqual(q.tree.selection(), (items[1],))
        before = self.settings.read_bytes()
        with mock.patch('translation_queue.messagebox.askyesno', return_value=False) as confirm:
            q.context_menu.invoke(0)
            confirm.assert_called_once()
            self.assertIn('1', confirm.call_args.args[1])
        self.assertEqual([j.path for j in q.jobs], [first, second, third])
        self.assertEqual(self.settings.read_bytes(), before)
        with mock.patch('translation_queue.messagebox.askyesno', return_value=True):
            q.context_menu.invoke(0)
        self.assertEqual([j.path for j in q.jobs], [first, third])
        self.assertEqual([r['path'] for r in comic.load_json(self.settings, {})['bt_jobs']], [str(first), str(third)])
        self.root.update()
        q.tree.selection_set(*(str(id(job)) for job in q.jobs))
        with mock.patch.object(q.context_menu, 'tk_popup') as popup:
            q.tree.event_generate('<Button-3>', x=50, y=5)
            q.tree.event_generate('<Button-3>', x=50, y=q.tree.winfo_height() - 4)
            popup.assert_not_called()
        for running, busy in ((True, False), (False, True)):
            q.running, self.app.manga_busy = running, busy
            with mock.patch.object(q.context_menu, 'tk_popup'), \
                    mock.patch('translation_queue.messagebox.askyesno') as confirm:
                right_click(items[0])
                self.assertEqual(q.context_menu.entrycget(0, 'state'), 'disabled')
                q.context_menu.invoke(0)
                q.remove(confirm=True)
                confirm.assert_not_called()
            self.assertEqual(len(q.jobs), 2)
        q.running = self.app.manga_busy = False
        with mock.patch.object(q.context_menu, 'tk_popup'):
            right_click(items[0])
        self.assertEqual(set(q.tree.selection()), {items[0], items[2]})
        with mock.patch('translation_queue.messagebox.askyesno', return_value=True) as confirm:
            q.context_menu.invoke(0)
            self.assertIn('2', confirm.call_args.args[1])
        self.assertEqual(q.jobs, [])
        self.assertEqual(comic.load_json(self.settings, {})['bt_jobs'], [])
        for path in (first, second, third):
            self.assertEqual((path / 'result/1.png').read_bytes(), b'image')

    def test_manga_right_click_deletes_only_confirmed_folder_and_updates_queue(self):
        trash = self.folder / "Recycle Bin"
        trash.mkdir()
        recycle = mock.patch("windows_recycle.recycle_folder", side_effect=lambda path: path.rename(trash / path.name))
        recycle.start()
        self.addCleanup(recycle.stop)
        first, second, third = [self.chapter(f"Chapter {n}") for n in (1, 2, 3)]
        base = self.folder / "Comics"
        other = base / "Other" / "Chapter 1"
        (other / "result").mkdir(parents=True)
        (other / "result/1.png").write_bytes(b"keep")
        self.app.base_path.set(str(base))
        self.app.apply_scan_data(base, self.app.scan_folder_data(base))
        q, tree = self.app.translation_queue, self.app.folder_tree
        q.add_paths([first, second, third, other], "translate")
        self.root.geometry("1024x960")
        self.root.deiconify()
        self.root.update()

        def right_click(path):
            item = next(key for key, (_, value) in self.app.tree_items.items() if value == path)
            tree.see(item)
            self.root.update()
            x, y, _, height = tree.bbox(item)
            with mock.patch.object(self.app.folder_context_menu, "tk_popup") as popup:
                tree.event_generate("<Button-3>", x=x + 50, y=y + height // 2)
                popup.assert_called_once()
            self.assertEqual(tree.selection(), (item,))

        def wait_for_idle():
            deadline = time.monotonic() + 5
            while self.app.manga_busy and time.monotonic() < deadline:
                self.root.update()
                time.sleep(.01)
            self.assertFalse(self.app.manga_busy)

        tree.selection_set(*self.app.tree_items)
        right_click(second)
        with mock.patch.object(comic.messagebox, "askyesno", return_value=False) as confirm:
            self.app.folder_context_menu.invoke(0)
            self.assertIn(str(second), confirm.call_args.args[1])
            self.assertIn("將移至資源回收筒", confirm.call_args.args[1])
            self.assertNotIn("永久刪除", confirm.call_args.args[1])
            self.assertEqual(confirm.call_args.kwargs["default"], comic.messagebox.NO)
        self.assertTrue(second.exists())
        self.assertEqual(len(q.jobs), 4)
        for running, busy in ((True, False), (False, True)):
            q.running, self.app.manga_busy = running, busy
            right_click(second)
            with mock.patch.object(comic.messagebox, "askyesno") as confirm:
                self.assertEqual(self.app.folder_context_menu.entrycget(0, "state"), "disabled")
                self.app.folder_context_menu.invoke(0)
                self.app.confirm_delete_folder("chapter", second, base)
                confirm.assert_not_called()
        q.running = self.app.manga_busy = False
        right_click(second)
        with mock.patch.object(comic.messagebox, "askyesno", return_value=True), \
                mock.patch.object(comic, "delete_manga_folder", side_effect=PermissionError("locked")), \
                mock.patch.object(comic.messagebox, "showerror") as error:
            self.app.folder_context_menu.invoke(0)
            wait_for_idle()
            self.assertIn("locked", error.call_args.args[1])
        self.assertTrue(second.exists())
        self.assertEqual(len(q.jobs), 4)
        right_click(second)
        # Changing selection after opening the menu must not change the target.
        tree.selection_set(next(key for key, (_, value) in self.app.tree_items.items() if value == first))
        with mock.patch.object(comic.messagebox, "askyesno", return_value=True), \
                mock.patch.object(comic.messagebox, "showerror") as error:
            self.app.folder_context_menu.invoke(0)
            wait_for_idle()
            error.assert_not_called()
        self.assertFalse(second.exists())
        self.assertEqual((trash / "Chapter 2/result/1.png").read_bytes(), b"image")
        self.assertEqual([job.path for job in q.jobs], [first, third, other])
        self.assertNotIn(second, [path for _, path in self.app.tree_items.values()])

        self.app.search_text.set("Chapter 1")
        self.root.after_cancel(self.app.search_after)
        self.app.apply_filter()
        self.assertNotIn(third, [path for _, path in self.app.tree_items.values()])
        right_click(first.parent)
        with mock.patch.object(comic.messagebox, "askyesno", return_value=True) as confirm:
            self.app.folder_context_menu.invoke(0)
            self.assertIn("包含搜尋未顯示的章節", confirm.call_args.args[1])
            wait_for_idle()
        self.assertFalse(first.parent.exists())
        self.assertEqual((trash / "Series/Chapter 3/result/1.png").read_bytes(), b"image")
        self.assertEqual((other / "result/1.png").read_bytes(), b"keep")
        self.assertEqual([job.path for job in q.jobs], [other])
        self.assertEqual([row["path"] for row in comic.load_json(self.settings, {})["bt_jobs"]], [str(other)])
        with mock.patch.object(self.app.folder_context_menu, "tk_popup") as popup:
            tree.event_generate("<Button-3>", x=50, y=5)
            tree.event_generate("<Button-3>", x=50, y=tree.winfo_height() - 4)
            popup.assert_not_called()
        self.app.apply_scan_data(other.parent, self.app.scan_folder_data(other.parent))
        right_click(other.parent)
        self.assertEqual(self.app.folder_context_menu.entrycget(0, "state"), "disabled")

    def double_click(self, x, y, timestamp):
        tree = self.app.folder_tree
        for event, offset in (("<ButtonPress-1>", 0), ("<ButtonRelease-1>", 10),
                              ("<ButtonPress-1>", 50), ("<ButtonRelease-1>", 60)):
            tree.event_generate(event, x=x, y=y, time=timestamp + offset)
        self.root.update()

    def test_double_click_adds_only_clicked_chapter_with_selected_action_and_deduplicates(self):
        from comic_core import load_json
        from ui_language import tr
        first, second = self.chapter("Chapter 1"), self.chapter("Chapter 2")
        base = self.folder / "Comics"
        self.app.apply_scan_data(base, self.app.scan_folder_data(base))
        tree, q = self.app.folder_tree, self.app.translation_queue
        parent = tree.get_children()[0]
        tree.item(parent, open=True)
        children = tree.get_children(parent)
        tree.selection_set(*children)
        self.app.work_tabs.select(self.app.export_tab)
        self.root.deiconify()
        self.root.update()
        q.action.set(tr("匯出"))
        x, y, _, height = tree.bbox(children[1])
        self.double_click(x + 80, y + height // 2, 10000)
        self.assertEqual([(j.path, j.action) for j in q.jobs], [(second, "export")])
        self.assertEqual(self.app.work_tabs.select(), str(self.app.queue_tab))
        self.double_click(x + 80, y + height // 2, 11000)
        self.assertEqual(len(q.jobs), 1)
        saved = load_json(self.settings, {})["bt_jobs"]
        self.assertEqual([(r["path"], r["action"]) for r in saved], [(str(second), "export")])
        q.action.set(tr("翻譯"))
        x, y, _, height = tree.bbox(children[0])
        self.double_click(x + 80, y + height // 2, 12000)
        self.assertEqual([(j.path, j.action) for j in q.jobs], [(second, "export"), (first, "translate")])

    def test_double_click_series_heading_empty_space_and_busy_chapter_do_not_add(self):
        chapter = self.chapter("Chapter 1")
        base = self.folder / "Comics"
        self.app.apply_scan_data(base, self.app.scan_folder_data(base))
        tree = self.app.folder_tree
        parent = tree.get_children()[0]
        tree.item(parent, open=True)
        child = tree.get_children(parent)[0]
        self.root.deiconify()
        self.root.update()
        x, y, _, height = tree.bbox(parent)
        self.double_click(x + 80, y + height // 2, 20000)
        self.assertFalse(tree.item(parent, "open"))
        self.double_click(100, 5, 21000)
        self.double_click(100, tree.winfo_height() - 3, 22000)
        tree.item(parent, open=True)
        self.root.update()
        self.app.set_manga_busy(True)
        x, y, _, height = tree.bbox(child)
        self.double_click(x + 80, y + height // 2, 23000)
        self.assertEqual(self.app.translation_queue.jobs, [])

    def test_cancelled_predecessor_cannot_be_bypassed(self):
        chapter = self.chapter("Chapter 1")
        q = self.app.translation_queue
        q.jobs = [Job(chapter, "export", "cancelled"), Job(chapter, "cleanup")]
        with mock.patch.object(comic.messagebox, "showerror") as error:
            self.assertTrue(q.validate())
        error.assert_not_called()
        with mock.patch("translation_queue.threading.Thread") as worker, mock.patch.object(q, "confirm_cleanup", return_value=True):
            q.start()
        self.assertEqual(worker.call_args.kwargs["kwargs"]["blocked_jobs"], {0})
        q.running = False
        self.app.set_manga_busy(False)

    def test_page_range_edits_only_one_job_and_survives_restart(self):
        from comic_core import load_json
        from ui_language import tr
        first, second = self.chapter("Chapter 1"), self.chapter("Chapter 2")
        for chapter in (first, second):
            for index in range(1, 6):
                (chapter / f"{index}.png").touch()
        q = self.app.translation_queue
        q.add_paths([first, second], "translate")
        self.wait_counts(q)
        self.assertIn("待翻譯檔案 10 張", q.summary.get())
        q.jobs[0].status = "done"
        q.tree.selection_set(str(id(q.jobs[0])))
        dialog = q.edit_page_range()
        box = dialog.winfo_children()[0]
        first_input, last_input = (box.grid_slaves(row=row, column=1)[0] for row in (2, 3))
        self.assertEqual((first_input.get(), last_input.get()), ("1", ""))
        export_check = box.grid_slaves(row=4)[0]
        self.assertFalse(export_check.instate(["selected"]))
        self.assertTrue(export_check.instate(["disabled"]))
        first_input.set("2")
        last_input.set("4")
        self.assertFalse(export_check.instate(["disabled"]))
        export_check.invoke()
        buttons = box.grid_slaves(row=6)[0].winfo_children()
        next(b for b in buttons if b["text"] == tr("套用")).invoke()
        self.assertEqual((q.jobs[0].start_page, q.jobs[0].end_page, q.jobs[0].status), (2, 4, "pending"))
        self.assertEqual((q.jobs[1].start_page, q.jobs[1].end_page), (1, None))
        self.wait_counts(q)
        self.assertIn("待翻譯檔案 8 張", q.summary.get())
        self.assertEqual([j.range_export for j in q.jobs], [True, False])
        self.assertIn("2", q.tree.set(str(id(q.jobs[0])), "pages"))
        self.assertEqual(load_json(self.settings, {})["bt_jobs"][0]["end_page"], 4)
        another = tk.Toplevel(self.root)
        restored = comic.FileAggregatorApp(another).translation_queue
        self.wait_counts(restored)
        self.assertIn("待翻譯檔案 8 張", restored.summary.get())
        self.assertEqual([(j.start_page, j.end_page) for j in restored.jobs], [(2, 4), (1, None)])
        self.assertEqual([j.range_export for j in restored.jobs], [True, False])
        another.destroy()
        q.jobs[0].status = "done"
        dialog = q.edit_page_range()
        box = dialog.winfo_children()[0]
        self.assertTrue(box.grid_slaves(row=4)[0].instate(["selected"]))
        box.grid_slaves(row=4)[0].invoke()
        buttons = box.grid_slaves(row=6)[0].winfo_children()
        next(b for b in buttons if b["text"] == tr("套用")).invoke()
        self.assertFalse(q.jobs[0].range_export)
        self.assertEqual(q.jobs[0].status, "pending")
        dialog = q.edit_page_range()
        buttons = dialog.winfo_children()[0].grid_slaves(row=6)[0].winfo_children()
        next(b for b in buttons if b["text"] == tr("全部頁面")).invoke()
        next(b for b in buttons if b["text"] == tr("套用")).invoke()
        self.assertEqual((q.jobs[0].start_page, q.jobs[0].end_page), (1, None))
        self.assertFalse(q.jobs[0].range_export)
        self.wait_counts(q)
        self.assertIn("待翻譯檔案 10 張", q.summary.get())

    def test_pending_file_total_tracks_jobs_retries_removal_and_rescan(self):
        first, second = self.chapter("Chapter 1"), self.chapter("Chapter 2")
        for chapter, names in ((first, ("1.png", "2.JPG", "3.webp")), (second, ("1.png", "2.jpeg"))):
            for name in names:
                (chapter / name).touch()
            (chapter / "notes.txt").touch()
        q = self.app.translation_queue
        q.add_paths([first, second], "translate")
        q.add_paths([first], "export")
        self.wait_counts(q)
        self.assertIn("待翻譯檔案 5 張", q.summary.get())
        q.active_jobs = tuple(q.jobs)
        q.events.put(("status", 0, "running", ""))
        with mock.patch("translation_queue.image_files", side_effect=AssertionError("status must not rescan")):
            q.poll()
        self.wait_counts(q)
        self.assertIn("待翻譯檔案 2 張", q.summary.get())
        q.events.put(("status", 0, "failed", "synthetic failure"))
        q.poll()
        q.tree.selection_set(str(id(q.jobs[0])))
        q.retry()
        self.wait_counts(q)
        self.assertIn("待翻譯檔案 5 張", q.summary.get())
        q.tree.selection_set(str(id(q.jobs[1])))
        q.remove()
        self.wait_counts(q)
        self.assertIn("待翻譯檔案 3 張", q.summary.get())
        q.add_paths([self.folder / "missing"], "translate")
        self.wait_counts(q)
        self.assertIn("待翻譯檔案 3 張（1 項無法計數）", q.summary.get())
        with mock.patch("translation_queue.image_files", side_effect=PermissionError("synthetic denied")):
            q.render(refresh_counts=True)
            self.wait_counts(q)
        self.assertIn("2 項無法計數", q.summary.get())
        (first / "4.png").touch()
        base = self.folder / "Comics"
        self.app.scan_events.put(("done", base, self.app.scan_folder_data(base)))
        self.app.poll_scan_events()
        self.wait_counts(q)
        self.assertIn("待翻譯檔案 4 張（1 項無法計數）", q.summary.get())

    def test_range_export_controls_output_validation_and_legacy_jobs_default_off(self):
        from comic_core import save_json
        chapter = self.chapter("Chapter 1")
        (chapter / "1.png").touch()
        save_json(self.settings, {"bt_export": True, "bt_jobs": [dict(
            path=str(chapter), action="translate", start_page=1, end_page=1)]})
        another = tk.Toplevel(self.root)
        q = comic.FileAggregatorApp(another).translation_queue
        self.assertFalse(q.jobs[0].range_export)
        with mock.patch.object(q, "command"), mock.patch("translation_queue.messagebox.showerror") as error:
            self.assertTrue(q.validate())  # Global export does not require an output for this range.
            q.app.komga_path.set(str(chapter))
            self.assertTrue(q.validate())
            error.assert_not_called()
            q.jobs[0].range_export = True
            self.assertTrue(q.validate())  # The worker rejects this job, without blocking other paths.
            q.export.set(False)
            q.app.komga_path.set("")
            self.assertFalse(q.validate())  # Per-job opt-in requires an output even when global export is off.
            q.app.komga_path.set(str(self.folder / "Komga"))
            self.assertTrue(q.validate())
            q.app.komga_path.set("")
            q.jobs.append(Job(chapter, "export"))
            q.jobs[0].range_export = False
            self.assertFalse(q.validate())  # A separate manual export keeps its own requirements.
        another.destroy()

    def test_page_range_rejects_multi_selection_nontranslation_busy_and_bad_values(self):
        from ui_language import tr
        chapter = self.chapter("Chapter 1")
        (chapter / "1.png").touch()
        q = self.app.translation_queue
        q.add_paths([chapter], "translate")
        q.add_paths([chapter], "export")
        selections = ((), tuple(str(id(j)) for j in q.jobs), (str(id(q.jobs[1])),))
        for selection in selections:
            q.tree.selection_set(*selection)
            q.update_controls()
            self.assertEqual(str(q.range_button["state"]), "disabled")
            self.assertIsNone(q.edit_page_range())
        q.tree.selection_set(str(id(q.jobs[0])))
        self.app.set_manga_busy(True)
        self.assertIsNone(q.edit_page_range())
        self.app.set_manga_busy(False)
        q.update_controls()
        self.assertEqual(str(q.range_button["state"]), "normal")
        dialog = q.edit_page_range()
        box = dialog.winfo_children()[0]
        box.grid_slaves(row=2, column=1)[0].set("0")
        buttons = box.grid_slaves(row=6)[0].winfo_children()
        with mock.patch("translation_queue.messagebox.showerror") as error:
            next(b for b in buttons if b["text"] == tr("套用")).invoke()
        error.assert_called_once()
        self.assertEqual(q.jobs[0].start_page, 1)
        self.assertTrue(dialog.winfo_exists())
        dialog.destroy()

    def test_translation_completion_opens_final_location_once(self):
        from queue_worker import run_jobs
        chapter = self.chapter("Chapter 1, 測試")
        (chapter / "1.png").write_bytes(b"source")
        q = self.app.translation_queue
        self.assertFalse(q.open_after_completion.get())
        for opening, exporting in ((False, False), (False, True), (True, False), (True, True)):
            q.open_after_completion.set(opening)
            with self.subTest(opening=opening, exporting=exporting), \
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
                if not opening:
                    startfile.assert_not_called()
                    explorer.assert_not_called()
                elif exporting:
                    archive = self.folder / "Komga" / "Series" / f"{chapter.name}.cbz"
                    self.assertTrue(archive.is_file())
                    explorer.assert_called_once_with(f'explorer.exe /select,"{archive}"')
                    startfile.assert_not_called()
                else:
                    startfile.assert_called_once_with(chapter / "result")
                    explorer.assert_not_called()
                q.start()  # Already completed jobs must not reopen their results.
                self.assertEqual(startfile.call_count + explorer.call_count, int(opening))

    def test_open_completed_folder_checkbox_is_saved_and_restored(self):
        self.assertFalse(self.app.translation_queue.open_after_completion.get())
        for expected in (True, False):
            q = self.app.translation_queue
            check = next(w for w in q.controls if isinstance(w, tk.ttk.Checkbutton)
                         and w.cget("text") == "完成後開啟資料夾")
            check.invoke()
            self.assertIs(comic.load_json(self.settings, {})["bt_open_after_completion"], expected)
            self.app.set_manga_busy(True)
            self.assertTrue(check.instate(["disabled"]))
            self.app.set_manga_busy(False)
            self.root.update_idletasks()
            for handle in self.root.tk.call("after", "info"):
                self.root.tk.call("after", "cancel", handle)
            self.root.destroy()
            self.root = tk.Tk()
            self.root.withdraw()
            self.app = comic.FileAggregatorApp(self.root)
            self.assertIs(self.app.translation_queue.open_after_completion.get(), expected)

    def test_manual_export_open_failure_releases_busy_state(self):
        self.app.set_manga_busy(True)
        self.app.open_after_export.set(True)
        counts = dict(created=1, updated=0, skipped=0, failed=0)
        self.app.events.put(('done', counts, [], str(self.folder), None))
        with mock.patch.object(comic.os, 'startfile', side_effect=OSError('Explorer unavailable')), \
                mock.patch.object(comic.messagebox, 'showinfo'), \
                mock.patch.object(comic.messagebox, 'showwarning') as warning:
            self.app.poll_events()
        self.assertFalse(self.app.manga_busy)
        warning.assert_called_once()
        self.assertIn(str(self.folder), warning.call_args.args[1])

    def test_result_open_failure_does_not_interrupt_queue_completion(self):
        q = self.app.translation_queue
        q.open_after_completion.set(True)
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

    def test_completed_anomaly_is_visible_persisted_and_can_be_retried(self):
        from comic_core import load_json
        from queue_worker import run_jobs
        from ui_language import tr
        chapter = self.chapter("Chapter 1")
        (chapter / "1.png").write_bytes(b"source")
        q = self.app.translation_queue
        q.add_paths([chapter], "translate")
        q.active_jobs = tuple(q.jobs)
        with mock.patch("queue_worker.translator_command", return_value=([], self.folder, None)), \
                mock.patch("queue_worker.run_translation", return_value="exit=3221225477"), \
                mock.patch("translation_queue.os.startfile"):
            run_jobs(q.active_jobs, dict(bt_path="installed", bt_config=""), "", True, q.stop, q.events.put)
            q.poll()
        self.assertEqual(q.jobs[0].status, "done_warning")
        self.assertEqual(q.tree.set(str(id(q.jobs[0])), "status"), tr("完成（有異常）"))
        self.assertIn("3221225477", q.jobs[0].error)
        self.assertEqual(load_json(self.settings, {})["bt_jobs"][0]["status"], "done_warning")
        q.tree.selection_set(str(id(q.jobs[0])))
        q.retry()
        self.assertEqual((q.jobs[0].status, q.jobs[0].error), ("pending", ""))

    def test_layout_fits_standard_desktop(self):
        self.root.geometry('1024x780')
        self.root.deiconify()
        self.app.work_tabs.select(self.app.queue_tab)
        self.root.update()
        self.root.update_idletasks()
        footer = self.app.translation_queue.usage_frame
        self.assertTrue(footer.winfo_ismapped())
        self.assertLessEqual(footer.winfo_rooty() - self.root.winfo_rooty() + footer.winfo_reqheight(), 780)
        self.assertEqual(len(self.app.work_tabs.tabs()), 4)
        n = self.app.translation_queue.notifications
        self.app.work_tabs.select(n.tab)
        self.root.update()
        self.assertTrue(n.test_button.winfo_ismapped())
        self.assertLessEqual(n.test_button.winfo_rooty() - self.root.winfo_rooty() + n.test_button.winfo_height(), 780)

    def test_window_resize_grip_expands_queue_and_keeps_minimum_size(self):
        # Keep both sizes inside the 1024x768 desktop used by Windows CI.
        self.root.geometry('820x640+10+10')
        self.root.deiconify()
        self.root.update()
        grip, tree = self.app.window_grip, self.app.translation_queue.tree
        self.assertEqual(self.root.resizable(), (1, 1))
        self.assertTrue(grip.winfo_ismapped())
        initial_width, initial_height = tree.winfo_width(), tree.winfo_height()
        x, y = grip.winfo_rootx() + 2, grip.winfo_rooty() + 2
        grip.event_generate('<ButtonPress-1>', x=2, y=2, rootx=x, rooty=y)
        grip.event_generate('<B1-Motion>', x=162, y=82, rootx=x + 160, rooty=y + 80)
        grip.event_generate('<ButtonRelease-1>', x=162, y=82, rootx=x + 160, rooty=y + 80)
        self.root.update()
        self.assertEqual((self.root.winfo_width(), self.root.winfo_height()), (980, 720))
        self.assertGreater(tree.winfo_width(), initial_width)
        self.assertGreater(tree.winfo_height(), initial_height)
        self.root.geometry('600x400')
        self.root.update()
        self.assertEqual((self.root.winfo_width(), self.root.winfo_height()), (820, 640))

    def test_expanded_queue_sections_borrow_manga_space_and_restore_it(self):
        self.root.geometry('980x900')
        self.root.deiconify()
        self.root.update()
        q, panes = self.app.translation_queue, self.app.panes
        manga = panes.nametowidget(panes.panes()[0])
        panes.sashpos(0, 450)
        self.root.update()
        original_sash = panes.sashpos(0)
        original_tree = q.tree.winfo_height()
        original_manga = manga.winfo_height()
        for _ in range(2):
            q.bt_toggle.invoke()
            self.root.update()
            self.assertLess(manga.winfo_height(), original_manga)
            self.assertAlmostEqual(q.tree.winfo_height(), original_tree, delta=1)
            progress_sash = panes.sashpos(0)
            q.usage_toggle.invoke()
            self.root.update()
            self.assertLess(panes.sashpos(0), progress_sash)
            self.assertAlmostEqual(q.tree.winfo_height(), original_tree, delta=1)
            # Close in a different order from opening; no cumulative sash drift.
            q.bt_toggle.invoke()
            self.root.update()
            self.assertAlmostEqual(q.tree.winfo_height(), original_tree, delta=1)
            q.usage_toggle.invoke()
            self.root.update()
            self.assertEqual(panes.sashpos(0), original_sash)
            self.assertAlmostEqual(q.tree.winfo_height(), original_tree, delta=1)
        # On a small desktop, the manga controls must remain accessible.
        self.root.geometry('980x720')
        self.root.update()
        panes.sashpos(0, 335)
        self.root.update()
        q.bt_toggle.invoke()
        q.usage_toggle.invoke()
        self.root.update()
        self.assertTrue(q.tree.winfo_ismapped())
        self.assertTrue(self.app.aggregate_button.winfo_ismapped())
        self.assertLessEqual(self.app.aggregate_button.winfo_rooty() + self.app.aggregate_button.winfo_reqheight(),
                             manga.winfo_rooty() + manga.winfo_height())
        q.usage_toggle.invoke()
        q.bt_toggle.invoke()
        self.root.update()
        self.assertEqual(panes.sashpos(0), 335)
        # A user's divider adjustment while expanded remains after collapsing.
        self.root.geometry('980x900')
        self.root.update()
        panes.sashpos(0, 450)
        self.root.update()
        q.bt_toggle.invoke()
        self.root.update()
        panes.sashpos(0, panes.sashpos(0) + 15)
        self.root.update()
        adjusted_tree = q.tree.winfo_height()
        q.bt_toggle.invoke()
        self.root.update()
        self.assertEqual(panes.sashpos(0), 465)
        self.assertAlmostEqual(q.tree.winfo_height(), adjusted_tree, delta=1)

    def test_queue_sections_collapse_independently_and_keep_receiving_updates(self):
        from queue_worker import parse_bt_usage
        from test_bt_usage import usage_line
        q = self.app.translation_queue
        self.root.geometry('980x720')
        self.root.deiconify()
        self.root.update()
        # Reserve queue space so expanded content is visible on small CI desktops.
        self.app.panes.sashpos(0, 120)
        self.root.update()
        progress_grid = q.bt_bars['OCR'].master
        usage_grid = q.usage_frame.winfo_children()[0]
        self.assertFalse(progress_grid.winfo_ismapped())
        self.assertFalse(usage_grid.winfo_ismapped())
        q.bt_toggle.invoke()
        q.usage_toggle.invoke()
        self.root.update()
        self.assertTrue(progress_grid.winfo_ismapped())
        self.assertTrue(usage_grid.winfo_ismapped())
        self.assertTrue(q.tree.winfo_ismapped())
        expanded_height = q.tree.winfo_height()
        self.app.set_manga_busy(True)
        q.running, q.started_at = True, 100
        self.assertFalse(q.bt_toggle.instate(['disabled']))
        self.assertFalse(q.usage_toggle.instate(['disabled']))
        q.bt_toggle.invoke()
        self.root.update()
        self.assertFalse(progress_grid.winfo_ismapped())
        self.assertTrue(usage_grid.winfo_ismapped())
        self.assertGreater(q.tree.winfo_height(), expanded_height)
        q.usage_toggle.invoke()
        self.root.update_idletasks()
        self.assertFalse(usage_grid.winfo_ismapped())
        self.assertTrue(q.bt_toggle.winfo_ismapped())
        self.assertTrue(q.usage_toggle.winfo_ismapped())
        for event in [('bt_progress', 'OCR', 50, 24, 48, '01:00'),
                      ('stage_time', 0, 'OCR', 60),
                      ('usage', 0, parse_bt_usage(usage_line())),
                      ('stage', '匯出', 10), ('done',)]:
            q.events.put(event)
        with mock.patch('translation_queue.time.monotonic', return_value=220):
            q.poll()
        q.bt_toggle.invoke()
        q.usage_toggle.invoke()
        self.root.update_idletasks()
        self.assertTrue(progress_grid.winfo_ismapped())
        self.assertTrue(usage_grid.winfo_ismapped())
        self.assertEqual(q.bt_bars['OCR']['value'], 50)
        self.assertIn('24/48', q.bt_labels['OCR'].get())
        self.assertIn('00:01:00', q.time_labels['OCR'].get())
        self.assertIn('1.27M', q.usage_labels['total'].get())
        self.assertIn('US$2.49', q.usage_labels['total'].get())
        self.assertIn('00:02:00', q.elapsed_label.get())
        self.assertTrue(q.stage.winfo_ismapped())

    def test_window_size_survives_close_and_sections_restart_collapsed(self):
        self.root.geometry('900x700')
        self.root.deiconify()
        self.root.update()
        q = self.app.translation_queue
        q.bt_toggle.invoke()
        q.usage_toggle.invoke()
        self.root.update()
        self.app.close()
        saved = comic.load_json(self.settings, {})
        self.assertEqual(saved['window_size'], [900, 700])
        self.assertIs(saved['window_maximized'], False)
        self.root = tk.Tk()
        self.app = comic.FileAggregatorApp(self.root)
        self.root.update()
        self.assertEqual((self.root.winfo_width(), self.root.winfo_height()), (900, 700))
        q = self.app.translation_queue
        self.assertFalse(q.bt_bars['OCR'].master.winfo_ismapped())
        self.assertFalse(q.usage_frame.winfo_children()[0].winfo_ismapped())

    @unittest.skipUnless(comic.os.name == 'nt', 'Windows maximized state')
    def test_maximized_window_keeps_normal_size_through_minimize_and_restart(self):
        self.root.geometry('900x700')
        self.root.deiconify()
        self.root.update()
        self.root.state('zoomed')
        self.root.update()
        self.root.iconify()
        self.root.update()
        self.app.close()
        saved = comic.load_json(self.settings, {})
        self.assertEqual(saved['window_size'], [900, 700])
        self.assertIs(saved['window_maximized'], True)
        self.root = tk.Tk()
        self.app = comic.FileAggregatorApp(self.root)
        self.root.update()
        self.assertEqual(self.root.state(), 'zoomed')
        self.root.state('normal')
        self.root.update()
        self.assertEqual((self.root.winfo_width(), self.root.winfo_height()), (900, 700))

    def test_usage_totals_do_not_double_count_scopes_or_repeated_summaries(self):
        from queue_worker import parse_bt_usage
        from test_bt_usage import usage_line
        q = self.app.translation_queue
        records = [parse_bt_usage(usage_line(scope, tokens, cost)) for scope, tokens, cost in
                   [('OCR ', 503029, '0.971684'), ('translation ', 771156, '1.511601'), ('', 1274185, '2.483284')]]
        for record in records + [records[-1]]:
            q.events.put(('usage', 0, record))
        q.poll()
        self.assertIn('503.03K', q.usage_labels['OCR'].get())
        self.assertIn('US$0.98', q.usage_labels['OCR'].get())
        self.assertIn('771.16K', q.usage_labels['translation'].get())
        self.assertIn('US$1.52', q.usage_labels['translation'].get())
        self.assertIn('1.27M', q.usage_labels['total'].get())
        self.assertIn('US$2.49', q.usage_labels['total'].get())
        q.events.put(('usage', 1, records[-1]))
        q.poll()
        self.assertIn('2.55M', q.usage_labels['total'].get())
        self.assertIn('US$4.97', q.usage_labels['total'].get())
        q.events.put(('usage', 1, parse_bt_usage(usage_line(cost='unavailable'))))
        q.poll()
        self.assertIn('US$2.49', q.usage_labels['total'].get())
        self.assertIn('僅含已知金額', q.usage_labels['total'].get())

    def test_history_grand_totals_stay_at_bottom_and_ignore_filters_and_selection(self):
        from queue_history import save_run
        usage = dict(total_tokens=1000, requests=1, cost='0.004', missing_usage_requests=0, unpriced_requests=0)
        for index in range(2):
            record = dict(id=f'run-{index}', started_at=f'2026-09-1{index + 3}', saved_at='2026-09-15',
                          status='done', elapsed_seconds=3600, jobs=[dict(
                              path=f'Chapter {index}', action='export', status='done', usage={'total': usage})])
            save_run(self.app.history_path, record)
        self.app.translation_queue.open_history()
        window = self.app.translation_queue.history_window
        self.root.update()
        expected = [window.totals.item(row, 'values') for row in window.totals.get_children()]
        self.assertEqual({key: var.get() for key, var in window.total_counts.items()}, {'runs': '2', 'jobs': '2', 'elapsed': '02:00:00'})
        self.assertEqual(window.totals.item('total', 'values'), ('合計', '2,000', 'US$0.01', '2', '完整', '完整'))
        window.tree.selection_set('run-0')
        window.show_details()
        self.assertEqual([window.totals.item(row, 'values') for row in window.totals.get_children()], expected)
        window.keyword.set('Chapter 0')
        window.start.set('2026-09-13')
        window.end.set('2026-09-13')
        window.refresh()
        self.assertEqual(len(window.tree.get_children()), 1)
        self.assertEqual([window.totals.item(row, 'values') for row in window.totals.get_children()], expected)
        window.keyword.set('no matching folder')
        window.refresh()
        self.assertEqual(window.tree.get_children(), ())
        self.assertEqual([window.totals.item(row, 'values') for row in window.totals.get_children()], expected)
        window.geometry('820x480')
        self.root.update()
        self.assertGreater(window.totals.winfo_rooty(), window.details.winfo_rooty())
        self.assertLessEqual(window.totals.winfo_rooty() + window.totals.winfo_height(),
                             window.winfo_rooty() + window.winfo_height())
        record.update(id='run-2')
        save_run(self.app.history_path, record)
        window.refresh()
        self.assertEqual(window.total_counts['runs'].get(), '3')
        self.assertEqual(window.totals.set('total', 'cost'), 'US$0.02')
        window.refresh()
        self.assertEqual(window.total_counts['runs'].get(), '3')

    def test_queue_totals_match_collapsed_history_and_folder_breakdown(self):
        from queue_history import find_runs
        from queue_worker import parse_bt_usage
        from test_bt_usage import usage_line
        first, second = self.chapter('Chapter 1'), self.chapter('Chapter 2')
        q = self.app.translation_queue
        q.add_paths([first, second], 'translate')
        with mock.patch.object(q, 'validate', return_value=True), mock.patch('translation_queue.threading.Thread'):
            q.start()
        for index, values in enumerate((
                [('OCR ', 1000, '0.3334'), ('translation ', 2000, '0.6676'), ('', 3000, '1.001')],
                [('OCR ', 2000, '0.5'), ('translation ', 3000, '1.501'), ('', 5000, '2.001')])):
            for scope, tokens, cost in values + [values[-1]]:
                q.events.put(('usage', index, parse_bt_usage(usage_line(scope, tokens, cost))))
            for event in [('stage_time', index, 'OCR', 5), ('stage_time', index, 'Translation', 10),
                          ('status', index, 'done', ''),
                          ('job_time', index, '2026-09-13T01:00:00+08:00', '2026-09-13T01:00:15+08:00', 15),
                          ('total', index + 1)]:
                q.events.put(event)
            q.poll()
        q.events.put(('run_time', '2026-09-13T01:00:00+08:00', '2026-09-13T01:00:30+08:00', 30))
        q.events.put(('done',))
        q.poll()
        self.assertIn('3.00K', q.usage_labels['OCR'].get())
        self.assertIn('5.00K', q.usage_labels['translation'].get())
        self.assertIn('8.00K', q.usage_labels['total'].get())
        self.assertIn('US$3.01', q.usage_labels['total'].get())  # Round after summing original costs.
        q.open_history()
        window = q.history_window
        self.root.update()
        batch = window.tree.get_children()[0]
        folders = window.tree.get_children(batch)
        self.assertEqual(len(folders), 2)
        self.assertFalse(window.tree.item(batch, 'open'))
        self.assertFalse(window.tree.bbox(folders[0]))
        self.assertEqual(window.tree.set(batch, 'tokens'), '8,000')
        self.assertNotIn('usage', window.tree['columns'])
        for scope in ('OCR', 'translation', 'total'):
            self.assertTrue(window.detail_tables['usage'].exists(scope))
        self.assertEqual(window.detail_tables['usage'].set('total', 'tokens'), '8,000')
        self.assertNotIn(str(first), detail_values(window))
        window.tree.item(batch, open=True)
        self.root.update_idletasks()
        for folder, tokens, cost in zip(folders, ('3,000', '5,000'), ('US$1.01', 'US$2.01')):
            self.assertFalse(window.tree.item(folder, 'open'))
            self.assertTrue(window.tree.bbox(folder))
            self.assertEqual(tokens, window.tree.set(folder, 'tokens'))
            self.assertIn(cost, window.tree.set(folder, 'cost'))
        scopes = window.tree.get_children(folders[0])
        self.assertEqual([window.tree.item(item, 'text') for item in scopes], ['OCR', '翻譯', '合計'])
        self.assertFalse(window.tree.bbox(scopes[0]))
        window.tree.item(folders[0], open=True)
        self.root.update_idletasks()
        self.assertTrue(window.tree.bbox(scopes[0]))
        self.assertEqual('1,000', window.tree.set(scopes[0], 'tokens'))
        self.assertEqual(window.tree.set(scopes[0], 'elapsed'), '00:00:05')
        window.tree.selection_set(scopes[0])
        window.show_details()
        self.assertIn(str(first), detail_values(window))
        self.assertNotIn(str(second), detail_values(window))
        self.assertIn('00:00:15', detail_values(window))
        window.refresh()
        self.assertFalse(window.tree.item(batch, 'open'))
        self.assertTrue(all(not window.tree.item(item, 'open') for item in window.tree.get_children(batch)))
        q.add_paths([self.chapter('Chapter 3')], 'translate')
        with mock.patch.object(q, 'validate', return_value=True), mock.patch('translation_queue.threading.Thread'):
            q.start()
        self.assertEqual(q.usage_labels['total'].get(), '尚未回報')
        self.assertEqual(len(find_runs(self.app.history_path)), 2)
        window.refresh()
        self.assertEqual('8,000', window.tree.set(batch, 'tokens'))

    def test_history_records_checkpoints_usage_failures_and_survives_restart(self):
        from queue_history import find_runs
        from queue_worker import parse_bt_usage, run_jobs
        from test_bt_usage import usage_line
        first, second = self.chapter('Chapter 1'), self.chapter('Chapter 2')
        for chapter in (first, second):
            (chapter / '1.png').write_bytes(b'source')
        q = self.app.translation_queue
        q.add_paths([first, second], 'translate')
        with mock.patch.object(q, 'validate', return_value=True), mock.patch('translation_queue.threading.Thread'):
            q.start()
        checkpoints = []
        count = 0

        def translate(*_args, **kwargs):
            nonlocal count
            count += 1
            for scope in ('OCR ', 'translation ', ''):
                record = parse_bt_usage(usage_line(scope, cost='2.483284' if count == 1 else 'unavailable',
                                                   subtotal='0.125', unpriced=0 if count == 1 else 1))
                kwargs['usage'](record)
                kwargs['usage'](record)  # Repeated log summaries must not create duplicate charges.
            kwargs['timing']('OCR', 20)
            kwargs['timing']('Translation', 30)
            if count == 2:
                raise RuntimeError('translation failed after usage report')

        def emit(event):
            q.events.put(event)
            if event[0] == 'total':
                q.poll()
                checkpoints.append(find_runs(self.app.history_path)[0])

        with mock.patch('queue_worker.translator_command', return_value=([], self.folder, None)), \
                mock.patch('queue_worker.run_translation', side_effect=translate):
            run_jobs(q.active_jobs, q.settings(), '', True, q.stop, emit)
        q.poll()
        self.assertEqual(checkpoints[0]['status'], 'running')
        self.assertEqual(checkpoints[0]['jobs'][0]['status'], 'done')
        stored = find_runs(self.app.history_path)
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]['status'], 'done_warning')
        self.assertEqual([job['status'] for job in stored[0]['jobs']], ['done', 'failed'])
        self.assertIsNotNone(stored[0]['finished_at'])
        for job in stored[0]['jobs']:
            self.assertIsNotNone(job['started_at'])
            self.assertGreaterEqual(job['elapsed_seconds'], 0)
            self.assertEqual(job['stage_seconds']['OCR'], 20)
            self.assertEqual(job['usage']['total']['total_tokens'], 1274185)
        self.assertEqual(stored[0]['jobs'][0]['usage']['total']['cost'], '2.483284')
        self.assertEqual(stored[0]['jobs'][1]['usage']['total']['cost'], '0.125')
        self.assertIn('US$2.61', q.usage_labels['total'].get())
        q.clear_completed()
        self.assertEqual(len(find_runs(self.app.history_path)), 1)
        self.root.update_idletasks()
        for handle in self.root.tk.call('after', 'info'):
            self.root.tk.call('after', 'cancel', handle)
        self.root.destroy()
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = comic.FileAggregatorApp(self.root)
        q = self.app.translation_queue
        q.history_button.invoke()
        window = q.history_window
        self.assertEqual(len(window.tree.get_children()), 1)
        self.assertEqual('2,548,370', window.tree.set(window.tree.get_children()[0], 'tokens'))
        self.assertIn('US$2.61', detail_values(window))
        batch = window.tree.get_children()[0]
        self.assertFalse(window.tree.item(batch, 'open'))
        folder = window.tree.get_children(batch)[1]
        self.assertIn('US$2.61', window.tree.set(batch, 'cost'))
        self.assertIn('US$0.13', window.tree.set(folder, 'cost'))
        self.assertFalse(window.tree.item(folder, 'open'))
        window.tree.item(batch, open=True)
        window.tree.selection_set(folder)
        window.show_details()
        self.assertIn(str(second), detail_values(window))
        self.assertNotIn(str(first), detail_values(window))
        self.assertNotIn('translation failed after usage report', detail_values(window))
        self.assertIn('未能分類', detail_values(window))
        self.assertIn('OpenAI Standard API equivalent (not a bill)', detail_values(window))
        window.keyword.set('not in history')
        window.refresh()
        self.assertEqual(window.tree.get_children(), ())
        window.keyword.set('Chapter 2')
        window.refresh()
        self.assertEqual(len(window.tree.get_children()), 1)
        self.app.set_manga_busy(True)
        self.assertFalse(q.history_button.instate(['disabled']))
        self.app.set_manga_busy(False)

    def test_history_save_failure_prevents_start_and_preserves_failed_checkpoint_for_retry(self):
        import sqlite3
        from queue_history import find_runs
        chapter = self.chapter('Chapter 1')
        q = self.app.translation_queue
        q.add_paths([chapter], 'translate')
        with mock.patch.object(q, 'validate', return_value=True), \
                mock.patch('translation_queue.threading.Thread') as thread, \
                mock.patch('translation_queue.save_run', side_effect=sqlite3.OperationalError('read only')), \
                mock.patch('translation_queue.messagebox.showerror') as error:
            q.start()
            self.assertFalse(q.running)
            thread.assert_not_called()
            error.assert_called_once()
        with mock.patch.object(q, 'validate', return_value=True), mock.patch('translation_queue.threading.Thread'):
            q.start()
        for event in (('status', 0, 'done', ''), ('job_time', 0, '2026-09-13T01:00:00+08:00', '2026-09-13T01:00:05+08:00', 5),
                      ('total', 1), ('run_time', '2026-09-13T01:00:00+08:00', '2026-09-13T01:00:05+08:00', 5), ('done',)):
            q.events.put(event)
        with mock.patch('translation_queue.save_run', side_effect=sqlite3.OperationalError('disk full')), \
                mock.patch('translation_queue.messagebox.showerror') as error:
            q.poll()
        error.assert_called_once()
        self.assertTrue(q.stop.is_set())
        self.assertTrue(q.history_save_failed)
        self.assertFalse(q.running)
        q.jobs[0].status = 'pending'
        with mock.patch.object(q, 'validate', return_value=False):
            q.start()
        stored = find_runs(self.app.history_path)
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]['status'], 'cancelled')
        self.assertEqual(stored[0]['jobs'][0]['status'], 'done')
        self.assertEqual(stored[0]['elapsed_seconds'], 5)

    def test_pause_resume_keeps_one_batch_usage_and_excludes_waiting_time(self):
        from queue_history import find_runs
        q = self.app.translation_queue
        q.add_paths([self.chapter('Chapter 1'), self.chapter('Chapter 2')], 'cleanup')
        usage = dict(scope='total', total_tokens=1000, requests=1, cost='0.004',
                     missing_usage_requests=0, unpriced_requests=0)
        with mock.patch('translation_queue.threading.Thread') as worker, \
                mock.patch.object(q, 'notify_run') as notify, \
                mock.patch('translation_queue.time.monotonic', return_value=100) as clock:
            q.start(cleanup_confirmed=True)
            batch_id = q.history_run['id']
            q.events.put(('status', 0, 'running', ''))
            q.poll()
            q.pause_button.invoke()
            self.assertTrue(q.pause_requested.is_set())
            self.assertFalse(q.stop.is_set())
            self.assertFalse(q.start_button.instate(['disabled']))
            q.start_button.invoke()  # Cancel a pending pause without starting another worker.
            self.assertFalse(q.pause_requested.is_set())
            q.pause_button.invoke()
            for event in [('bt_progress', 'OCR', 100, 2, 2, '00:00'), ('stage_time', 0, 'OCR', 5),
                          ('usage', 0, usage), ('status', 0, 'done', ''),
                          ('job_time', 0, 'start', 'end', 5), ('total', 1), ('paused', 105)]:
                q.events.put(event)
            clock.return_value = 105
            q.poll()
            self.assertTrue(q.running)
            self.assertTrue(self.app.manga_busy)
            self.assertEqual([job.status for job in q.active_jobs], ['done', 'pending'])
            self.assertIn('佇列已暫停', q.label.get())
            self.assertFalse(q.stop_button.instate(['disabled']))
            self.assertTrue(q.range_button.instate(['disabled']))
            notify.assert_not_called()
            before = q.usage_labels['total'].get()
            clock.return_value = 1000
            q.poll()
            self.assertIn('00:00:05', q.elapsed_label.get())
            self.assertIn('平均：2.50 秒／頁', q.bt_labels['OCR'].get())
            stored = find_runs(self.app.history_path)
            self.assertEqual(len(stored), 1)
            self.assertEqual(stored[0]['status'], 'running')
            self.assertEqual(stored[0]['elapsed_seconds'], 5)
            q.start_button.invoke()
            self.assertFalse(q.pause_requested.is_set())
            self.assertEqual(q.history_run['id'], batch_id)
            self.assertEqual(q.usage_labels['total'].get(), before)
            self.assertEqual(worker.call_count, 1)
            q.events.put(('resumed', 895))
            q.poll()
            q.poll()
            self.assertIn('00:00:05', q.elapsed_label.get())
            for event in [('status', 1, 'running', ''), ('usage', 1, usage), ('status', 1, 'done', ''),
                          ('job_time', 1, 'resume', 'end', 5), ('total', 2),
                          ('run_time', '2026-09-15T00:00:00+08:00', '2026-09-15T00:15:05+08:00', 10), ('done',)]:
                q.events.put(event)
            clock.return_value = 1005
            q.poll()
            self.assertFalse(q.running)
            self.assertFalse(q.pause_requested.is_set())
            self.assertIsNone(q.paused_at)
            self.assertFalse(self.app.manga_busy)
            self.assertIn('2.00K', q.usage_labels['total'].get())
            self.assertIn('US$0.01', q.usage_labels['total'].get())
            self.assertIn('00:00:10', q.elapsed_label.get())
            self.assertEqual(q.total['value'], 2)
            notify.assert_called_once()
            stored = find_runs(self.app.history_path)
            self.assertEqual(len(stored), 1)
            self.assertEqual((stored[0]['id'], stored[0]['status'], stored[0]['elapsed_seconds']), (batch_id, 'done', 10))

    def test_elapsed_time_accumulates_per_job_and_freezes_on_completion(self):
        from translation_queue import elapsed_text
        self.assertEqual(elapsed_text(90061), '25:01:01')
        q = self.app.translation_queue
        q.running, q.started_at = True, 100
        for event in [('stage_time', 0, 'OCR', 120), ('stage_time', 0, 'OCR', 119),
                      ('stage_time', 1, 'OCR', 60), ('stage_time', 0, 'Translation', 30), ('done',)]:
            q.events.put(event)
        with mock.patch('translation_queue.time.monotonic', return_value=3761):
            q.poll()
        self.assertIn('01:01:01', q.elapsed_label.get())
        self.assertIn('00:03:00', q.time_labels['OCR'].get())
        self.assertIn('00:00:30', q.time_labels['Translation'].get())
        with mock.patch('translation_queue.time.monotonic', return_value=9000):
            q.poll()
        self.assertIn('01:01:01', q.elapsed_label.get())

    def test_four_stage_progress_remains_independent_and_resets_per_job(self):
        from queue_worker import BT_STAGES
        from ui_language import tr
        q = self.app.translation_queue
        q.jobs = [Job(self.folder / "Chapter 1", "translate"), Job(self.folder / "Chapter 2", "translate")]
        q.active_jobs = tuple(q.jobs)
        q.render()
        for event in (("status", 0, "running", ""), ("bt_reset", 100, dict.fromkeys(BT_STAGES, True)),
                      ("bt_progress", "Text Detection", 90, 90, 100, "00:10"),
                      ('stage_time', 0, 'Text Detection', 45),
                      ("bt_progress", "OCR", 60, 60, 100, "01:30"),
                      ('stage_time', 0, 'OCR', 150),
                      ("bt_progress", "Inpaint", 55, 55, 100, "02:00"),
                      ('stage_time', 0, 'Inpaint', 0),
                      ("bt_progress", "Translation", 40, 40, 100, "12:34"),
                      ('stage_time', 0, 'Translation', 7200)):
            q.events.put(event)
        q.poll()
        self.assertEqual([q.bt_bars[name]["value"] for name in BT_STAGES], [90, 60, 55, 40])
        self.assertIn("90/100", q.bt_labels["Text Detection"].get())
        self.assertIn("12:34", q.bt_labels["Translation"].get())
        for name, average in zip(BT_STAGES, ('0.50', '2.50', '<1.00', '180.00')):
            self.assertIn(f'平均：{average} 秒／頁', q.bt_labels[name].get())
        q.events.put(("status", 0, "cancelled", "stop"))
        q.poll()
        self.assertIn(tr("已停止"), q.bt_labels["Translation"].get())
        self.assertNotIn("12:34", q.bt_labels["Translation"].get())
        self.assertIn('平均：180.00 秒／頁', q.bt_labels['Translation'].get())
        q.events.put(("status", 1, "running", ""))
        q.events.put(("bt_reset", 20, {name: name != "OCR" for name in BT_STAGES}))
        q.poll()
        self.assertEqual(q.bt_labels["OCR"].get(), tr("未啟用"))
        self.assertIn("0/20", q.bt_labels["Translation"].get())
        self.assertEqual(q.bt_bars["Text Detection"]["value"], 0)
        self.assertIn('平均：— 秒／頁', q.bt_labels['Translation'].get())
        for event in [('bt_progress', 'Translation', 50, 10, 20, '00:30'),
                      ('stage_time', 1, 'Translation', 30)]:
            q.events.put(event)
        q.poll()
        self.assertIn('平均：3.00 秒／頁', q.bt_labels['Translation'].get())
        self.assertIn('02:00:30', q.time_labels['Translation'].get())  # Batch elapsed must not be the numerator.
        # A new report without valid timing must not reuse the previous report's seconds.
        q.events.put(('bt_progress', 'Translation', 75, 15, 20, None))
        q.poll()
        self.assertIn('平均：— 秒／頁', q.bt_labels['Translation'].get())
        for current, seconds, average in ((None, 30, '—'), (0, 30, '—'), (1000, 1, '<0.01')):
            q.events.put(('bt_progress', 'Translation', 0, current, 1000, None))
            q.events.put(('stage_time', 1, 'Translation', seconds))
            q.poll()
            self.assertIn(f'平均：{average} 秒／頁', q.bt_labels['Translation'].get())

    def test_export_progress_does_not_overwrite_translation_bars(self):
        q = self.app.translation_queue
        q.events.put(("bt_progress", "Translation", 100, 960, 960, "00:00"))
        q.events.put(("stage", "匯出", 50))
        q.poll()
        self.assertEqual(q.stage["value"], 50)
        self.assertEqual(q.stage.winfo_manager(), "pack")
        self.assertEqual(q.bt_bars["Translation"]["value"], 100)
        self.assertIn("960/960", q.bt_labels["Translation"].get())
