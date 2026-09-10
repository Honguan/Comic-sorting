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
        events = self.run_job(Job(self.chapter, "translate", start_page=2, end_page=3), bt_export=True, bt_cleanup=True)
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
        events = self.run_job(Job(self.chapter, "translate", start_page=2, end_page=2), bt_export=True)
        self.assertIn(("status", 0, "done", ""), events)
        for name in ("1.png", "3.png", "10.png"):
            self.assertEqual((result / name).read_bytes(), b"existing translation")
        archive = output_path_for(self.output, self.chapter.parent, self.chapter)
        self.assertTrue(archive.is_file())
        self.assertIn(("result", archive), events)

    def test_default_still_translates_all_pages(self):
        events = self.run_job(Job(self.chapter, "translate"))
        self.assertIn(("status", 0, "done", ""), events)
        self.assertEqual(len(image_files(self.chapter / "result")), 4)

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
