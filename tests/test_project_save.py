import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from bt_run_bridge import install_project_save


class ProjectSaveTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'Windows file sharing')
    def test_atomic_save_waits_for_reader_and_preserves_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / 'imgtrans_章節.json'
            target.write_text('old', encoding='utf-8')
            attempts = []

            class Project:
                def save(self, keep_exist_as_backup=False):
                    attempts.append(True)
                    replacement = target.with_suffix('.json.tmp')
                    replacement.write_text('new', encoding='utf-8')
                    if keep_exist_as_backup and target.exists():
                        os.replace(target, target.with_suffix('.json.backup'))
                    os.replace(replacement, target)
                    return 'saved'

            install_project_save(Project)
            reader = target.open('rb')
            release = threading.Timer(.15, reader.close)
            release.start()
            try:
                self.assertEqual(Project().save(keep_exist_as_backup=True), 'saved')
            finally:
                reader.close()
                release.join()
            self.assertGreater(len(attempts), 1)
            self.assertEqual(target.read_text(encoding='utf-8'), 'new')
            self.assertEqual(target.with_suffix('.json.backup').read_text(encoding='utf-8'), 'old')

    def test_permanent_lock_is_bounded_and_preserves_error(self):
        error = PermissionError('locked')
        error.winerror = 32

        class Project:
            save = Mock(side_effect=error)

        original = Project.save
        install_project_save(Project)
        with patch('bt_run_bridge.time.monotonic', side_effect=[0, .1, 1]), patch('bt_run_bridge.time.sleep') as sleep:
            with self.assertRaises(PermissionError) as raised:
                Project().save()
        self.assertIs(raised.exception, error)
        self.assertEqual(original.call_count, 2)
        sleep.assert_called_once_with(.05)

    def test_other_errors_are_not_retried(self):
        for error in (PermissionError('denied'), OSError('disk failure')):
            with self.subTest(error=error):
                class Project:
                    save = Mock(side_effect=error)

                original = Project.save
                install_project_save(Project)
                with patch('bt_run_bridge.time.sleep') as sleep:
                    with self.assertRaises(type(error)) as raised:
                        Project().save()
                self.assertIs(raised.exception, error)
                self.assertEqual(original.call_count, 1)
                sleep.assert_not_called()


if __name__ == '__main__':
    unittest.main()
