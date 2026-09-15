import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest import mock
import win32gui

from ntfy_notifications import NtfyNotifications


class WindowsNotificationTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        patcher = mock.patch('windows_notifications.win32gui.Shell_NotifyIcon')
        self.shell = patcher.start()
        self.addCleanup(patcher.stop)
        self.app = SimpleNamespace(root=self.root, work_tabs=tk.ttk.Notebook(self.root), save_settings=mock.Mock(return_value=True))
        self.notifications = NtfyNotifications(self.app, {})
        self.windows = self.notifications.windows

    def test_independent_defaults_events_errors_and_cleanup(self):
        self.assertFalse(self.notifications.saved['enabled'])
        self.notifications.send('success', '成功', 'test')
        self.shell.assert_not_called()
        self.notifications.send('failed', '異常', 'E_TEST: test', 4)
        self.assertEqual(self.shell.call_args.args[0], win32gui.NIM_MODIFY)
        self.assertEqual(self.shell.call_args.args[1][-1], win32gui.NIIF_WARNING)
        self.assertIn('E_TEST', self.shell.call_args.args[1][6])
        self.notifications.send('done', '完成', '😀' * 300)
        self.assertLessEqual(len(self.shell.call_args.args[1][6].encode('utf-16-le')), 510)
        self.windows.close(SimpleNamespace(widget=self.root))
        self.assertEqual(self.shell.call_args.args[0], win32gui.NIM_DELETE)
        self.assertIsNone(self.windows.icon_window)
        self.shell.side_effect = RuntimeError('simulated')
        self.notifications.send('failed', '異常', 'error', 4)  # Must not escape into queue processing.

    def test_save_restart_and_failed_save_rollback(self):
        self.windows.fields['enabled'].set(False)
        self.windows.save()
        saved = self.notifications.settings()
        self.assertFalse(saved['windows_notify_enabled'])
        restored = NtfyNotifications(self.app, saved)
        self.assertFalse(restored.windows.saved['enabled'])
        self.windows.fields['enabled'].set(True)
        self.app.save_settings.return_value = False
        self.windows.save()
        self.assertFalse(self.windows.saved['enabled'])
        self.notifications.send('done', 'complete', 'test')
        self.shell.assert_not_called()


if __name__ == '__main__':
    unittest.main()
