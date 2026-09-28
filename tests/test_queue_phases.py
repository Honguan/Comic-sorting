import io
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from comic_core import load_json, save_json
from queue_phases import MARKER, mark_prepared, prepared
from queue_worker import Job, run_jobs, run_translation


class PhaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / 'config.json'
        save_json(self.config, {'module': {'enable_translate': True}, 'other': 'preserve'})
        self.jobs = []
        for name in ('Chapter 1', 'Chapter 2'):
            path = self.root / name
            path.mkdir()
            (path / '[1].png').write_bytes(b'image')
            self.jobs.append(Job(path, 'translate'))

    def artifacts(self, path):
        save_json(path / f'imgtrans_{path.name}.json', {'pages': {'[1].png': [{'text': ['OCR'], 'lines': [], 'xyxy': [0, 0, 10, 10]}]}})
        for folder in ('mask', 'inpainted'):
            (path / folder).mkdir(exist_ok=True)
            (path / folder / '[1].png').write_bytes(b'artifact')

    def run_mode(self, mode, execute, **options):
        events = []
        settings = dict(bt_path='installed', bt_config=str(self.config), bt_workflow=mode, **options)
        with patch('queue_worker.translator_command', side_effect=lambda root, config, python, chapter, **kw:
                   ([str(chapter), config], self.root, None)), patch('queue_worker.run_translation', side_effect=execute):
            run_jobs(self.jobs, settings, '', True, threading.Event(), events.append)
        return events

    def test_two_pass_order_configs_models_and_usage(self):
        calls, sessions = [], {}
        def execute(command, root, env, stop, progress, **kw):
            path, config = Path(command[0]), load_json(command[1], {})
            phase = kw['request']['phase']
            calls.append((phase, path.name))
            kw['retry']()
            sessions.setdefault(phase, []).append(kw['session'])
            self.assertEqual(config['other'], 'preserve')
            self.assertEqual(config['module']['enable_translate'], phase == 'translate')
            for name in ('detect', 'ocr', 'inpaint'):
                self.assertEqual(config['module']['enable_' + name], phase == 'prepare')
            if phase == 'prepare':
                self.artifacts(path)
            else:
                self.assertTrue(prepared(path, [path / '[1].png']))
                (path / 'result').mkdir()
                (path / 'result/[1].png').write_bytes(b'translated')
            kw['usage'](dict(scope='total', requests=1, total_tokens=10, cost=None if phase == 'prepare' else '0.3',
                             missing_usage_requests=0, unpriced_requests=int(phase == 'prepare')))
        with patch('queue_worker.TranslationSession') as session:
            session.side_effect = lambda *a: Mock()
            events = self.run_mode('two_pass', execute)
        self.assertEqual(calls, [('prepare', 'Chapter 1'), ('prepare', 'Chapter 2'),
                                 ('translate', 'Chapter 1'), ('translate', 'Chapter 2')])
        self.assertIs(sessions['prepare'][0], sessions['prepare'][1])
        self.assertIs(sessions['translate'][0], sessions['translate'][1])
        self.assertIsNot(sessions['prepare'][0], sessions['translate'][0])
        for values in sessions.values():
            values[0].close.assert_called_once_with(graceful=True)
        self.assertEqual([e[2] for e in events if e[0] == 'status'],
                         ['running', 'prepared', 'running', 'prepared', 'running', 'done', 'running', 'done'])
        self.assertEqual([e for e in events if e[0] == 'usage'][-1][2]['requests'], 2)
        self.assertEqual([e for e in events if e[0] == 'usage'][-1][2]['cost'], '0.3')
        self.assertEqual(events.count(('done',)), 1)
        self.assertEqual(events.count(('retry',)), 4)
        self.assertEqual(load_json(self.config, {}), {'module': {'enable_translate': True}, 'other': 'preserve'})

    def test_usage_snapshots_replace_within_phase_and_sum_only_matching_job_scope(self):
        from queue_phases import run_phased_jobs
        events = []
        def worker(jobs, settings, output, skip, stop, emit, **kw):
            for index in range(len(jobs)):
                for count in (1, 3):
                    for scope in ('OCR', 'translation'):
                        emit(('usage', index, dict(scope=scope, requests=count + index,
                                                  cost=None if scope == 'OCR' else '0.25')))
                    emit(('jev_stats', index, dict(calls=count + index, elapsed_seconds=0.5)))
        with patch('queue_worker.run_jobs', side_effect=worker):
            run_phased_jobs(self.jobs, dict(bt_config=str(self.config), bt_workflow='two_pass'),
                            '', True, threading.Event(), events.append)
        for index in range(2):
            for scope in ('OCR', 'translation'):
                snapshots = [e[2] for e in events if e[:2] == ('usage', index) and e[2]['scope'] == scope]
                self.assertEqual([s['requests'] for s in snapshots],
                                 [1 + index, 3 + index, 4 + 2 * index, 6 + 2 * index])
                self.assertEqual(snapshots[-1]['cost'], None if scope == 'OCR' else '0.50')
            jev = [e[2] for e in events if e[:2] == ('jev_stats', index)]
            self.assertEqual(jev[-1], dict(calls=6 + 2 * index, elapsed_seconds=1.0))

    def test_restart_validation_and_missing_changed_artifacts(self):
        path = self.jobs[0].path
        self.artifacts(path)
        page = path / '[1].png'
        mark_prepared(path, [page])
        self.assertTrue(prepared(Path(str(path)), [page]))
        self.assertFalse(prepared(path, []))
        for file in (page, path / 'mask/[1].png', path / 'inpainted/[1].png'):
            old = file.read_bytes()
            file.write_bytes(old + b'changed')
            self.assertFalse(prepared(path, [page]))
            mark_prepared(path, [page])
        (path / 'inpainted/[1].png').unlink()
        self.assertFalse(prepared(path, [page]))
        events = self.run_mode('translate', Mock(side_effect=AssertionError('must skip')))
        self.assertEqual([e[2] for e in events if e[0] == 'status'], ['running', 'skipped', 'running', 'skipped'])

    def test_preprocessing_does_not_export_clean_or_accept_failed_results(self):
        self.jobs = self.jobs[:1]
        path = self.jobs[0].path
        self.jobs[0].range_export = True
        def execute(*args, **kwargs):
            self.artifacts(path)
        with patch('queue_worker.export_chapter') as export, patch('queue_worker.clear_work_folders') as cleanup:
            events = self.run_mode('prepare', execute, bt_export=True, bt_cleanup=True)
            export.assert_not_called()
            cleanup.assert_not_called()
        self.assertIn(('status', 0, 'prepared', ''), events)
        self.assertTrue((path / MARKER).exists())
        self.assertFalse(any(e[0] == 'result' for e in events))
        self.run_mode('prepare', Mock(side_effect=AssertionError('already prepared')))
        (path / '[1].png').write_bytes(b'new source')
        events = self.run_mode('prepare', lambda *a, **k: 'stage error')
        self.assertFalse(prepared(path, [path / '[1].png']))
        self.assertTrue(any(e[:3] == ('status', 0, 'failed') for e in events))

    def test_native_success_required_and_preprocessing_never_renders(self):
        from bt_run_bridge import install_session
        class Window:
            def on_run_imgtrans(self): pass
            def run_next_dir(self): pass
            def on_pagtrans_finished(self, index):
                self.imgtrans_proj.save()
                self.saveCurrentPage(False, False)
            def saveCurrentPage(self, *args): raise AssertionError('must not render')
        request = dict(phase='prepare', token='job', pages=None)
        install_session(Window, SimpleNamespace(pbar={}), request)
        window = Window()
        window.imgtrans_proj = Mock()
        window.on_pagtrans_finished(0)
        window.imgtrans_proj.save.assert_called_once()
        for native in ('', 'COMIC_SORTING_FINISHED:job:0\n', 'COMIC_SORTING_FINISHED:job:1\n'):
            session = Mock(fatal=threading.Event())
            session.open.return_value = Mock(stdout=io.StringIO(native + 'finished translating all dirs\nCOMIC_SORTING_DONE:job\n'))
            args = ([], self.root, None, threading.Event(), lambda *a: None)
            if ':1' in native:
                self.assertIsNone(run_translation(*args, session=session, request=request))
            else:
                with self.assertRaises(RuntimeError):
                    run_translation(*args, session=session, request=request)

    def test_blank_pages_ranges_and_interrupted_preprocessing(self):
        path = self.jobs[0].path
        first = path / '[1].png'
        second = path / '2.png'
        second.write_bytes(b'blank')
        self.artifacts(path)
        project = path / f'imgtrans_{path.name}.json'
        data = load_json(project, {})
        data['pages'][second.name] = []
        save_json(project, data)
        mark_prepared(path, [first])
        self.assertFalse(prepared(path, [first, second]))
        mark_prepared(path, [second])
        self.assertTrue(prepared(path, [first, second]))
        data['pages'][first.name][0]['text'] = ['changed OCR']
        save_json(project, data)
        self.assertFalse(prepared(path, [first]))
        def interrupted(*args, **kw):
            args[3].set()
        self.jobs = self.jobs[:1]
        events = self.run_mode('prepare', interrupted)
        self.assertFalse(prepared(path, [first, second]))
        self.assertTrue(any(e[:3] == ('status', 0, 'cancelled') for e in events))

    def test_failed_first_pass_never_translates_and_quota_stops(self):
        calls = []
        def execute(command, *args, **kw):
            phase = kw['request']['phase']
            calls.append((phase, Path(command[0]).name))
            if phase == 'prepare':
                raise RuntimeError('Your workspace is out of credits')
        events = self.run_mode('two_pass', execute)
        self.assertEqual(calls, [('prepare', 'Chapter 1')])
        self.assertFalse(any(e[:2] == ('status', 1) for e in events))

    def test_two_pass_real_subprocess_protocol(self):
        script = '''
import json, os, sys
from pathlib import Path
request = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
while True:
    path = Path(request['path'])
    with (path.parent / 'processes.jsonl').open('a') as f:
        f.write(json.dumps([request['phase'], os.getpid()]) + '\\n')
    if request['phase'] == 'prepare':
        (path / ('imgtrans_' + path.name + '.json')).write_text(json.dumps({'pages': {'[1].png': []}}))
    else:
        (path / 'result').mkdir()
        (path / 'result/[1].png').write_bytes(b'translated')
    print('COMIC_SORTING_FINISHED:' + request['token'] + ':1', flush=True)
    print('finished translating all dirs', flush=True)
    print('COMIC_SORTING_DONE:' + request['token'], flush=True)
    line = sys.stdin.readline()
    if not line or line.strip() == 'exit': break
    request = json.loads(line)
'''
        def command(root, config, python, chapter, **kwargs):
            return [sys.executable, '-u', '-c', script, str(kwargs['page_manifest'])], self.root, None
        events = []
        with patch('queue_worker.translator_command', side_effect=command):
            run_jobs(self.jobs, dict(bt_path='installed', bt_config=str(self.config), bt_workflow='two_pass'),
                     '', True, threading.Event(), events.append)
        self.assertEqual([e[2] for e in events if e[0] == 'status'],
                         ['running', 'prepared', 'running', 'prepared', 'running', 'done', 'running', 'done'])
        records = [json.loads(line) for line in (self.root / 'processes.jsonl').read_text().splitlines()]
        self.assertEqual(records[0], records[1])
        self.assertEqual(records[2], records[3])
        self.assertNotEqual(records[0][1], records[2][1])


if __name__ == '__main__':
    unittest.main()
