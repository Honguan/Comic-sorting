import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

from queue_history import HistoryWindow, save_run
from translation_queue import ACTIONS, STATUSES


class HistoryLoadingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'history.sqlite3'
        self.root = tk.Tk()
        self.root.withdraw()
        def close():
            self.root.update_idletasks()
            self.root.destroy()
        self.addCleanup(close)

    def save(self, identifier, size):
        save_run(self.path, dict(id=str(identifier), started_at='2026-10-01', status='done',
            jobs=[dict(path=f'Manga/Chapter {i}', action='translate', status='done',
                       usage={'total': dict(total_tokens=1000, requests=1, cost='0.01',
                                           unpriced_requests=0, missing_usage_requests=0)}) for i in range(size)]))

    def test_only_expanded_children_load_and_refresh_preserves_selection(self):
        self.save('large', 300)
        self.save('other', 1)
        original = self.path.read_bytes()
        window = HistoryWindow(self.root, self.path, ACTIONS, STATUSES)
        self.assertEqual(len(window.records), 2)
        self.assertEqual(len(window.tree.get_children('large')), 1)
        window.load_children('large')
        self.assertEqual(len(window.tree.get_children('large')), 300)
        job = window.tree.get_children('large')[0]
        window.load_children(job)
        self.assertEqual([window.tree.item(i, 'text') for i in window.tree.get_children(job)], ['OCR', '翻譯', '合計'])
        window.tree.item('large', open=True)
        window.tree.item(job, open=True)
        window.tree.selection_set(job)
        window.refresh()
        self.assertEqual(window.tree.selection(), (job,))
        self.assertTrue(window.tree.item('large', 'open'))
        self.assertTrue(window.tree.item(job, 'open'))
        self.assertEqual(window.tree.set(job, 'tokens'), '1.00K')
        self.assertEqual(self.path.read_bytes(), original)

    def test_pagination_and_bad_filter_preserve_page(self):
        for index in range(205):
            self.save(index, 0)
        window = HistoryWindow(self.root, self.path, ACTIONS, STATUSES)
        self.assertEqual(len(window.tree.get_children()), 200)
        window.next.invoke()
        self.assertEqual(window.tree.get_children(), ('4', '3', '2', '1', '0'))
        self.assertEqual(str(window.next['state']), 'disabled')
        window.start.set('bad')
        with patch('queue_history.messagebox.showerror') as error:
            self.assertFalse(window.refresh())
        error.assert_called_once()
        self.assertEqual(window.offset, 200)
        self.assertEqual(window.tree.get_children(), ('4', '3', '2', '1', '0'))
        window.start.set('')
        window.previous.invoke()
        self.assertEqual(len(window.tree.get_children()), 200)
        self.assertEqual(window.tree.get_children()[0], '204')
