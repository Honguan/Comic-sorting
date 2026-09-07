import os
import stat
import tempfile
import unittest
from pathlib import Path

from comic_core import clear_work_folders

if os.name == "nt":
    from _winapi import CreateJunction


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
