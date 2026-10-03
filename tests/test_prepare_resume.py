import io
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from comic_core import load_json, save_json
from queue_phases import MARKER, mark_prepared, prepared
from queue_worker import run_translation
import test_queue_phases as fixtures


class PrepareResumeTests(unittest.TestCase):
    setUp = fixtures.PhaseTests.setUp
    artifacts = fixtures.PhaseTests.artifacts
    run_mode = fixtures.PhaseTests.run_mode

    def pages(self):
        self.jobs = self.jobs[:1]
        path = self.jobs[0].path
        self.artifacts(path)
        project = load_json(path / f'imgtrans_{path.name}.json', {})
        (path / '[1].png').replace(path / '1.png')
        project['pages']['1.png'] = project['pages'].pop('[1].png')
        for folder in ('mask', 'inpainted'):
            (path / folder / '[1].png').replace(path / folder / '1.png')
        for name in ('2.png', '3.png', '4.png'):
            (path / name).write_bytes(b'source')
            project['pages'][name] = [dict(text=['OCR'], lines=[], xyxy=[0, 0, 10, 10])]
            for folder in ('mask', 'inpainted'):
                (path / folder / name).write_bytes(b'artifact')
        save_json(path / f'imgtrans_{path.name}.json', project)
        return path, [path / name for name in ('1.png', '2.png', '3.png', '4.png')]

    def test_parallel_prepares_only_missing_pages_in_selected_range(self):
        path, pages = self.pages()
        mark_prepared(path, [pages[0], pages[2]])
        before = load_json(path / MARKER, {})['pages']
        self.jobs[0].start_page = 2
        calls = []

        def execute(command, root, env, stop, progress, **kw):
            phase = kw['request']['phase']
            calls.append((phase, kw['request']['pages']))
            if phase == 'translate':
                (path / 'result').mkdir()
                for source in pages[1:]:
                    (path / 'result' / source.name).write_bytes(b'translated')

        with patch('queue_worker.TranslationSession', side_effect=lambda *args: Mock()):
            self.run_mode('parallel', execute)
        self.assertEqual(calls, [('prepare', ['2.png', '4.png']),
                                 ('translate', ['2.png', '3.png', '4.png'])])
        after = load_json(path / MARKER, {})['pages']
        self.assertEqual(after['1.png'], before['1.png'])
        self.assertEqual(after['3.png'], before['3.png'])
        self.assertTrue(prepared(path, pages))

    def test_cancel_then_restart_keeps_committed_page(self):
        path, pages = self.pages()

        def interrupted(command, root, env, stop, progress, **kw):
            (path / 'inpainted' / pages[0].name).write_bytes(b'new artifact')
            kw['on_prepared'](pages[0].name)
            stop.set()
            raise RuntimeError('stopped')

        with patch('queue_worker.TranslationSession', side_effect=lambda *args: Mock()):
            events = self.run_mode('parallel', interrupted)
        self.assertTrue(prepared(path, pages[:1]))
        self.assertFalse(prepared(path, pages))
        self.assertTrue(any(e[0] == 'phase_event' and e[2][:3] == ('status', 0, 'cancelled') for e in events))
        calls = []

        def resumed(command, root, env, stop, progress, **kw):
            calls.append(kw['request']['pages'])

        with patch('queue_worker.TranslationSession', side_effect=lambda *args: Mock()):
            self.run_mode('prepare', resumed)
        self.assertEqual(calls, [['2.png', '3.png', '4.png']])

    def test_failed_inpaint_or_changed_source_is_not_checkpointed(self):
        for failure in ('unchanged_artifact', 'missing_project_page', 'changed_source'):
            with self.subTest(failure=failure):
                if failure != 'unchanged_artifact':
                    self.setUp()
                path, pages = self.pages()
                project_path = path / f'imgtrans_{path.name}.json'
                project = load_json(project_path, {})
                if failure == 'missing_project_page':
                    incomplete = dict(project, pages={})
                    save_json(project_path, incomplete)

                def interrupted(command, root, env, stop, progress, **kw):
                    if failure == 'changed_source':
                        pages[0].write_bytes(b'changed source')
                        (path / 'inpainted' / pages[0].name).write_bytes(b'new artifact')
                    if failure == 'missing_project_page':
                        save_json(project_path, project)
                    kw['on_prepared'](pages[0].name)
                    stop.set()
                    raise RuntimeError('stopped')

                with patch('queue_worker.TranslationSession', side_effect=lambda *args: Mock()):
                    self.run_mode('prepare', interrupted)
                self.assertFalse(prepared(path, pages[:1]))

    def test_page_checkpoint_protocol_is_saved_and_bound_to_job(self):
        from bt_run_bridge import install_session
        order = []

        class Window:
            def on_run_imgtrans(self): pass
            def run_next_dir(self): pass
            def saveCurrentPage(self): pass
            def on_pagtrans_finished(self, index):
                order.append('saved')

        request = dict(phase='prepare', token='job', pages=None)
        install_session(Window, SimpleNamespace(pbar={}), request)
        window = Window()
        window.imgtrans_proj = SimpleNamespace(idx2pagename=lambda index: '1.png',
                                               get_page_progress=lambda page: True)
        output = io.StringIO()
        with redirect_stdout(output):
            window.on_pagtrans_finished(0)
        self.assertEqual(order, ['saved'])
        self.assertIn('COMIC_SORTING_PREPARED:', output.getvalue())
        lines = output.getvalue().replace('"job"', '"previous"') + output.getvalue()
        lines += 'COMIC_SORTING_FINISHED:job:1\nfinished translating all dirs\nCOMIC_SORTING_DONE:job\n'
        session = Mock(fatal=threading.Event())
        session.open.return_value = Mock(stdout=io.StringIO(lines))
        saved = []
        run_translation([], self.root, None, threading.Event(), lambda *args: None,
                        session=session, request=request, on_prepared=saved.append)
        self.assertEqual(saved, ['1.png'])
        session.open.return_value = Mock(stdout=io.StringIO('[ERROR] module_manager:save:1 - failed\n' + lines))
        saved.clear()
        run_translation([], self.root, None, threading.Event(), lambda *args: None,
                        session=session, request=request, on_prepared=saved.append)
        self.assertEqual(saved, [])


if __name__ == '__main__':
    unittest.main()
