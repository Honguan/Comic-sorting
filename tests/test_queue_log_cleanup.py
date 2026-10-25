import io
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from queue_worker import Job, run_jobs, run_translation


LOG_LOCK = (r"[ERROR  ] logger:setup_logging:113 - [WinError 32] "
            r"程序無法存取檔案，因為檔案正由另一個程序使用。: 'D:\AI\logs\old.log'")


class QueueLogCleanupTests(unittest.TestCase):
    def session(self, diagnostics, *, finished=True, boundary=True, completed=True, code=0):
        session = mock.Mock(fatal=threading.Event())

        def open_process(command, root, env, request, log_context):
            lines = list(diagnostics)
            if completed:
                lines.append('finished translating all dirs')
            if request is not None:
                lines.append(f"COMIC_SORTING_FINISHED:{request['token']}:{int(finished)}")
                if boundary:
                    lines.append('COMIC_SORTING_DONE:' + request['token'])
            process = mock.Mock(stdout=io.StringIO('\n'.join(lines)), wait=mock.Mock(return_value=code))
            return process

        session.open.side_effect = open_process
        return session

    def translate(self, diagnostics, **options):
        return run_translation([], Path('.'), None, threading.Event(), lambda *values: None,
                               session=self.session(diagnostics, **options),
                               request=dict(token='job', phase='prepare'))

    def test_locked_old_log_is_not_a_completion_anomaly(self):
        for line in (LOG_LOCK, '\x1b[31m' + LOG_LOCK + '\x1b[0m',
                     LOG_LOCK.replace('WinError 32', 'WinError 2'),
                     '2026-10-03 19:42:13 ' + LOG_LOCK,
                     LOG_LOCK.replace("'D:\\AI\\logs\\old.log'", '"D:/AI/logs/old.log"')):
            with self.subTest(line=line):
                self.assertIsNone(self.translate([line]))

    def test_other_errors_are_still_reported(self):
        lines = (LOG_LOCK.replace('logger:setup_logging', 'module_manager:run'),
                 LOG_LOCK.replace('old.log', 'page.png'),
                 LOG_LOCK.replace('WinError 32', 'WinError 5'),
                 LOG_LOCK.replace('logger:setup_logging', 'logger:open_log'),
                 'Traceback (most recent call last):')
        for line in lines:
            with self.subTest(line=line):
                diagnostic = self.translate([LOG_LOCK, line])
                self.assertIsInstance(diagnostic, str)
                self.assertIn(line, diagnostic)

    def test_log_cleanup_does_not_override_completion_guards(self):
        for options in (dict(finished=False), dict(boundary=False), dict(completed=False)):
            with self.subTest(options=options), self.assertRaises(RuntimeError):
                self.translate([LOG_LOCK], **options)
        diagnostic = run_translation([], Path('.'), None, threading.Event(), lambda *values: None,
                                     session=self.session([LOG_LOCK, 'Translation: 100%'], code=5))
        self.assertIsInstance(diagnostic, str)
        self.assertIn('exit=5', diagnostic)

    def test_prepare_marks_ready_only_for_old_log_cleanup_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            chapter = Path(temporary).resolve() / 'chapter'
            chapter.mkdir()
            (chapter / 'page.png').write_bytes(b'source')
            config = chapter.parent / 'config.json'
            config.write_text('{}', encoding='utf-8')
            settings = dict(bt_path='installed', bt_config=str(config), bt_phase='prepare')
            for line, expected in ((LOG_LOCK, 'prepared'),
                                   (LOG_LOCK.replace('old.log', 'page.png'), 'failed')):
                events = []
                with self.subTest(line=line), \
                        mock.patch('queue_worker.TranslationSession', return_value=self.session([line])), \
                        mock.patch('queue_worker.translator_command', return_value=([], chapter.parent, None)), \
                        mock.patch('queue_phases.prepared', return_value=True), \
                        mock.patch('queue_phases.mark_prepared') as mark:
                    run_jobs([Job(chapter, 'translate')], settings, '', True,
                             threading.Event(), events.append)
                    self.assertEqual([event[2] for event in events if event[0] == 'status'],
                                     ['running', expected])
                    self.assertEqual(mark.call_count, int(expected == 'prepared'))


if __name__ == '__main__':
    unittest.main()
