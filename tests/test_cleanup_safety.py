import os
import tempfile
import threading
import unittest
from pathlib import Path

from comic_core import clear_work_folders, export_chapter, save_json
from queue_worker import Job, run_jobs


class CleanupSafetyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.chapter = self.root / 'Mangas' / 'Series' / 'Chapter 1'
        self.output = self.root / 'Komga'
        self.chapter.mkdir(parents=True)
        (self.chapter / '1.png').write_bytes(b'source')
        self.artifacts = []
        for name in ('mask', 'inpainted'):
            folder = self.chapter / name
            folder.mkdir()
            artifact = folder / '1.png'
            artifact.write_bytes(b'needed-to-resume')
            self.artifacts.append(artifact)

    def exported(self):
        result = self.chapter / 'result'
        result.mkdir(exist_ok=True)
        (result / '1.png').write_bytes(b'translated')
        state = {}
        _, archive = export_chapter(self.chapter.parent, self.chapter, self.output, state)
        save_json(self.output / '.comic-sorting-state.json', state)
        return archive, state

    def assert_preserved(self):
        for artifact in self.artifacts:
            self.assertEqual(artifact.read_bytes(), b'needed-to-resume')
        self.assertEqual((self.chapter / '1.png').read_bytes(), b'source')

    def test_unexported_preprocessing_and_unexported_results_are_preserved(self):
        for translated in (False, True):
            with self.subTest(translated=translated):
                if translated:
                    (self.chapter / 'result').mkdir()
                    (self.chapter / 'result/1.png').write_bytes(b'translated')
                folders, removed, errors = clear_work_folders(self.chapter, self.output)
                self.assertEqual((folders, removed), (2, 0))
                self.assertTrue(errors)
                self.assert_preserved()

    def test_verified_export_cleans_only_work_contents(self):
        archive, _ = self.exported()
        original_archive = archive.read_bytes()
        self.assertEqual(clear_work_folders(self.chapter, self.output), (2, 2, []))
        for artifact in self.artifacts:
            self.assertTrue(artifact.parent.is_dir())
            self.assertFalse(artifact.exists())
        self.assertEqual(archive.read_bytes(), original_archive)
        self.assertEqual((self.chapter / 'result/1.png').read_bytes(), b'translated')
        self.assertEqual((self.chapter / '1.png').read_bytes(), b'source')

    def test_changed_results_or_missing_archive_keep_work_files(self):
        archive, _ = self.exported()
        (self.chapter / 'result/1.png').write_bytes(b'new translation')
        self.assertEqual(clear_work_folders(self.chapter, self.output)[1], 0)
        self.assert_preserved()
        archive, _ = self.exported()
        archive.unlink()
        self.assertEqual(clear_work_folders(self.chapter, self.output)[1], 0)
        self.assert_preserved()

    def test_archive_crc_is_checked_even_when_size_and_time_match(self):
        archive, _ = self.exported()
        info = archive.stat()
        payload = bytearray(archive.read_bytes())
        payload[payload.index(b'translated')] ^= 1
        archive.write_bytes(payload)
        os.utime(archive, ns=(info.st_atime_ns, info.st_mtime_ns))
        self.assertEqual(clear_work_folders(self.chapter, self.output)[1], 0)
        self.assert_preserved()

    def test_new_preprocessing_preserves_both_work_folders(self):
        archive, _ = self.exported()
        newer = archive.stat().st_mtime_ns + 1_000_000_000
        os.utime(self.artifacts[-1], ns=(newer, newer))
        self.assertEqual(clear_work_folders(self.chapter, self.output)[1], 0)
        self.assert_preserved()

    def test_foreign_or_malformed_export_record_does_not_authorize_cleanup(self):
        _, state = self.exported()
        record = state['Series/Chapter 1']
        for source in (str(self.root / 'other/result'), 12, None):
            with self.subTest(source=source):
                record['source'] = source
                save_json(self.output / '.comic-sorting-state.json', state)
                self.assertEqual(clear_work_folders(self.chapter, self.output)[1], 0)
                self.assert_preserved()

    def test_queue_cleanup_cannot_bypass_export_validation(self):
        events = []
        run_jobs([Job(self.chapter, 'cleanup')], {}, str(self.output), True,
                 threading.Event(), events.append)
        self.assertIn('failed', [event[2] for event in events if event[0] == 'status'])
        self.assert_preserved()
        self.exported()
        events.clear()
        run_jobs([Job(self.chapter, 'cleanup')], {}, str(self.output), True,
                 threading.Event(), events.append)
        self.assertIn(('status', 0, 'done', ''), events)
        self.assertTrue(all(not path.exists() for path in self.artifacts))


if __name__ == '__main__':
    unittest.main()
