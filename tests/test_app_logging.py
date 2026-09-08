from pathlib import Path
import sys
import tempfile
import threading
import unittest

from app_logging import configure_logging, log_path, logger
from queue_worker import Job, run_jobs, run_translation


class AppLoggingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.old_handlers = logger.handlers[:]
        self.old_level, self.old_propagate = logger.level, logger.propagate
        self.addCleanup(self.restore_logging)
        self.log = configure_logging(self.root / "logs/test.log")

    def restore_logging(self):
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
            handler.close()
        for handler in self.old_handlers:
            logger.addHandler(handler)
        logger.setLevel(self.old_level)
        logger.propagate = self.old_propagate

    def test_utf8_output_and_process_exit_are_persisted(self):
        run_translation([sys.executable, "-u", "-c",
                         "print('Translation: 100%'); print('finished translating all dirs'); input()"],
                        self.root, None, threading.Event(), lambda *p: None, log_context="job-1")
        logger.info("漫畫測試")
        text = self.log.read_text(encoding="utf-8")
        for expected in ("job-1", "translator_start", "translator_pid=", "Translation: 100%",
                         "translator_exit=0", "completed=True", "漫畫測試"):
            self.assertIn(expected, text)
        self.assertEqual(log_path(), self.log)

    def test_secrets_are_redacted_in_messages_and_tracebacks(self):
        logger.info('api_key="key-123" Authorization: Bearer bearer-456 password=pwd-789')
        try:
            raise RuntimeError("https://example.test/?access_token=token-123&x=1")
        except RuntimeError:
            logger.exception("request_failed")
        text = self.log.read_text(encoding="utf-8")
        for secret in ("key-123", "bearer-456", "pwd-789", "token-123"):
            self.assertNotIn(secret, text)
        self.assertIn("<REDACTED>", text)
        self.assertIn("Traceback", text)

    def test_log_rotation_is_bounded(self):
        handler = logger.handlers[0]
        self.assertEqual((handler.maxBytes, handler.backupCount), (10 * 1024 * 1024, 3))
        handler.maxBytes = 512
        for _ in range(30):
            logger.info("x" * 200)
        self.assertEqual(len(list(self.log.parent.glob("test.log*"))), 4)

    def test_queue_failure_has_job_path_and_traceback(self):
        events = []
        run_jobs([Job(self.root / "missing", "translate")], {}, "", True, threading.Event(), events.append)
        text = self.log.read_text(encoding="utf-8")
        for expected in ("queue_start", "job_start", "missing", "job_failed", "Traceback", "queue_end"):
            self.assertIn(expected, text)
        self.assertEqual(events[-1], ("done",))


if __name__ == "__main__":
    unittest.main()
