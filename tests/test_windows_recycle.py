import tempfile
import unittest
from pathlib import Path
from unittest import mock

import windows_recycle as recycle


class RecycleTests(unittest.TestCase):
    def test_com_callback_aborts_permanent_deletion(self):
        with tempfile.TemporaryDirectory() as temp:
            marker = Path(temp) / "keep.txt"
            marker.write_text("keep", encoding="utf-8")
            item = recycle.shell.SHCreateItemFromParsingName(temp, None, recycle.shell.IID_IShellItem)
            tracker = recycle.RecycleSink()
            with self.assertRaises(recycle.COMException) as refusal:
                tracker.PreDeleteItem(0, item)
            self.assertEqual(refusal.exception.scode, recycle.winerror.E_ABORT)
            sink = recycle.pythoncom.WrapObject(tracker, recycle.shell.IID_IFileOperationProgressSink)
            fileop = recycle.pythoncom.CoCreateInstance(
                recycle.shell.CLSID_FileOperation, None, recycle.pythoncom.CLSCTX_INPROC_SERVER,
                recycle.shell.IID_IFileOperation)
            # A real Shell operation without recycling must be vetoed by our COM callback.
            fileop.SetOperationFlags(recycle.shellcon.FOF_NOCONFIRMATION
                                     | recycle.shellcon.FOF_NOERRORUI | recycle.shellcon.FOF_SILENT
                                     | recycle.shellcon.FOFX_EARLYFAILURE)
            fileop.DeleteItem(item, sink)
            try:
                fileop.PerformOperations()
            except recycle.pywintypes.com_error:
                pass
            self.assertFalse(tracker.recycled)
            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

    def test_only_completed_recycling_is_reported_as_success(self):
        for aborted, result, recycled in ((False, 0, True), (True, 0, True),
                                           (False, -1, False), (False, 0, False)):
            with self.subTest(aborted=aborted, result=result, recycled=recycled):
                fileop = mock.Mock()
                fileop.PerformOperations.return_value = 0
                fileop.GetAnyOperationsAborted.return_value = aborted
                fileop.DeleteItem.side_effect = lambda item, sink: sink.PostDeleteItem(
                    0, item, result, object() if recycled else None)
                with mock.patch.object(recycle.pythoncom, "CoCreateInstance", return_value=fileop), \
                        mock.patch.object(recycle.pythoncom, "WrapObject", side_effect=lambda value, _iid: value), \
                        mock.patch.object(recycle.shell, "SHCreateItemFromParsingName"):
                    if recycled and not aborted:
                        recycle.recycle_folder(Path("C:/test-only"))
                    else:
                        with self.assertRaises(OSError):
                            recycle.recycle_folder(Path("C:/test-only"))
                flags = fileop.SetOperationFlags.call_args.args[0]
                self.assertTrue(flags & 0x00080000)  # FOFX_RECYCLEONDELETE
                self.assertTrue(flags & recycle.shellcon.FOFX_EARLYFAILURE)
