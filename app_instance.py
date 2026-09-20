"""Windows process-lifetime guard, keyed by the resolved settings file."""
import hashlib
import os
from pathlib import Path

if os.name == 'nt':
    import ctypes
    from ctypes import wintypes
    _user32 = ctypes.WinDLL('user32', use_last_error=True)
    _user32.SetPropW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.HANDLE]
    _user32.SetPropW.restype = wintypes.BOOL
    for _name in ('GetPropW', 'RemovePropW'):
        getattr(_user32, _name).argtypes = [wintypes.HWND, wintypes.LPCWSTR]
        getattr(_user32, _name).restype = wintypes.HANDLE


class AppInstance:
    def __init__(self, settings):
        self.name = 'ComicSorting-' + hashlib.sha256(
            os.path.normcase(str(Path(settings).resolve())).encode('utf-8')).hexdigest()
        self.handle = None
        self.acquired = False
        self.window = None

    def acquire(self):
        import win32api
        import win32event
        self.handle = win32event.CreateMutex(None, False, 'Global\\' + self.name)
        result = win32event.WaitForSingleObject(self.handle, 0)
        self.acquired = result in (win32event.WAIT_OBJECT_0, win32event.WAIT_ABANDONED)
        if not self.acquired:
            win32api.CloseHandle(self.handle)
            self.handle = None
        return self.acquired

    def register_window(self, root):
        import win32con
        import win32gui
        root.update_idletasks()
        self.window = win32gui.GetAncestor(root.winfo_id(), win32con.GA_ROOT)
        if not _user32.SetPropW(self.window, self.name, 1):
            raise ctypes.WinError(ctypes.get_last_error())

    def activate_existing(self):
        import win32con
        import win32gui
        def activate(window, _):
            if _user32.GetPropW(window, self.name):
                if win32gui.IsIconic(window):
                    win32gui.ShowWindow(window, win32con.SW_RESTORE)
                try:
                    win32gui.SetForegroundWindow(window)
                except Exception:
                    # Windows may deny focus stealing; still signal the taskbar.
                    win32gui.FlashWindow(window, True)
        win32gui.EnumWindows(activate, None)

    def close(self):
        import win32api
        import win32event
        import win32gui
        if self.window is not None and win32gui.IsWindow(self.window):
            _user32.RemovePropW(self.window, self.name)
        self.window = None
        if self.handle is not None:
            if self.acquired:
                win32event.ReleaseMutex(self.handle)
            win32api.CloseHandle(self.handle)
            self.handle = None
            self.acquired = False
