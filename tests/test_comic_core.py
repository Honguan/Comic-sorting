import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from comic_core import clear_work_folders, create_cbz, delete_manga_folder, load_json, save_json, validate_cbz

if os.name == "nt":
    from _winapi import CreateJunction


class FolderDeletionTests(unittest.TestCase):
    def test_deletes_only_selected_subtree_and_rejects_root_or_external_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            root = base / "Comics"
            chapter = root / "Series" / "Chapter 1"
            (chapter / "result").mkdir(parents=True)
            (chapter / "result/1.png").write_bytes(b"translated")
            keep = root / "Series" / "Chapter 2"
            keep.mkdir()
            sentinel = keep / "1.png"
            sentinel.write_bytes(b"keep")
            recycle = mock.patch("windows_recycle.recycle_folder", side_effect=lambda path: path.rename(base / f"recycled-{path.name}"))
            recycle.start()
            self.addCleanup(recycle.stop)
            for target in (root, base, root / "..", base / "outside"):
                with self.assertRaises(ValueError):
                    delete_manga_folder(target, root)
            delete_manga_folder(chapter, root)
            self.assertFalse(chapter.exists())
            self.assertEqual((base / "recycled-Chapter 1/result/1.png").read_bytes(), b"translated")
            self.assertEqual(sentinel.read_bytes(), b"keep")
            with mock.patch("windows_recycle.recycle_folder", side_effect=PermissionError("locked")):
                with self.assertRaises(PermissionError):
                    delete_manga_folder(keep, root)
            self.assertEqual(sentinel.read_bytes(), b"keep")
            delete_manga_folder(keep.parent, root)
            self.assertTrue(root.is_dir())
            self.assertEqual(list(root.iterdir()), [])

    @unittest.skipUnless(os.name == "nt", "Windows junctions require Windows")
    def test_junctions_are_rejected_before_deleting_any_files(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            root = base / "Comics"
            chapter = root / "Series" / "Chapter 1"
            chapter.mkdir(parents=True)
            external = base / "External"
            external.mkdir()
            sentinel = external / "keep.png"
            sentinel.write_bytes(b"external")
            local = chapter / "keep.png"
            local.write_bytes(b"local")
            link = chapter / "linked"
            CreateJunction(str(external), str(link))
            try:
                for target in (chapter, link, link / "keep.png"):
                    with self.assertRaises(ValueError):
                        delete_manga_folder(target, root)
                    self.assertEqual(sentinel.read_bytes(), b"external")
                    self.assertEqual(local.read_bytes(), b"local")
            finally:
                self.assertTrue(link.parent.resolve().is_relative_to(root))
                link.rmdir()


class AtomicWriteTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows file sharing requires Windows")
    def test_json_and_cbz_wait_for_a_temporary_reader_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            image = base / "1.png"
            image.write_bytes(b"image")
            for name in ("state.json", "chapter.cbz"):
                with self.subTest(name=name):
                    target = base / name
                    target.write_bytes(b"original")
                    reader = target.open("rb")
                    timer = threading.Timer(0.08, reader.close)
                    timer.start()
                    try:
                        if target.suffix == ".json":
                            save_json(target, {"updated": True})
                            self.assertEqual(load_json(target, {}), {"updated": True})
                        else:
                            create_cbz([image], target)
                            validate_cbz(target, [image])
                    finally:
                        timer.join()
                        reader.close()
                    self.assertEqual(list(base.glob("*.tmp")), [])

    def test_replace_retries_are_bounded_and_keep_original_on_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            target = base / "state.json"
            target.write_bytes(b"original")
            for winerror in (5, 32, 33, None):
                with self.subTest(winerror=winerror):
                    error = PermissionError("synthetic write failure")
                    if winerror is not None:
                        error.winerror = winerror
                    with mock.patch("comic_core.os.replace", side_effect=error) as replace, \
                            mock.patch("time.sleep") as sleep:
                        with self.assertRaises(PermissionError):
                            save_json(target, {"updated": True})
                    self.assertEqual(replace.call_count, 6 if winerror is not None else 1)
                    self.assertEqual(sleep.call_count, replace.call_count - 1)
                    self.assertLess(sum(call.args[0] for call in sleep.call_args_list), 1)
                    self.assertEqual(target.read_bytes(), b"original")
                    self.assertEqual(list(base.iterdir()), [target])


@unittest.skipUnless(os.name == "nt", "Windows junctions require Windows")
class CleanupJunctionTests(unittest.TestCase):
    def test_discovery_and_work_folder_junctions_preserve_external_data(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "root"
            external = base / "external"
            root.mkdir()
            (external / "mask").mkdir(parents=True)
            sentinel = external / "mask" / "keep.png"
            sentinel.write_bytes(b"keep")
            links = [root / "linked-series", root / "mask"]
            created = []
            try:
                for target, link in zip((external, external / "mask"), links):
                    CreateJunction(str(target), str(link))
                    created.append(link)

                folders, removed, errors = clear_work_folders(root)

                self.assertEqual((folders, removed), (1, 0))
                self.assertEqual(len(errors), 2)
                for link in links:
                    self.assertTrue(link.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
                    self.assertTrue(any(str(link) in error for error in errors))
                self.assertEqual(sentinel.read_bytes(), b"keep")
            finally:
                for link in created:
                    self.assertTrue(link.parent.resolve().is_relative_to(root.resolve()))
                    link.rmdir()
                self.assertEqual(sentinel.read_bytes(), b"keep")

    def test_nested_junction_error_does_not_stop_normal_cleanup(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "root"
            external = base / "external"
            external.mkdir()
            sentinel = external / "keep.png"
            sentinel.write_bytes(b"keep")
            inpainted = root / "Chapter 1" / "inpainted"
            nested = inpainted / "nested"
            nested.mkdir(parents=True)
            link = nested / "external"
            (nested / "preserved.png").write_bytes(b"preserved")
            (inpainted / "remove.png").write_bytes(b"remove")
            mask = root / "Chapter 2" / "Mask"
            (mask / "nested").mkdir(parents=True)
            (mask / "nested" / "remove.png").write_bytes(b"remove")
            result = root / "Chapter 2" / "result"
            result.mkdir()
            (result / "keep.png").write_bytes(b"result")
            CreateJunction(str(external), str(link))
            try:
                folders, removed, errors = clear_work_folders(root)

                self.assertEqual((folders, removed), (2, 2))
                self.assertEqual(len(errors), 1)
                self.assertIn(str(link), errors[0])
                self.assertTrue(link.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
                self.assertEqual(sentinel.read_bytes(), b"keep")
                self.assertEqual((nested / "preserved.png").read_bytes(), b"preserved")
                self.assertFalse((inpainted / "remove.png").exists())
                self.assertTrue(mask.is_dir())
                self.assertEqual(list(mask.iterdir()), [])
                self.assertEqual((result / "keep.png").read_bytes(), b"result")
            finally:
                self.assertTrue(link.parent.resolve().is_relative_to(root.resolve()))
                link.rmdir()
                self.assertEqual(sentinel.read_bytes(), b"keep")


if __name__ == "__main__":
    unittest.main()
