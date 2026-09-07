import subprocess
import sys
import tempfile
import threading
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest import mock

from comic_core import output_path_for
from queue_worker import Job, run_jobs, run_translation


class QueueWorkerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def chapter(self, name="Chapter 1"):
        chapter = self.root / "漫畫" / name
        (chapter / "result").mkdir(parents=True)
        (chapter / "mask").mkdir()
        (chapter / "mask" / "keep.png").write_bytes(b"mask")
        for index in range(3):
            (chapter / f"{index}.png").write_bytes(b"source")
            (chapter / "result" / f"{index}.png").write_bytes(b"translated")
        return chapter

    def test_subprocess_cancel_terminates_child_and_joins_watcher(self):
        stop = threading.Event()
        processes, watchers = [], []
        popen, thread = subprocess.Popen, threading.Thread

        def launch(*args, **kwargs):
            process = popen(*args, **kwargs)
            processes.append(process)
            return process

        def watch(*args, **kwargs):
            watcher = thread(*args, **kwargs)
            watchers.append(watcher)
            return watcher

        def progress(name, percent):
            self.assertEqual((name, percent), ("Translation", 1))
            stop.set()

        with mock.patch("queue_worker.subprocess.Popen", side_effect=launch), \
                mock.patch("queue_worker.threading.Thread", side_effect=watch):
            with self.assertRaises(RuntimeError):
                run_translation([sys.executable, "-u", "-c",
                                 "import sys; print('Translation: 1%'); sys.stdin.read()"],
                                self.root, None, stop, progress)
        self.assertTrue(stop.is_set())
        self.assertIsNotNone(processes[0].poll())
        self.assertTrue(processes[0].stdin.closed)
        self.assertTrue(processes[0].stdout.closed)
        self.assertEqual(len(watchers), 1)
        self.assertFalse(watchers[0].is_alive())

    def test_subprocess_failure_keeps_twenty_clean_diagnostic_lines(self):
        script = ("print('\\x1b[31mERROR\\x1b[0m'); "
                  "[print('\\x1b[32mline'+str(i)+'\\x1b[0m') for i in range(25)]; "
                  "print('finished translating all dirs'); input()")
        with self.assertRaises(RuntimeError) as caught:
            run_translation([sys.executable, "-u", "-c", script], self.root, None,
                            threading.Event(), lambda *args: None)
        detail = str(caught.exception)
        self.assertNotIn("\x1b", detail)
        self.assertEqual(detail.splitlines()[1:],
                         [f"line{i}" for i in range(6, 25)] + ["finished translating all dirs"])

    def test_failure_blocks_same_path_but_continues_other_path_without_mutating_jobs(self):
        chapter, other = self.chapter(), self.chapter("Chapter 2")
        jobs = (Job(chapter, "translate"), Job(chapter, "cleanup"), Job(other, "cleanup"))
        before = [asdict(job) for job in jobs]
        events = []
        with mock.patch("queue_worker.translator_command", return_value=([], self.root, {})), \
                mock.patch("queue_worker.run_translation", side_effect=RuntimeError("translation failed")):
            run_jobs(jobs, {"bt_path": "", "bt_config": "", "bt_cleanup": True},
                     str(self.root / "output"), False, threading.Event(), events.append)
        self.assertEqual([(e[1], e[2]) for e in events if e[0] == "status"],
                         [(0, "running"), (0, "failed"), (1, "blocked"), (2, "running"), (2, "done")])
        self.assertTrue((chapter / "mask" / "keep.png").exists())
        self.assertFalse((other / "mask" / "keep.png").exists())
        self.assertEqual([asdict(job) for job in jobs], before)
        self.assertEqual([e for e in events if e[0] == "total"],
                         [("total", 1), ("total", 2), ("total", 3)])
        self.assertEqual(events[-1], ("done",))

    def test_export_state_save_failure_prevents_cleanup(self):
        chapter = self.chapter()
        output = self.root / "output"
        events = []
        with mock.patch("queue_worker.save_json", side_effect=OSError("state write failed")) as save:
            run_jobs((Job(chapter, "export"), Job(chapter, "cleanup")), {"bt_cleanup": True},
                     str(output), False, threading.Event(), events.append)
        save.assert_called_once()
        self.assertTrue(output_path_for(output, chapter.parent, chapter).is_file())
        self.assertTrue((chapter / "mask" / "keep.png").exists())
        self.assertIn(("status", 0, "failed", "state write failed"), events)
        self.assertIn("blocked", [e[2] for e in events if e[0] == "status"])

    def test_cancel_during_export_preserves_original_archive_and_pending_jobs(self):
        chapter = self.chapter()
        output = self.root / "output"
        archive = output_path_for(output, chapter.parent, chapter)
        archive.parent.mkdir(parents=True)
        archive.write_bytes(b"original archive")
        jobs = (Job(chapter, "export"), Job(chapter, "cleanup"))
        before = [asdict(job) for job in jobs]
        stop, events = threading.Event(), []

        def emit(event):
            events.append(event)
            if event[0] == "stage" and event[2] > 0:
                stop.set()

        with mock.patch("queue_worker.save_json") as save:
            run_jobs(jobs, {"bt_cleanup": True}, str(output), False, stop, emit)
        save.assert_not_called()
        self.assertTrue(stop.is_set())
        self.assertEqual(archive.read_bytes(), b"original archive")
        self.assertFalse(archive.with_suffix(".cbz.tmp").exists())
        self.assertTrue((chapter / "mask" / "keep.png").exists())
        self.assertEqual([(e[1], e[2]) for e in events if e[0] == "status"],
                         [(0, "running"), (0, "cancelled")])
        self.assertEqual([asdict(job) for job in jobs], before)
        self.assertEqual(events[-1], ("done",))


if __name__ == "__main__":
    unittest.main()
