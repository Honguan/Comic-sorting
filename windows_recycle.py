"""Recycle one folder through Windows Shell; never fall back to permanent deletion."""

import pythoncom
import pywintypes
import winerror
from win32com.server.exception import COMException
from win32com.server.policy import DesignatedWrapPolicy
from win32com.shell import shell, shellcon

from ui_language import tr


class RecycleSink(DesignatedWrapPolicy):
    _com_interfaces_ = [shell.IID_IFileOperationProgressSink]
    _public_methods_ = [
        "StartOperations", "FinishOperations", "PreRenameItem", "PostRenameItem",
        "PreMoveItem", "PostMoveItem", "PreCopyItem", "PostCopyItem",
        "PreDeleteItem", "PostDeleteItem", "PreNewItem", "PostNewItem",
        "UpdateProgress", "ResetTimer", "PauseTimer", "ResumeTimer",
    ]

    def __init__(self):
        self._wrap_(self)
        self.recycled = False
        self.result = None

    def _noop(self, *_args):
        pass

    StartOperations = FinishOperations = PreRenameItem = PostRenameItem = _noop
    PreMoveItem = PostMoveItem = PreCopyItem = PostCopyItem = _noop
    PreNewItem = PostNewItem = UpdateProgress = ResetTimer = PauseTimer = ResumeTimer = _noop

    def PreDeleteItem(self, flags, _item):
        if not flags & shellcon.TSF_DELETE_RECYCLE_IF_POSSIBLE:
            # Returning an HRESULT from a Python COM handler does not abort it; raise instead.
            raise COMException(tr("無法移至資源回收筒，已停止刪除"), scode=winerror.E_ABORT)

    def PostDeleteItem(self, _flags, _item, result, newly_created):
        self.result = result
        self.recycled = result >= 0 and newly_created is not None


def recycle_folder(folder):
    pythoncom.CoInitialize()
    fileop = item = sink = None
    try:
        fileop = pythoncom.CoCreateInstance(
            shell.CLSID_FileOperation, None, pythoncom.CLSCTX_INPROC_SERVER,
            shell.IID_IFileOperation)
        fileop.SetOperationFlags(
            shellcon.FOF_NOCONFIRMATION | shellcon.FOF_NOERRORUI | shellcon.FOF_SILENT
            | shellcon.FOFX_EARLYFAILURE | 0x00080000 | 0x20000000)
        # Windows 8+: FOFX_RECYCLEONDELETE and FOFX_ADDUNDORECORD.
        tracker = RecycleSink()
        sink = pythoncom.WrapObject(tracker, shell.IID_IFileOperationProgressSink)
        item = shell.SHCreateItemFromParsingName(str(folder), None, shell.IID_IShellItem)
        fileop.DeleteItem(item, sink)
        result = fileop.PerformOperations()
        if result or fileop.GetAnyOperationsAborted() or not tracker.recycled:
            raise OSError(tr("無法移至資源回收筒，已停止刪除"), tracker.result)
    except pywintypes.com_error as error:
        raise OSError(tr("無法移至資源回收筒，已停止刪除"), error.strerror) from error
    finally:
        fileop = item = sink = None
        pythoncom.CoUninitialize()
