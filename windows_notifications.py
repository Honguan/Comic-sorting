"""Windows taskbar notifications, independent of the remote ntfy service."""
import tkinter as tk
from tkinter import ttk

import win32con
import win32gui

from app_logging import logger
from ui_language import tr


class WindowsNotifications:
    def __init__(self, app, parent, settings):
        self.app = app
        self.icon_window = None
        self.saved = {key: settings.get('windows_notify_' + key, key != 'success') is True
                      for key in ('enabled', 'done', 'failed', 'success')}
        self.fields = {key: tk.BooleanVar(value=value) for key, value in self.saved.items()}
        box = ttk.LabelFrame(parent, text=tr('Windows 桌面通知'), padding=8)
        box.pack(fill='x', padx=8, pady=8)
        row = ttk.Frame(box)
        row.pack(fill='x')
        for key, label in (('enabled', '啟用通知'), ('done', '整批結束'),
                           ('failed', '單項失敗／異常'), ('success', '單項成功')):
            ttk.Checkbutton(row, text=tr(label), variable=self.fields[key]).pack(side='left', padx=(0, 12))
        row = ttk.Frame(box)
        row.pack(fill='x', pady=4)
        ttk.Button(row, text=tr('儲存通知設定'), command=self.save).pack(side='left')
        ttk.Button(row, text=tr('測試 Windows 通知'), command=lambda: self.show(
            tr('測試通知'), tr('這是 Comic sorting 的 Windows 測試通知。'), 3)).pack(side='left', padx=4)
        self.status = tk.StringVar()
        ttk.Label(box, textvariable=self.status, wraplength=720).pack(anchor='w')
        app.root.bind('<Destroy>', self.close, add='+')

    def settings(self):
        return {'windows_notify_' + key: value for key, value in self.saved.items()}

    def save(self):
        previous = self.saved
        self.saved = {key: value.get() for key, value in self.fields.items()}
        if not self.app.save_settings():
            self.saved = previous
            self.status.set(tr('通知設定儲存失敗'))
            return
        self.status.set(tr('通知設定已儲存'))

    def send(self, event, title, message, priority):
        if self.saved['enabled'] and self.saved.get(event):
            self.show(title, message, priority)

    def show(self, title, message, priority):
        try:
            # Tk's message loop owns this window and keeps the notification icon alive.
            hwnd = self.app.root.winfo_id()
            if self.icon_window is not None:
                win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, (self.icon_window, 1))
                self.icon_window = None
            icon = win32gui.LoadIcon(0, win32con.IDI_APPLICATION)
            win32gui.Shell_NotifyIcon(win32gui.NIM_ADD,
                                    (hwnd, 1, win32gui.NIF_ICON | win32gui.NIF_TIP, 0, icon, 'Comic sorting'))
            self.icon_window = hwnd
            # Windows limits title/body to 63/255 UTF-16 code units.
            def limit(text, size):
                return text.encode('utf-16-le')[:size * 2].decode('utf-16-le', errors='ignore')
            win32gui.Shell_NotifyIcon(win32gui.NIM_MODIFY,
                (hwnd, 1, win32gui.NIF_INFO, 0, icon, 'Comic sorting',
                 limit(message, 255), 10000, limit('Comic sorting - ' + title, 63),
                 win32gui.NIIF_WARNING if priority >= 4 else win32gui.NIIF_INFO))
            self.status.set(tr('已交給 Windows 顯示；勿擾模式或系統通知設定可能隱藏通知。'))
        except Exception:
            logger.warning('windows_notification_failed', exc_info=True)
            self.status.set(tr('Windows 通知失敗；佇列繼續執行。'))

    def close(self, event):
        if event.widget is self.app.root and self.icon_window is not None:
            try:
                win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, (self.icon_window, 1))
            except Exception:
                logger.warning('windows_notification_cleanup_failed', exc_info=True)
            self.icon_window = None
