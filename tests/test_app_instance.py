import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from app_instance import AppInstance


@unittest.skipUnless(os.name == 'nt', 'Windows instance guard')
class InstanceTests(unittest.TestCase):
    def test_other_process_is_blocked_and_release_allows_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            settings = Path(temp) / 'settings.json'
            first = AppInstance(settings)
            self.addCleanup(first.close)
            self.assertTrue(first.acquire())
            script = ('from app_instance import AppInstance; import sys; '
                      'guard=AppInstance(sys.argv[1]); print(guard.acquire()); guard.close()')
            def check(path):
                return subprocess.check_output([sys.executable, '-c', script, str(path)], text=True).strip()
            self.assertEqual(check(settings), 'False')
            self.assertEqual(check(settings.parent / '.' / settings.name.upper()), 'False')
            self.assertEqual(check(settings.with_name('other.json')), 'True')
            self.assertFalse(settings.exists())
            first.close()
            self.assertEqual(check(settings), 'True')

    def test_crashed_owner_does_not_leave_stale_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            settings = Path(temp) / 'settings.json'
            script = ('from app_instance import AppInstance; import sys,os; '
                      'guard=AppInstance(sys.argv[1]); assert guard.acquire(); os._exit(0)')
            subprocess.run([sys.executable, '-c', script, str(settings)], check=True)
            guard = AppInstance(settings)
            self.addCleanup(guard.close)
            self.assertTrue(guard.acquire())

    def test_existing_minimized_window_is_restored(self):
        root = Mock()
        root.winfo_id.return_value = 123
        guard = AppInstance(__file__)
        with patch('win32gui.GetAncestor', return_value=456), patch('app_instance._user32.SetPropW', return_value=1) as register:
            guard.register_window(root)
            register.assert_called_once_with(456, guard.name, 1)
        with patch('win32gui.EnumWindows', side_effect=lambda callback, data: callback(456, data)), patch('app_instance._user32.GetPropW', return_value=1), patch('win32gui.IsIconic', return_value=True), patch('win32gui.ShowWindow') as restore, patch('win32gui.SetForegroundWindow') as foreground:
            guard.activate_existing()
            restore.assert_called_once()
            foreground.assert_called_once_with(guard.window)
