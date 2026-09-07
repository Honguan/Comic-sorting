import queue
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from queue_worker import Job, run_jobs, run_translation, translator_command
from test_comic_sorting import comic


class TranslationQueueTests(unittest.TestCase):
    def test_installed_runtime_and_unicode_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            (root / "ballontranslator").mkdir()
            (root / "ballontranslator/__main__.py").touch()
            (root / "ballontrans_pylibs_win").mkdir()
            (root / "ballontrans_pylibs_win/python.exe").touch()
            config = root / "config.json"
            config.write_text("{}")
            command, cwd, env = translator_command(root, config, chapter=root / "漫畫 1")
            self.assertEqual(cwd, root.resolve())
            self.assertEqual(command[-1], str(root / "漫畫 1"))
            self.assertEqual(command[0], str(root / "ballontrans_pylibs_win/python.exe"))
            self.assertEqual(env["PYTHONIOENCODING"], "utf-8")
            with self.assertRaises(ValueError):
                translator_command(root, config, chapter=root / "a,b")

    def test_process_completion_progress_and_failure(self):
        progress = []
        run_translation([sys.executable, "-u", "-c",
                         "print('Translation: 100%'); print('finished translating all dirs'); input()"],
                        Path.cwd(), None, threading.Event(), lambda *p: progress.append(p))
        self.assertEqual(progress, [("Translation", 100)])
        with self.assertRaises(RuntimeError):
            run_translation([sys.executable, "-c", "input()"], Path.cwd(), None,
                            threading.Event(), lambda *p: None)

    def test_translation_failure_blocks_export_and_cleanup(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "1.png").touch()
            work = root / "mask"
            work.mkdir()
            (work / "keep.png").touch()
            app = comic.FileAggregatorApp.__new__(comic.FileAggregatorApp)
            jobs = [Job(root, "translate"), Job(root, "cleanup")]
            events_queue = queue.Queue()
            stop = threading.Event()
            settings = dict(bt_path="missing", bt_config="missing", bt_python="",
                            bt_export=True, bt_cleanup=True)
            run_jobs(jobs, settings, str(root / "output"), True, stop, events_queue.put)
            self.assertTrue((work / "keep.png").exists())
            self.assertFalse((root / "output").exists())
            events = list(events_queue.queue)
            self.assertEqual(sum(e[0] == "status" and e[2] in ("failed", "blocked") for e in events), 2)

    def test_gui_constructs_and_selection_deduplicates(self):
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        try:
            with mock.patch.object(comic, "load_json", return_value={}), mock.patch.object(comic.FileAggregatorApp, "save_settings", return_value=True):
                app = comic.FileAggregatorApp(root)
            with mock.patch.object(app, "save_settings", return_value=True):
                app.translation_queue.add_paths([Path.cwd(), Path.cwd()])
            self.assertEqual(len(app.translation_queue.jobs), 1)
            self.assertEqual(str(app.folder_tree.cget("selectmode")), "extended")
            root.update_idletasks()
        finally:
            root.destroy()
