from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from bt_run_bridge import install_page_selection
from comic_core import image_files, output_path_for
from queue_worker import Job, run_jobs


class PageRangeTests(unittest.TestCase):
    def continuous_native(self):
        package = Path(self.settings['bt_path']) / 'ballontranslator'
        launch = package / 'launch.py'
        source = launch.read_text(encoding='utf-8')
        source = source.replace('    MainWindow(args.exec_dirs[0]).on_run_imgtrans()',
                                '    MainWindow(args.exec_dirs[0]).run_batch(args.exec_dirs)')
        source = source.replace("    print('finished translating all dirs')\n    input()\n", '')
        launch.write_text(source, encoding='utf-8')
        main = package / 'ui/mainwindow.py'
        source = main.read_text(encoding='utf-8').replace('def on_run_imgtrans(', 'def translate_pages(')
        source += '''
    def on_run_imgtrans(self, pages_to_process=None):
        import os, time
        with (self.path.parent / 'pids.txt').open('a') as log:
            log.write(str(os.getpid()) + '\\n')
        if self.path.name == 'Fail':
            print('[INFO] module_manager:run:1 - Image translation pipeline stopped.', flush=True)
            time.sleep(30)
            return
        self.translate_pages(pages_to_process)
        print('[INFO] module_manager:_finish_llm_usage_run:1 - LLM run usage: requests=1, total_tokens=10, missing_usage_requests=0, unpriced_requests=0, estimated_cost_usd=0.01', flush=True)
        self.run_next_dir()
    def run_batch(self, paths):
        assert isinstance(paths, list)
        self.exec_dirs = list(paths)
        self.imsave_thread = SimpleNamespace(isRunning=lambda: False)
        self.module_manager = SimpleNamespace(proj_finished=lambda: True)
        self.app = SimpleNamespace(quit=lambda: None)
        self.run_next_dir()
    def run_next_dir(self):
        if self.exec_dirs:
            self.path = Path(self.exec_dirs.pop(0))
            self.imgtrans_proj = SimpleNamespace(pages={p.name: [] for p in self.path.glob('*.png')})
            self.on_run_imgtrans()
'''
        main.write_text(source, encoding='utf-8')

    def test_continuous_process_keeps_pages_usage_and_pause_boundaries(self):
        import subprocess
        self.continuous_native()
        other = self.chapter.parent / 'Chapter 2, Unicode 漫畫'
        other.mkdir()
        (other / '1.png').write_bytes(b'source')
        jobs = [Job(self.chapter, 'translate', start_page=2, end_page=3), Job(other, 'translate')]
        pause, paused, stop = threading.Event(), threading.Event(), threading.Event()
        events = []
        def emit(event):
            events.append(event)
            if event[:3] == ('status', 0, 'done'):
                pause.set()
            if event[0] == 'paused':
                paused.set()
        processes = []
        original = subprocess.Popen
        def spawn(*args, **kwargs):
            process = original(*args, **kwargs)
            processes.append(process)
            return process
        with mock.patch('queue_worker.subprocess.Popen', side_effect=spawn):
            worker = threading.Thread(target=run_jobs, args=(jobs, self.settings, str(self.output), True, stop, emit),
                                      kwargs={'pause': pause})
            worker.start()
            try:
                self.assertTrue(paused.wait(8), events)
                self.assertEqual(len(processes), 1)
                self.assertIsNone(processes[0].poll())
                self.assertFalse((other / 'result').exists())
                pause.clear()
                worker.join(8)
                self.assertFalse(worker.is_alive())
            finally:
                stop.set()
                pause.clear()
                worker.join(8)
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].poll())
        self.assertEqual([e[2] for e in events if e[0] == 'status'], ['running', 'done', 'running', 'done'])
        self.assertEqual({p.name for p in (self.chapter / 'result').iterdir()}, {'2.png', '3.png'})
        self.assertEqual({p.name for p in (other / 'result').iterdir()}, {'1.png'})
        self.assertEqual([(e[1], e[2]['total_tokens']) for e in events if e[0] == 'usage'], [(0, 10), (1, 10)])
        self.assertEqual(len(set((self.chapter.parent / 'pids.txt').read_text().splitlines())), 1)

    def test_continuous_failure_restarts_for_next_folder(self):
        self.continuous_native()
        failed = self.chapter.parent / 'Fail'
        failed.mkdir()
        (failed / '1.png').write_bytes(b'source')
        events = []
        run_jobs([Job(failed, 'translate'), Job(self.chapter, 'translate')], self.settings,
                 str(self.output), True, threading.Event(), events.append)
        self.assertEqual([e[2] for e in events if e[0] == 'status'], ['running', 'failed', 'running', 'done'])
        self.assertEqual(len(set((self.chapter.parent / 'pids.txt').read_text().splitlines())), 2)

    def test_stop_while_session_paused_closes_process_and_keeps_next_job_pending(self):
        import subprocess
        self.continuous_native()
        other = self.chapter.parent / 'Chapter 2'
        other.mkdir()
        (other / '1.png').write_bytes(b'source')
        pause, stop = threading.Event(), threading.Event()
        events, processes = [], []
        def emit(event):
            events.append(event)
            if event[:3] == ('status', 0, 'done'):
                pause.set()
            if event[0] == 'paused':
                stop.set()
        original = subprocess.Popen
        def spawn(*args, **kwargs):
            process = original(*args, **kwargs)
            processes.append(process)
            return process
        with mock.patch('queue_worker.subprocess.Popen', side_effect=spawn):
            run_jobs([Job(self.chapter, 'translate'), Job(other, 'translate')], self.settings,
                     str(self.output), True, stop, emit, pause=pause)
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].poll())
        self.assertFalse(any(event[0] == 'status' and event[1] == 1 for event in events))
        self.assertFalse((other / 'result').exists())

    def test_selected_pages_do_not_import_bundled_python_modules(self):
        bundle = self.root / 'bundle'
        bundle.mkdir()
        (bundle / 'bt_run_bridge.py').write_bytes(
            (Path(__file__).resolve().parents[1] / 'bt_run_bridge.py').read_bytes())
        (bundle / 'socket.py').write_text("raise ImportError('bundled Python version mismatch')\n")
        launch = Path(self.settings['bt_path']) / 'ballontranslator' / 'launch.py'
        launch.write_text('import socket\n' + launch.read_text(encoding='utf-8'), encoding='utf-8')
        job = Job(self.chapter, 'translate', start_page=1, end_page=2)
        with mock.patch.object(sys, '_MEIPASS', str(bundle), create=True):
            events = self.run_job(job)
        done = [event for event in events if event[0] == 'status'][-1]
        self.assertEqual(done[2], 'done', done[3])
        self.assertEqual({p.name for p in (self.chapter / 'result').iterdir()}, {'1.png', '2.png'})

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.chapter = self.root / "系列, 測試" / "Chapter 1"
        self.chapter.mkdir(parents=True)
        for name in ("10.png", "2.png", "1.png", "3.png"):
            (self.chapter / name).write_bytes(b"source")
        package = self.root / "native" / "ballontranslator"
        (package / "ui").mkdir(parents=True)
        for path in (package / "__init__.py", package / "__main__.py", package / "ui" / "__init__.py"):
            path.touch()
        (package / "launch.py").write_text(
            "import argparse\nfrom types import SimpleNamespace\n"
            "parser = argparse.ArgumentParser()\nparser.add_argument('--config')\n"
            "parser.add_argument('--headless', action='store_true')\nparser.add_argument('--exec_dirs')\n"
            "args, _ = parser.parse_known_args()\n"
            "shared = SimpleNamespace(pbar={'detect': SimpleNamespace(reset=lambda **kw: None)})\nready = False\n"
            "def setup_locks():\n    global ready\n    ready = True\n"
            "def main():\n    setup_locks()\n    from .ui.mainwindow import MainWindow\n"
            "    assert isinstance(args.exec_dirs, list) and len(args.exec_dirs) == 1\n"
            "    MainWindow(args.exec_dirs[0]).on_run_imgtrans()\n"
            "    print('finished translating all dirs')\n    input()\n", encoding="utf-8")
        (package / "ui" / "mainwindow.py").write_text(
            "from pathlib import Path\nfrom types import SimpleNamespace\n"
            "from .. import launch\nassert launch.ready\n"
            "class MainWindow:\n"
            "    def __init__(self, chapter):\n"
            "        self.path = Path(chapter)\n"
            "        self.imgtrans_proj = SimpleNamespace(pages={p.name: [] for p in self.path.glob('*.png')})\n"
            "    def on_run_imgtrans(self, pages_to_process=None):\n"
            "        pages = list(self.imgtrans_proj.pages) if pages_to_process is None else pages_to_process\n"
            "        result = self.path / 'result'\n        result.mkdir(exist_ok=True)\n"
            "        for name in pages:\n            (result / name).write_bytes(b'translated')\n"
            "        for stage in ('Text Detection', 'OCR', 'Inpaint', 'Translation'):\n"
            "            print(f'{stage}: 100%|##########| {len(pages)}/{len(pages)} [00:01<00:00, 1it/s]')\n",
            encoding="utf-8")
        self.config = self.root / "config.json"
        self.config.write_text("{}", encoding="utf-8")
        self.settings = dict(bt_path=str(package.parent), bt_python=sys.executable, bt_config=str(self.config))
        self.output = self.root / "output"

    def run_job(self, job, **options):
        events = []
        run_jobs([job], dict(self.settings, **options), str(self.output), True, threading.Event(), events.append)
        return events

    def test_range_uses_natural_order_and_inclusive_endpoints(self):
        images = image_files(self.chapter)
        self.assertEqual([p.name for p in images], ["1.png", "2.png", "3.png", "10.png"])
        self.assertEqual(Job(self.chapter, "translate").select_pages(images), images)
        self.assertEqual(Job(self.chapter, "translate", start_page=2, end_page=3).select_pages(images), images[1:3])
        self.assertEqual(Job(self.chapter, "translate", start_page=4).select_pages(images), images[3:])
        for start, end in ((0, None), (3, 2), (1, 5), (5, None), (True, 2), ("2", 3), (1, 1.5)):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                Job(self.chapter, "translate", start_page=start, end_page=end).select_pages(images)

    def test_selected_pages_reach_native_process_and_partial_results_are_successful(self):
        mask = self.chapter / "mask"
        mask.mkdir()
        (mask / "keep.png").write_bytes(b"work")
        events = self.run_job(Job(self.chapter, "translate", start_page=2, end_page=3, range_export=True), bt_export=True, bt_cleanup=True)
        self.assertEqual([p.name for p in image_files(self.chapter / "result")], ["2.png", "3.png"])
        self.assertTrue((mask / "keep.png").exists())
        self.assertFalse(self.output.exists())
        done = [e for e in events if e[0] == "status"][-1]
        self.assertEqual(done[2], "done", done[3])
        self.assertTrue(done[3])
        self.assertEqual([e[1] for e in events if e[0] == "bt_reset"], [2])
        self.assertEqual([e[2:] for e in events if e[0] == "bt_progress"], [(100, 2, 2, "00:00")] * 4)
        self.assertIn(("result", self.chapter / "result"), events)

    def test_unselected_results_are_preserved_and_complete_chapter_can_export(self):
        result = self.chapter / "result"
        result.mkdir()
        for name in ("1.png", "3.png", "10.png"):
            (result / name).write_bytes(b"existing translation")
        events = self.run_job(Job(self.chapter, "translate", start_page=2, end_page=2, range_export=True), bt_export=False)
        self.assertIn(("status", 0, "done", ""), events)
        for name in ("1.png", "3.png", "10.png"):
            self.assertEqual((result / name).read_bytes(), b"existing translation")
        archive = output_path_for(self.output, self.chapter.parent, self.chapter)
        self.assertTrue(archive.is_file())
        self.assertIn(("result", archive), events)

    def test_selected_pages_default_to_no_export_even_with_complete_results(self):
        result = self.chapter / "result"
        result.mkdir()
        for name in ("1.png", "2.png", "3.png", "10.png"):
            (result / name).write_bytes(b"existing translation")
        for start, end in ((2, 2), (1, 4), (2, None)):
            with self.subTest(start=start, end=end):
                events = self.run_job(Job(self.chapter, "translate", start_page=start, end_page=end), bt_export=True)
                self.assertIn(("status", 0, "done", ""), events)
                self.assertFalse(self.output.exists())
                self.assertIn(("result", result), events)

    def test_default_still_translates_all_pages(self):
        events = self.run_job(Job(self.chapter, "translate"), bt_export=True)
        self.assertIn(("status", 0, "done", ""), events)
        self.assertEqual(len(image_files(self.chapter / "result")), 4)
        self.assertTrue(output_path_for(self.output, self.chapter.parent, self.chapter).is_file())

    def test_invalid_range_or_missing_selected_output_fails_without_followups(self):
        for job in (Job(self.chapter, "translate", start_page=5), Job(self.chapter, "translate", start_page=2, end_page=3)):
            with self.subTest(start=job.start_page), mock.patch("queue_worker.run_translation") as translate:
                events = self.run_job(job, bt_export=True, bt_cleanup=True)
            self.assertEqual([e[2] for e in events if e[0] == "status"], ["running", "failed"])
            self.assertFalse(any(e[0] == "result" for e in events))
            self.assertFalse(self.output.exists())
            self.assertEqual(translate.call_count, int(job.start_page == 2))

    def test_bridge_resets_all_totals_and_refuses_missing_pages_or_unsupported_version(self):
        class Window:
            def on_run_imgtrans(self, pages_to_process=None):
                return pages_to_process
        bars = {name: mock.Mock() for name in ("detect", "ocr", "inpaint", "translate")}
        install_page_selection(Window, SimpleNamespace(pbar=bars), ["2.png", "3.png"])
        window = Window()
        window.imgtrans_proj = SimpleNamespace(pages={"1.png": [], "2.png": [], "3.png": []})
        self.assertEqual(window.on_run_imgtrans(), ["2.png", "3.png"])
        for bar in bars.values():
            bar.reset.assert_called_once_with(total=2)
        del window.imgtrans_proj.pages["3.png"]
        with self.assertRaises(ValueError):
            window.on_run_imgtrans()
        class OldWindow:
            def on_run_imgtrans(self):
                pass
        with self.assertRaisesRegex(RuntimeError, "does not support"):
            install_page_selection(OldWindow, SimpleNamespace(pbar={}), ["2.png"])

    def test_all_disabled_stages_are_rejected_before_starting_native_process(self):
        self.config.write_text('{"module":{"enable_detect":false,"enable_ocr":false,"enable_translate":false,"enable_inpaint":false}}')
        with mock.patch("queue_worker.run_translation") as translate:
            events = self.run_job(Job(self.chapter, "translate", start_page=2, end_page=3))
        translate.assert_not_called()
        self.assertEqual([e[2] for e in events if e[0] == "status"], ["running", "failed"])
