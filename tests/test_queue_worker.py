import subprocess
import sys
import tempfile
import threading
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest import mock

from comic_core import export_chapter, output_path_for, save_json
from queue_worker import BT_STAGES, Job, parse_bt_progress, run_jobs, run_translation


class QueueWorkerTests(unittest.TestCase):
    def test_native_completion_overrides_stale_progress_for_matching_job_only(self):
        import io
        from contextlib import redirect_stdout
        from types import SimpleNamespace
        from bt_run_bridge import install_session
        for finished in (True, False):
            for matching in (True, False):
                with self.subTest(finished=finished, matching=matching):
                    class Window:
                        def on_run_imgtrans(self): pass
                        def run_next_dir(self): pass
                    request = dict(token='job', pages=None)
                    install_session(Window, SimpleNamespace(pbar={}), request)
                    window = Window()
                    window.exec_dirs = []
                    window.imsave_thread = SimpleNamespace(isRunning=lambda: False)
                    window.module_manager = SimpleNamespace(proj_finished=lambda: finished)
                    window.app = mock.Mock()
                    output = io.StringIO()
                    with redirect_stdout(output), mock.patch('sys.stdin', io.StringIO('exit\n')):
                        window.run_next_dir()
                    progress = '94%| | 16/17' if finished else '100%| | 17/17'
                    transcript = 'Translation: ' + progress + '\n' + output.getvalue()
                    if not matching:
                        transcript = transcript.replace('FINISHED:job:', 'FINISHED:previous:')
                    process = mock.Mock(stdout=io.StringIO(transcript))
                    session = mock.Mock(fatal=threading.Event())
                    session.open.return_value = process
                    args = ([], self.root, None, threading.Event(), lambda *values: None)
                    if (finished and matching) or (not finished and not matching):
                        self.assertIsNone(run_translation(*args, session=session, request=request))
                    else:
                        with self.assertRaises(RuntimeError):
                            run_translation(*args, session=session, request=request)

    def test_stale_progress_uses_completion_and_still_checks_results(self):
        script = "print('Text Detection: 94%| | 17/18'); print('Translation: 100%| | 18/18'); print('finished translating all dirs')"
        config = self.root / 'config.json'
        config.write_text('{}')
        for complete in (True, False):
            with self.subTest(complete=complete):
                chapter = self.chapter(str(complete))
                if not complete:
                    (chapter / 'result/0.png').unlink()
                events = []
                with mock.patch('queue_worker.translator_command', return_value=(
                        [sys.executable, '-u', '-c', script], self.root, None)):
                    run_jobs([Job(chapter, 'translate')],
                             dict(bt_path='installed', bt_config=str(config), bt_cleanup=True, bt_export=True),
                             str(self.root / 'output'), True, threading.Event(), events.append)
                statuses = [event[2] for event in events if event[0] == 'status']
                self.assertEqual(statuses, ['running', 'done' if complete else 'failed'])
                self.assertEqual((chapter / 'mask/keep.png').exists(), not complete)

    def test_quota_stops_queue_without_retry_or_export(self):
        first, second = self.chapter(), self.chapter('Chapter 2')
        stop, events = threading.Event(), []
        message = 'Codex turn failed: Your workspace is out of credits. Ask your workspace owner to refill in order to continue.'
        with mock.patch('queue_worker.translator_command', return_value=([], self.root, None)), \
                mock.patch('queue_worker.run_translation', side_effect=RuntimeError(message)) as translate, \
                mock.patch('queue_worker.export_chapter') as export:
            run_jobs([Job(first, 'translate'), Job(second, 'translate')],
                     dict(bt_path='installed', bt_config='', bt_export=True),
                     str(self.root / 'output'), False, stop, events.append)
        translate.assert_called_once()
        export.assert_not_called()
        self.assertTrue(stop.is_set())
        self.assertEqual([e for e in events if e[0] == 'status'],
                         [('status', 0, 'running', ''), ('status', 0, 'failed', message)])
        self.assertIn(('done',), events)

    def test_pipeline_stop_terminates_process_without_user_stop(self):
        import time
        stop = threading.Event()
        started = time.monotonic()
        script = "import time; print('[INFO] module_manager:run:1 - Image translation pipeline stopped.', flush=True); time.sleep(20)"
        with self.assertRaisesRegex(RuntimeError, 'pipeline stopped'):
            run_translation([sys.executable, '-u', '-c', script], self.root, None, stop, lambda *args: None)
        self.assertFalse(stop.is_set())
        self.assertLess(time.monotonic() - started, 5)

    @unittest.skipUnless(sys.platform == 'win32', 'Windows process tree')
    def test_pipeline_stop_closes_descendant_stdout_pipe(self):
        import time
        script = ("import subprocess,sys,time; time.sleep(.1); "
                  "subprocess.Popen([sys.executable,'-c','import time; time.sleep(15)']); "
                  "print('[INFO] module_manager:run:1 - Image translation pipeline stopped.',flush=True); time.sleep(15)")
        started = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, 'pipeline stopped'):
            run_translation([sys.executable, '-u', '-c', script], self.root, None, threading.Event(), lambda *args: None)
        self.assertLess(time.monotonic() - started, 5)

    def test_missing_job_fails_and_other_path_continues(self):
        events = []
        chapter = self.chapter()
        with mock.patch('queue_worker.clear_work_folders', return_value=(0, 0, [])) as cleanup:
            run_jobs([Job(self.root / 'missing', 'cleanup'), Job(chapter, 'cleanup')], {}, '', True,
                     threading.Event(), events.append)
        self.assertEqual([event[2] for event in events if event[0] == 'status'], ['running', 'failed', 'running', 'done'])
        cleanup.assert_called_once_with(chapter, '')

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

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

    def test_pause_finishes_current_followups_then_resumes_or_stops_between_jobs(self):
        for resume in (True, False):
            with self.subTest(resume=resume):
                first, second = self.chapter(f'First {resume}'), self.chapter(f'Second {resume}')
                config = self.root / 'config.json'
                config.write_text('{}')
                settings = dict(bt_path='installed', bt_config=str(config), bt_export=True, bt_cleanup=True)
                pause, stop, paused = threading.Event(), threading.Event(), threading.Event()
                events, clock = [], [0]

                def translate(*args, **kwargs):
                    clock[0] += 5
                    if not events or sum(e[0] == 'status' and e[2] == 'running' for e in events) == 1:
                        pause.set()

                def emit(event):
                    events.append(event)
                    if event[0] == 'paused':
                        paused.set()

                with mock.patch('queue_worker.translator_command', return_value=([], self.root, None)), \
                        mock.patch('queue_worker.run_translation', side_effect=translate), \
                        mock.patch('queue_worker.time.monotonic', side_effect=lambda: clock[0]):
                    worker = threading.Thread(target=run_jobs, args=(
                        [Job(first, 'translate'), Job(second, 'translate')], settings,
                        str(self.root / 'output'), True, stop, emit), kwargs={'pause': pause})
                    worker.start()
                    try:
                        self.assertTrue(paused.wait(3))
                        self.assertIn(('status', 0, 'done', ''), events)
                        self.assertFalse((first / 'mask/keep.png').exists())
                        self.assertTrue(output_path_for(self.root / 'output', first.parent, first).exists())
                        self.assertTrue((second / 'mask/keep.png').exists())
                        self.assertFalse(any(e[0] == 'status' and e[1] == 1 for e in events))
                        self.assertNotIn(('done',), events)
                        clock[0] += 100
                        if resume:
                            pause.clear()
                        else:
                            stop.set()
                        worker.join(3)
                        self.assertFalse(worker.is_alive())
                    finally:
                        stop.set()
                        pause.clear()
                        worker.join(3)
                self.assertEqual(sum(e == ('done',) for e in events), 1)
                self.assertEqual([e[3] for e in events if e[0] == 'run_time'], [10 if resume else 5])
                self.assertEqual((second / 'mask/keep.png').exists(), not resume)

    def test_pause_on_last_item_does_not_prevent_queue_completion(self):
        pause = threading.Event()
        events = []
        def cleanup(_path, _output):
            pause.set()
            return 0, 0, []
        with mock.patch('queue_worker.clear_work_folders', side_effect=cleanup):
            run_jobs([Job(self.chapter(), 'cleanup')], {}, '', True, threading.Event(),
                     events.append, pause=pause)
        self.assertIn(('status', 0, 'done', ''), events)
        self.assertEqual(events[-1], ('done',))
        self.assertFalse(any(e[0] == 'paused' for e in events))

    def test_resuming_preserves_failed_path_followup_blocking(self):
        first, second = self.chapter(), self.chapter('Chapter 2')
        pause, stop = threading.Event(), threading.Event()
        events = []
        def cleanup(path, _output):
            if path == first:
                pause.set()
                raise RuntimeError('synthetic failure')
            return 0, 0, []
        def emit(event):
            events.append(event)
            if event[0] == 'paused':
                pause.clear()
        with mock.patch('queue_worker.clear_work_folders', side_effect=cleanup) as clear:
            run_jobs([Job(first, 'cleanup'), Job(first, 'cleanup'), Job(second, 'cleanup')],
                     {}, '', True, stop, emit, pause=pause)
        self.assertEqual([e[2] for e in events if e[0] == 'status'],
                         ['running', 'failed', 'blocked', 'running', 'done'])
        self.assertEqual(clear.call_count, 2)

    def test_progress_survives_carriage_returns_ansi_and_attached_logs(self):
        output = ("\x1b[32mText Detection: 50%|#####| 1/2 [00:01<00:01, 1it/s]\x1b[0m\r"
                  "OCR: 0%| | 0/2 [00:00<?, ?it/s]\x1b[A\r"
                  "Translation: 50%|#####| 1/2 [00:08<00:08, 8s/it][DEBUG  ] trans_llm:_translate:100 - example\n"
                  "Inpaint: 100%|##########| 2/2 [00:03<00:00, 1it/s]\n"
                  "finished translating all dirs\n")
        progress = []
        timing = []
        with self.assertRaises(RuntimeError):  # Output ends with unfinished stages.
            run_translation([sys.executable, "-u", "-c", f"import sys; sys.stdout.write({output!r}); sys.stdout.flush(); input()"],
                            self.root, None, threading.Event(), lambda *values: progress.append(values),
                            timing=lambda *values: timing.append(values))
        self.assertEqual([p[0] for p in progress], ["Text Detection", "OCR", "Translation", "Inpaint"])
        self.assertEqual(progress[2][2:], (1, 2, "00:08"))
        self.assertEqual(timing, [('Text Detection', 1), ('OCR', 0), ('Translation', 8), ('Inpaint', 3)])

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
        settings = dict(bt_path="installed", bt_config="")
        for exporting in (False, True, True):  # Include an unchanged CBZ reused on the second export.
            with self.subTest(exporting=exporting), \
                    mock.patch("queue_worker.translator_command", return_value=([], self.root, None)), \
                    mock.patch("queue_worker.run_translation"):
                events = []
                run_jobs([Job(chapter, "translate")], dict(settings, bt_export=exporting, bt_cleanup=exporting),
                         str(output), True, threading.Event(), events.append)
                expected = output_path_for(output, chapter.parent, chapter) if exporting else chapter / "result"
                self.assertTrue(expected.exists())
                self.assertEqual([e for e in events if e[0] == "result"], [("result", expected)])
                self.assertGreater(events.index(("result", expected)), events.index(("status", 0, "done", "")))
                self.assertEqual((chapter / "mask" / "keep.png").exists(), not exporting)

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

    def test_retry_notifications_count_recovery_once_and_not_usage_or_exhaustion(self):
        lines = [
            '[WARNING] llm_chat:request_chat_completion:366 - Codex retryable failure: timeout. Attempt 1/3',
            '[ERROR] trans_llm:_translate:1253 - Failed to parse matching translation count for prompt:',
            '[WARNING] trans_llm:_translate:1260 - LLM translation failed due to count mismatch. Attempt: 1',
            '[INFO] trans_llm:_translate:1334 - Codex timeout rescue: page=1.jpg, Jev checks=1; retrying translation once.',
            '[INFO] trans_llm:_log_token_usage:1160 - LLM token usage: page=1.jpg, attempt=2',
            '[ERROR] llm_chat:request_chat_completion:360 - Codex retry budget exhausted after 3 attempts',
        ]
        retry = mock.Mock()
        script = f"lines = {lines!r}; [print(line) for line in lines]; input()"
        with self.assertRaises(RuntimeError):
            run_translation([sys.executable, '-u', '-c', script], self.root, None,
                            threading.Event(), lambda *args: None, retry=retry)
        self.assertEqual(retry.call_count, 3)

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

    def test_completed_process_exit_anomaly_is_reported_but_incomplete_is_failed(self):
        for percent in (100, 99):
            script = (f"print('Translation: {percent}%'); "
                      "print('finished translating all dirs'); input(); raise SystemExit(5)")
            args = ([sys.executable, '-u', '-c', script], self.root, None,
                    threading.Event(), lambda *args: None)
            if percent == 100:
                warning = run_translation(*args)
                self.assertIn('exit=5', warning)
            else:
                with self.assertRaises(RuntimeError):
                    run_translation(*args)

    def test_incomplete_normal_exit_preserves_old_results_and_blocks_followups(self):
        chapter = self.chapter()
        output = self.root / 'output'
        script = "print('Translation: 50%'); print('finished translating all dirs'); input()"
        events = []
        with mock.patch('queue_worker.translator_command', return_value=(
                [sys.executable, '-u', '-c', script], self.root, None)):
            run_jobs([Job(chapter, 'translate'), Job(chapter, 'cleanup')],
                     dict(bt_path='', bt_config='', bt_export=True, bt_cleanup=True),
                     str(output), False, threading.Event(), events.append)
        self.assertEqual([event[2] for event in events if event[0] == 'status'],
                         ['running', 'failed', 'blocked'])
        self.assertFalse(output.exists())
        self.assertTrue((chapter / 'mask' / 'keep.png').exists())
        self.assertEqual((chapter / 'result' / '0.png').read_bytes(), b'translated')

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
        state = {}
        export_chapter(other.parent, other, self.root / 'output', state)
        save_json(self.root / 'output/.comic-sorting-state.json', state)
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

    def test_bad_translator_config_fails_before_starting_process(self):
        chapter = self.chapter()
        config = self.root / 'config.json'
        config.write_text('{broken')
        events = []
        with mock.patch('queue_worker.run_translation') as translate:
            run_jobs([Job(chapter, 'translate')], dict(bt_config=str(config)), '', False,
                     threading.Event(), events.append)
        translate.assert_not_called()
        self.assertEqual([e[2] for e in events if e[0] == 'status'], ['running', 'failed'])
        self.assertEqual(config.read_text(), '{broken')

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
