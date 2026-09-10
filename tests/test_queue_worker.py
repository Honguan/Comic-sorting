import subprocess
import sys
import tempfile
import threading
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest import mock

from comic_core import output_path_for
from queue_worker import BT_STAGES, Job, parse_bt_progress, run_jobs, run_translation


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

    def test_native_progress_counters_and_independent_eta(self):
        examples = (
            ("Text Detection:   0%|          | 0/960 [00:00<?, ?it/s]", ("Text Detection", 0, 0, 960, None)),
            ("OCR:   0%|          | 1/960 [00:15<4:00:04, 15.02s/it]", ("OCR", 0, 1, 960, "4:00:04")),
            ("Translation:  55%|#####     | 417/755 [52:41<33:36, 5.97s/it]", ("Translation", 55, 417, 755, "33:36")),
            ("Inpaint: 100%|##########| 2/2 [00:47<00:00, 23.79s/it]", ("Inpaint", 100, 2, 2, "00:00")),
            ("Translation: 100%|##########| 999/1000 [12:00<00:01, 1it/s]", ("Translation", 99, 999, 1000, "00:01")),
            ("OCR: 50%", ("OCR", 50, None, None, None)),
        )
        for line, expected in examples:
            with self.subTest(line=line):
                self.assertEqual(parse_bt_progress(line), expected)
        for line in ("[DEBUG] reply='Translation: 80%'", "Translation: 150%", "OCR: 0%| | 2/1 [00:00<00:00]"):
            self.assertIsNone(parse_bt_progress(line))

    def test_progress_survives_carriage_returns_ansi_and_attached_logs(self):
        output = ("\x1b[32mText Detection: 50%|#####| 1/2 [00:01<00:01, 1it/s]\x1b[0m\r"
                  "OCR: 0%| | 0/2 [00:00<?, ?it/s]\x1b[A\r"
                  "Translation: 50%|#####| 1/2 [00:08<00:08, 8s/it][DEBUG  ] trans_llm:_translate:100 - example\n"
                  "Inpaint: 100%|##########| 2/2 [00:03<00:00, 1it/s]\n"
                  "finished translating all dirs\n")
        progress = []
        run_translation([sys.executable, "-u", "-c", f"import sys; sys.stdout.write({output!r}); sys.stdout.flush(); input()"],
                        self.root, None, threading.Event(), lambda *values: progress.append(values))
        self.assertEqual([p[0] for p in progress], ["Text Detection", "OCR", "Translation", "Inpaint"])
        self.assertEqual(progress[2][2:], (1, 2, "00:08"))

    def test_each_translation_job_resets_real_total_and_enabled_stages(self):
        import json
        first = self.chapter()
        second = self.chapter("Chapter 2")
        config = self.root / "config.json"
        config.write_text(json.dumps({"module": {"enable_ocr": False}}), encoding="utf-8")
        events = []
        def translate(command, root, env, stop, progress, **kwargs):
            progress("Translation", 33, 1, 3, "00:20")
        settings = dict(bt_path="installed", bt_config=str(config))
        with mock.patch("queue_worker.translator_command", return_value=([], self.root, None)), \
                mock.patch("queue_worker.run_translation", side_effect=translate):
            run_jobs([Job(first, "translate"), Job(second, "translate")], settings, "", True,
                     threading.Event(), events.append)
        resets = [event for event in events if event[0] == "bt_reset"]
        self.assertEqual(len(resets), 2)
        self.assertEqual(resets[0][1], 3)
        self.assertFalse(resets[0][2]["OCR"])
        self.assertEqual(set(resets[0][2]), set(BT_STAGES))
        self.assertEqual(sum(e == ("bt_progress", "Translation", 33, 1, 3, "00:20") for e in events), 2)

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

        def progress(name, percent, current, total, eta):
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

    def test_completed_translation_reports_final_location_after_cleanup(self):
        chapter = self.chapter("Chapter 1, 測試")
        output = self.root / "output"
        settings = dict(bt_path="installed", bt_config="", bt_cleanup=True)
        for exporting in (False, True, True):  # Include an unchanged CBZ reused on the second export.
            with self.subTest(exporting=exporting), \
                    mock.patch("queue_worker.translator_command", return_value=([], self.root, None)), \
                    mock.patch("queue_worker.run_translation"):
                events = []
                run_jobs([Job(chapter, "translate")], dict(settings, bt_export=exporting),
                         str(output), True, threading.Event(), events.append)
                expected = output_path_for(output, chapter.parent, chapter) if exporting else chapter / "result"
                self.assertTrue(expected.exists())
                self.assertEqual([e for e in events if e[0] == "result"], [("result", expected)])
                self.assertGreater(events.index(("result", expected)), events.index(("status", 0, "done", "")))
                self.assertFalse((chapter / "mask" / "keep.png").exists())

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
                         ["ERROR"] + [f"line{i}" for i in range(6, 25)] + ["finished translating all dirs"])

    def test_recovered_llm_parse_error_does_not_fail_completed_translation(self):
        lines = [
            '[ERROR  ] trans_llm:_translate:1253 - Failed to parse matching translation count for prompt:',
            'Translate the following JSON array.',
            '[WARNING] trans_llm:_translate:1260 - LLM translation failed due to count mismatch. Attempt: 1',
            '[DEBUG  ] trans_llm:_log_token_usage:1090 - LLM token usage: page=76.jpg, attempt=2, finish_reason=stop',
            'Translation: 100%',
            'finished translating all dirs',
        ]
        script = f"lines = {lines!r}; [print(line) for line in lines]; input()"
        run_translation([sys.executable, '-u', '-c', script], self.root, None,
                        threading.Event(), lambda *args: None)

    def test_fatal_error_is_kept_even_when_progress_pushes_it_out_of_tail(self):
        script = ("print('[ERROR] message:create_error_dialog:33 - LLM output limit reached (8192)'); "
                  "[print('progress '+str(i)) for i in range(30)]; "
                  "print('finished translating all dirs'); input()")
        with self.assertRaisesRegex(RuntimeError, 'LLM output limit reached'):
            run_translation([sys.executable, '-u', '-c', script], self.root, None,
                            threading.Event(), lambda *args: None)

    def test_exhausted_retry_and_pipeline_stop_remain_failures(self):
        for error in (
                '[ERROR] trans_llm:_translate:1256 - LLM translation failed: count mismatch',
                '[INFO] module_manager:_imgtrans_pipeline:1208 - Image translation pipeline stopped.',
                'Translation: 50% [ERROR] message:create_error_dialog:33 - LLM output limit reached'):
            with self.subTest(error=error):
                script = f"print({error!r}); print('finished translating all dirs'); input()"
                with self.assertRaises(RuntimeError):
                    run_translation([sys.executable, '-u', '-c', script], self.root, None,
                                    threading.Event(), lambda *args: None)

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
        self.assertFalse(any(e[0] == "result" for e in events))

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
        self.assertFalse(any(e[0] == "result" for e in events))

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
        self.assertFalse(any(e[0] == "result" for e in events))


if __name__ == "__main__":
    unittest.main()
